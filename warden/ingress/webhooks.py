"""POST /webhooks/{platform}：只做校验、去重、入队，重活全部交给 worker，保证快速返回。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .dedupe import first_seen

if TYPE_CHECKING:
    from warden.app import Warden

router = APIRouter()


@router.post("/webhooks/{platform}")
async def receive(platform: str, request: Request) -> JSONResponse:
    warden: Warden = request.app.state.warden
    adapter = warden.platforms.get(platform)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"unknown platform: {platform}")

    body = await request.body()
    if not adapter.verify_webhook(request.headers, body):
        raise HTTPException(status_code=401, detail="invalid signature")

    event = adapter.parse_event(request.headers, body)
    if event is None:
        return JSONResponse({"status": "ignored"})
    if not await first_seen(warden.db, platform, event.delivery_id, event.name):
        return JSONResponse({"status": "duplicate"})

    await warden.queue.put(event)
    return JSONResponse({"status": "queued"}, status_code=202)
