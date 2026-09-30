"""POST /webhooks/{platform}：只做校验、去重、入队，重活全部交给 worker，保证快速返回。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from opentelemetry import context

from failgate import tracing
from failgate.orchestrator.dispatch import case_key

from .dedupe import first_seen

if TYPE_CHECKING:
    from failgate.app import FailGate

router = APIRouter()


@router.post("/webhooks/{platform}")
async def receive(platform: str, request: Request) -> JSONResponse:
    failgate: FailGate = request.app.state.failgate
    adapter = failgate.platforms.get(platform)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"unknown platform: {platform}")

    body = await request.body()
    if not adapter.verify_webhook(request.headers, body):
        raise HTTPException(status_code=401, detail="invalid signature")

    kind = request.headers.get("x-github-event", "?")
    # 一次投递一条 trace 的根：之后快车道、沙箱车道的 span 都接在它下面（ADR 0025）
    with tracing.tracer.start_as_current_span(f"webhook {platform}/{kind}") as span:
        event = adapter.parse_event(request.headers, body)
        if event is None:
            span.set_attribute("failgate.status", "ignored")
            return JSONResponse({"status": "ignored"})
        span.set_attribute("failgate.delivery_id", event.delivery_id)
        if not await first_seen(failgate.db, platform, event.delivery_id, event.name):
            span.set_attribute("failgate.status", "duplicate")
            return JSONResponse({"status": "duplicate"})
        span.set_attribute("failgate.status", "queued")
        key = case_key(event)
        if key is None:
            await failgate.enqueue(event.model_copy(update={"trace": tracing.inject()}))
        else:
            # 会话信息放进 Baggage，跟 traceparent 一起注入：队列另一头的 span 都会带上
            name = tracing.trace_name(event.name, key)
            span.set_attributes({tracing.CASE: key, tracing.SESSION: key, tracing.TRACE_NAME: name})
            capture = tracing.capture_enabled(failgate.settings, event.repo.full_name)
            token = context.attach(tracing.with_case(None, key, name, capture=capture))
            try:
                await failgate.enqueue(event.model_copy(update={"trace": tracing.inject()}))
            finally:
                context.detach(token)
    return JSONResponse({"status": "queued"}, status_code=202)
