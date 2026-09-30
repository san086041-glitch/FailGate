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
import contextlib
import json
import logging
from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING, Any

from opentelemetry import baggage, context, propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SpanExporter,
)

from failgate.policy.secrets_scan import find_secrets

if TYPE_CHECKING:
    from failgate.settings import Settings

log = logging.getLogger(__name__)

tracer = trace.get_tracer("failgate")

# 自己加的属性都放在 failgate.* 下；Langfuse 认的放在 langfuse.* 下
CASE = "failgate.case"
SESSION = "langfuse.session.id"
TRACE_NAME = "langfuse.trace.name"
# 这些键放在 Baggage 里随上下文传递（包括跨队列），每个 span 启动时抄成自己的属性：
# Langfuse 要求会话 ID 出现在 trace 里的每一个 span 上，只写在一个 span 上不算
PROPAGATED = (CASE, SESSION, TRACE_NAME)


class BaggageAttributes(SpanProcessor):
    """span 启动时把上下文 Baggage 里的 PROPAGATED 键抄成 span 属性（Langfuse 推荐的做法）。"""

    def on_start(self, span: Span, parent_context: context.Context | None = None) -> None:
        for key in PROPAGATED:
            value = baggage.get_baggage(key, parent_context)
            if value is not None:
                span.set_attribute(key, str(value))


def with_case(ctx: context.Context | None, case_key: str, trace_name: str, *,
              capture: bool = False) -> context.Context:
    """在 ctx 上挂好这个 Case 的会话信息；之后在它下面开的 span（包括队列另一头的）都会带上。

    capture=True 时顺带挂上"记内容"的标记（只加不撤：已经挂上的不会被清掉）。"""
    ctx = baggage.set_baggage(CASE, case_key, context=ctx)
    ctx = baggage.set_baggage(SESSION, case_key, context=ctx)
    if capture:
        ctx = baggage.set_baggage(CAPTURE, "1", context=ctx)
    return baggage.set_baggage(TRACE_NAME, trace_name, context=ctx)


# ---- 记不记内容（prompt、回答、skill 的输入输出、容器输出）

# Baggage 里的标记：webhook 入口按仓库决定，跟着上下文传到所有子 span（包括沙箱车道）。
# 这样 LLM 客户端、skill、沙箱都不用知道是哪个仓库
CAPTURE = "failgate.capture_content"
MAX_CONTENT = 8000  # 每个属性最多保留的字符数


def capture_enabled(settings: Settings, repo: str) -> bool:
    """这个仓库的 trace 要不要记内容：全局开关，或在 TRACING_CAPTURE_REPOS 白名单里（* = 全部）。"""
    if settings.tracing_capture_content:
        return True
    allowed = {r.strip().lower() for r in settings.tracing_capture_repos.split(",") if r.strip()}
    return "*" in allowed or repo.lower() in allowed


def capturing() -> bool:
    return baggage.get_baggage(CAPTURE) == "1"


def content(value: Any) -> str:
    """要放进 span 的内容：先过密钥扫描（疑似密钥整段隐去），再截断。"""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    hits = find_secrets(text)
    if hits:
        return f"[已隐去：疑似含密钥（{', '.join(hits)}）]"
    if len(text) > MAX_CONTENT:
        return f"{text[:MAX_CONTENT]}…（截断，共 {len(text)} 字符）"
    return text


def set_io(span: trace.Span, *, input: Any = None, output: Any = None) -> None:  # noqa: A002
    """只在当前 trace 允许记内容时，把输入 / 输出写成 Langfuse 认的属性。"""
    if not capturing():
        return
    if input is not None:
        span.set_attribute("langfuse.observation.input", content(input))
    if output is not None:
        span.set_attribute("langfuse.observation.output", content(output))


@contextlib.contextmanager
def attached(ctx: context.Context) -> Iterator[None]:
    """把 ctx 设成当前上下文（从队列里恢复出来的 trace 和 Baggage）。

    不能只把它传给 start_as_current_span(context=ctx)：那样 ctx 只用来找父 span，
    新 span 变成"当前"时是在调用前的上下文上改的，ctx 里的 Baggage 到不了子 span。"""
    token = context.attach(ctx)
    try:
        yield
    finally:
        context.detach(token)


def trace_name(event_name: str, case_key: str) -> str:
    """Langfuse 列表里显示的 trace 名：`issue.opened · failgate-demo#12`。

    case_key 的格式是 `平台:owner/repo:kind:编号`（dispatch.case_key）。"""
    _platform, repo, _kind, number = case_key.split(":", 3)
    return f"{event_name} · {repo.rsplit('/', 1)[-1]}#{number}"


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
        # 官方文档要求：带上这个头走 v4 的实时写入（不带会进旧的批处理管道）
        return endpoint, {"Authorization": f"Basic {token}", "x-langfuse-ingestion-version": "4"}
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
    provider.add_span_processor(BaggageAttributes())
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
