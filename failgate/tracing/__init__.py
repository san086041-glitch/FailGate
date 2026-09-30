"""链路追踪（W6，ADR 0025）：OpenTelemetry，OTLP 导出（Langfuse 或任何 OTel 后端）。

一次 webhook 在一条 trace 里：

    webhook github/issues                       ← 收到投递
    └─ event issue.opened                       ← 快车道（Case 锁、状态机、快阶段）
       ├─ skill triage   ─ chat deepseek-flash  ← LLM 调用（GenAI 语义约定：模型、token、花费）
       ├─ skill dedup    ─ chat …
       ├─ effect upsert_summary                 ← 写回平台
       └─ （投沙箱任务：traceparent 随任务参数进队列，包括 Redis）
          sandbox job                           ← 沙箱车道
          └─ skill verify ─ sandbox run …       ← 容器执行

状态转换记成所在 span 上的事件。Langfuse 按 `langfuse.session.id`（= Case 键）把同一个
Case 的多条 trace 归成一个会话。

默认不导出（TRACING_EXPORTER=none），也不记 prompt 和回答内容（sandbox 仓库是私有的），
要看内容设 TRACING_CAPTURE_CONTENT=true。没调用 setup() 时所有 span 都是空操作，不花什么钱。
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from opentelemetry import context, propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SpanExporter,
)

if TYPE_CHECKING:
    from failgate.settings import Settings

log = logging.getLogger(__name__)

tracer = trace.get_tracer("failgate")

# 自己加的属性都放在 failgate.* 下；Langfuse 认的放在 langfuse.* 下
CASE = "failgate.case"
SESSION = "langfuse.session.id"


def otlp_target(settings: Settings) -> tuple[str, dict[str, str]] | None:
    """OTLP/HTTP 的 traces 地址和请求头：显式配置优先，其次按 Langfuse 的三项拼出来。"""
    if settings.otlp_endpoint:
        headers = dict(
            kv.split("=", 1) for kv in settings.otlp_headers.split(",") if "=" in kv
        )
        return settings.otlp_endpoint, headers
    if settings.langfuse_public_key and settings.langfuse_secret_key:
        token = base64.b64encode(
            f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
        ).decode()
        endpoint = settings.langfuse_host.rstrip("/") + "/api/public/otel/v1/traces"
        return endpoint, {"Authorization": f"Basic {token}"}
    return None


def build_exporter(settings: Settings) -> SpanExporter | None:
    kind = settings.tracing_exporter
    if kind == "console":
        return ConsoleSpanExporter()
    if kind == "otlp":
        target = otlp_target(settings)
        if target is None:
            log.warning("TRACING_EXPORTER=otlp 但没有配置 OTLP 地址或 Langfuse 密钥：不导出")
            return None
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        endpoint, headers = target
        return OTLPSpanExporter(endpoint=endpoint, headers=headers)
    return None


def setup(settings: Settings) -> TracerProvider | None:
    """按配置装一个全局 TracerProvider。OTel 只允许设一次全局的：已经设过就沿用。"""
    exporter = build_exporter(settings)
    if exporter is None:
        return None
    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        return current
    resource = Resource.create({"service.name": settings.otel_service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    log.info("tracing enabled: %s", settings.tracing_exporter)
    return provider


def shutdown(provider: TracerProvider | None) -> None:
    if provider is not None:
        provider.shutdown()  # 把还在缓冲区里的 span 发出去


def inject() -> dict[str, str]:
    """当前 trace 上下文 → 可以放进队列任务的字典（W3C traceparent）。"""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


def extract(carrier: Mapping[str, str] | None) -> context.Context:
    return propagate.extract(dict(carrier or {}))


def event(name: str, **attrs: Any) -> None:
    """在当前 span 上记一个事件（状态转换等）；没有活动 span 时什么也不做。"""
    trace.get_current_span().add_event(name, {k: _attr(v) for k, v in attrs.items()})


def _attr(v: Any) -> Any:
    return v if isinstance(v, str | int | float | bool) else json.dumps(v, ensure_ascii=False)
