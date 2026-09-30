"""链路追踪（ADR 0025）：一次投递一条 trace，沙箱任务接在触发它的事件下面。"""

from __future__ import annotations

import base64
from collections.abc import Sequence

from conftest import SPANS, Harness, _harness, issue_event, make_settings
from opentelemetry.sdk.trace import ReadableSpan
from test_verify_pipeline import FakeVerifyRunner, pull_event, verification

from failgate import tracing
from failgate.verify.engine import ClaimVerdict


def by_name(spans: Sequence[ReadableSpan], name: str) -> list[ReadableSpan]:
    return [s for s in spans if s.name == name]


def one(spans: Sequence[ReadableSpan], name: str) -> ReadableSpan:
    found = by_name(spans, name)
    assert len(found) == 1, (name, [s.name for s in spans])
    return found[0]


def parent_of(span: ReadableSpan) -> int | None:
    return span.parent.span_id if span.parent is not None else None


async def test_issue_event_is_one_trace_from_webhook_to_llm(harness: Harness):
    SPANS.clear()
    await harness.send("issues", issue_event("opened", 1), "d-1")
    await harness.failgate.worker.drain()
    spans = SPANS.get_finished_spans()

    root = one(spans, "webhook github/issues")
    assert root.attributes["failgate.status"] == "queued"
    # FastAPI ≥ 0.142 检测到 OTel 时自带 span：POST /webhooks/{platform} → fastapi.endpoint，
    # webhook span 挂在它们下面；旧版本 FastAPI 下 webhook span 自己就是根
    ancestors = []
    parent = root.parent
    while parent is not None:
        p = next(s for s in spans if s.context.span_id == parent.span_id)
        ancestors.append(p.name)
        parent = p.parent
    assert ancestors in ([], ["fastapi.endpoint", "POST /webhooks/{platform}"])
    event = one(spans, "event issue.opened")
    # 跨过了进程内队列，仍然接在 webhook 的 span 下面
    assert event.context.trace_id == root.context.trace_id
    assert parent_of(event) == root.context.span_id
    assert event.attributes[tracing.SESSION] == "github:acme/widgets:issue:1"

    skills = [s for s in spans if s.name.startswith("skill ")]
    assert [s.name for s in skills] == ["skill intake", "skill triage", "skill dedup"]
    assert all(parent_of(s) == event.context.span_id for s in skills)

    # LLM 调用是 skill 的子 span，属性按 GenAI 语义约定
    chats = [s for s in spans if s.name.startswith("chat ")]
    assert chats and all(parent_of(c) in {s.context.span_id for s in skills} for c in chats)
    attrs = chats[0].attributes
    assert attrs["gen_ai.request.model"] == "deepseek-flash"
    assert attrs["gen_ai.usage.input_tokens"] == 1000
    assert attrs["gen_ai.usage.output_tokens"] == 100
    assert attrs["gen_ai.usage.cache_read.input_tokens"] == 600
    assert attrs["failgate.cost_usd"] > 0
    # 默认不记 prompt 和回答
    assert "langfuse.observation.input" not in attrs

    # Langfuse 要求会话 ID 出现在每个 span 上：webhook 以下（包括 LLM 调用）全都带着
    ours = [s for s in spans if s.context.trace_id == root.context.trace_id
            and not s.name.startswith(("POST ", "fastapi."))]
    missing = [s.name for s in ours if tracing.SESSION not in s.attributes]
    assert ours and not missing, missing
    assert {s.attributes[tracing.SESSION] for s in ours} == {"github:acme/widgets:issue:1"}
    assert {s.attributes[tracing.TRACE_NAME] for s in ours} == {"issue.opened · widgets#1"}

    # 状态转换记成 span 事件
    moves = [e.attributes["to_state"] for s in spans for e in s.events if e.name == "transition"]
    assert moves[:4] == ["INTAKE", "TRIAGING", "DEDUPING", "TRIAGE_ONLY"]


