"""本地 OTLP/HTTP 接收器和 span 树（ADR 0025）：没有 Langfuse 也能看链路。

    failgate trace sink spans.jsonl        # 监听 127.0.0.1:4318，收到的 span 按 JSON 行追加
    # 服务端：TRACING_EXPORTER=otlp、OTLP_ENDPOINT=http://127.0.0.1:4318/v1/traces
    failgate trace show spans.jsonl        # 按 trace 打成树

只解 OTLP/HTTP 的 protobuf 编码（Python 导出器的默认格式），只用来本地看，不是生产用的收集器。
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue


def _value(v: AnyValue) -> Any:
    for f in ("string_value", "int_value", "double_value", "bool_value"):
        if v.HasField(f):
            return getattr(v, f)
    return None


def _attrs(kvs: Iterable[KeyValue]) -> dict[str, Any]:
    return {a.key: _value(a.value) for a in kvs}


def decode(body: bytes) -> Iterator[dict[str, Any]]:
    req = ExportTraceServiceRequest()
    req.ParseFromString(body)
    for rs in req.resource_spans:
        service = _attrs(rs.resource.attributes).get("service.name")
        for ss in rs.scope_spans:
            for s in ss.spans:
                yield {
                    "service": service, "name": s.name, "trace": s.trace_id.hex(),
                    "span": s.span_id.hex(), "parent": s.parent_span_id.hex() or None,
                    "start": s.start_time_unix_nano, "end": s.end_time_unix_nano,
                    "status": s.status.code, "attrs": _attrs(s.attributes),
                    "events": [{"name": e.name, "attrs": _attrs(e.attributes)} for e in s.events],
                }


def serve(out: Path, host: str = "127.0.0.1", port: int = 4318) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 — http.server 的命名
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            with out.open("a", encoding="utf-8") as f:
                for span in decode(body):
                    f.write(json.dumps(span, ensure_ascii=False) + "\n")
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            return None

    HTTPServer((host, port), Handler).serve_forever()


# 树上每个 span 后面显示哪些属性（短名）
SHOWN = ("failgate.case", "failgate.state", "gen_ai.request.model", "gen_ai.usage.input_tokens",
         "gen_ai.usage.output_tokens", "failgate.cost_usd", "failgate.sandbox.exit_code",
         "failgate.effect.status", "failgate.layer.status", "failgate.status")


def render(spans: list[dict[str, Any]], *, hide: tuple[str, ...] = ()) -> str:
    """按 trace 分组、按父子关系缩进；hide 里的 span 名只计数不逐行显示。"""
    by_trace: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in spans:
        by_trace[s["trace"]].append(s)
    lines: list[str] = []

    def line(s: dict[str, Any], depth: int) -> str:
        ms = (s["end"] - s["start"]) / 1e6
        attrs = {k.rsplit(".", 1)[-1]: (round(v, 5) if isinstance(v, float) else v)
                 for k, v in s["attrs"].items() if k in SHOWN}
        moves = [e["attrs"].get("to_state") or e["name"] for e in s["events"]]
        tail = (f"  {attrs}" if attrs else "") + (f"  → {moves}" if moves else "")
        return f"{'  ' * depth}{s['name']}  {ms:,.0f} ms{tail}"

    def walk(kids: dict[str | None, list[dict[str, Any]]], parent: str | None,
             depth: int) -> None:
        children = sorted(kids[parent], key=lambda x: x["start"])
        hidden = [c for c in children if c["name"] in hide]
        for c in children:
            if c["name"] not in hide:
                lines.append(line(c, depth))
                walk(kids, c["span"], depth + 1)
        if hidden:
            total = sum(c["end"] - c["start"] for c in hidden) / 1e6
            name = hidden[0]["name"]
            lines.append(f"{'  ' * depth}（{len(hidden)} 个 {name}，共 {total:,.0f} ms）")

    for tid, group in sorted(by_trace.items(), key=lambda kv: min(x["start"] for x in kv[1])):
        ids = {s["span"] for s in group}
        kids: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
        for s in group:
            kids[s["parent"] if s["parent"] in ids else None].append(s)
        lines.append(f"=== trace {tid[:12]}（{len(group)} 个 span）")
        walk(kids, None, 0)
        lines.append("")
    return "\n".join(lines)