async def test_sandbox_job_joins_the_trace_of_the_event_that_queued_it(tmp_path):
    runner = FakeVerifyRunner(verification(ClaimVerdict.VERIFIED))
    async for h in _harness(make_settings(tmp_path), verify_runner=runner):
        SPANS.clear()
        await h.send("pull_request", pull_event("opened"), "p-1")
        await h.failgate.worker.drain()
        spans = SPANS.get_finished_spans()
        root = one(spans, "webhook github/pull_request")
        event = one(spans, "event pull.opened")
        job = one(spans, "sandbox job")
        assert job.context.trace_id == root.context.trace_id
        assert parent_of(job) == event.context.span_id
        assert parent_of(one(spans, "skill verify")) == job.context.span_id
        # 会话信息放在 Baggage 里跟着任务过了队列：沙箱车道的 span 也带着
        for name in ("sandbox job", "skill verify"):
            assert one(spans, name).attributes[tracing.SESSION] == "github:acme/widgets:pull:12"
        assert [e.name for e in event.events].count("enqueue_sandbox") == 1


async def test_prompt_and_answer_are_recorded_only_when_asked(tmp_path):
    async for h in _harness(make_settings(tmp_path, tracing_capture_content=True)):
        SPANS.clear()
        await h.send("issues", issue_event("opened", 1), "d-1")
        await h.failgate.worker.drain()
        chat = next(s for s in SPANS.get_finished_spans() if s.name.startswith("chat "))
        assert "KeyError when reading parquet" in chat.attributes["langfuse.observation.input"]
        assert chat.attributes["langfuse.observation.output"]


def test_langfuse_keys_become_an_otlp_endpoint_and_basic_auth(tmp_path):
    s = make_settings(tmp_path, tracing_exporter="otlp", langfuse_host="https://lf.example/",
                      langfuse_public_key="pk-lf-1", langfuse_secret_key="sk-lf-2")
    endpoint, headers = tracing.otlp_target(s) or ("", {})
    assert endpoint == "https://lf.example/api/public/otel/v1/traces"
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"pk-lf-1:sk-lf-2").decode()
    assert headers["x-langfuse-ingestion-version"] == "4"
    # 显式的 OTLP 地址优先
    s2 = make_settings(tmp_path, otlp_endpoint="http://collector:4318/v1/traces",
                       otlp_headers="x-a=1,x-b=2",
                       langfuse_public_key="pk", langfuse_secret_key="sk")
    assert tracing.otlp_target(s2) == ("http://collector:4318/v1/traces", {"x-a": "1", "x-b": "2"})


def test_tracing_is_off_by_default(tmp_path):
    s = make_settings(tmp_path)
    assert s.tracing_exporter == "none" and tracing.setup(s) is None
    # 选了 otlp 但什么地址都没给：不导出，只打警告
    assert tracing.build_exporter(make_settings(tmp_path, tracing_exporter="otlp")) is None


async def test_local_sink_decodes_real_otlp_payloads(harness: Harness):
    # 用 OTel 官方的 OTLP 编码器把测试里生成的 span 编成请求体，再用 sink 解码、打成树
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans

    from failgate.tracing.sink import decode, render

    SPANS.clear()
    await harness.send("issues", issue_event("opened", 1), "d-1")
    await harness.failgate.worker.drain()
    body = encode_spans(SPANS.get_finished_spans()).SerializeToString()
    spans = list(decode(body))
    assert {s["name"] for s in spans} >= {"webhook github/issues", "event issue.opened"}
    tree = render(spans, hide=("chat deepseek-flash",))
    lines = tree.splitlines()
    # webhook 可能是根，也可能在 FastAPI 自带的 HTTP span 下面：只要求 event 紧跟在它下一层
    i = next(n for n, ln in enumerate(lines) if ln.strip().startswith("webhook github/issues"))
    indent = len(lines[i]) - len(lines[i].lstrip())
    assert lines[i + 1].startswith(" " * (indent + 2) + "event issue.opened")
    assert any(ln.strip().startswith("skill triage") for ln in lines)
    assert "个 chat deepseek-flash" in tree and "→ ['INTAKE'" in tree


def test_langfuse_base_url_from_the_console_snippet_is_accepted(tmp_path, monkeypatch):
    # Langfuse 控制台复制出来的片段用 LANGFUSE_BASE_URL（带引号），和 LANGFUSE_HOST 等价
    from failgate.settings import Settings

    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://jp.cloud.langfuse.com")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.langfuse_host == "https://jp.cloud.langfuse.com"
