"""假的 OpenAI 兼容接口：按 system 提示词判断是哪个模块在调用，返回预设的 JSON。"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

INTAKE_OK: dict[str, Any] = {
    "reported_version": "2.4.1",
    "environment": {"os": "Ubuntu 22.04", "python": "3.12.3", "node": None, "other": {}},
    "repro_steps": ["运行 issue 中的示例代码"],
    "expected": "返回 DataFrame",
    "actual": "抛出 KeyError",
    "missing": ["environment"],
    "language": "zh",
}
DEDUP_NONE: dict[str, Any] = {
    "judgements": [
        {"id": "c1", "score": 0.1, "reason": "不同问题", "quote_new": "", "quote_candidate": ""}
    ]
}
TRIAGE_OK: dict[str, Any] = {
    "type": "bug",
    "labels": ["bug", "not-a-real-label"],
    "priority": "P2",
    "rationale": "读取 parquet 时抛出 KeyError，属于行为错误。",
    "evidence_quotes": ["KeyError"],
    "confidence": 0.9,
    "slop_score": 0.0,
}


def completion(content: str, model: str = "deepseek-flash") -> dict[str, Any]:
    return {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 100, "prompt_cache_hit_tokens": 600},
    }


class FakeLLM:
    def __init__(self) -> None:
        self.replies: dict[str, list[Callable[[], httpx.Response]]] = {
            "intake": [], "triage": [], "dedup": [],
        }
        self.defaults = {"intake": INTAKE_OK, "triage": TRIAGE_OK, "dedup": DEDUP_NONE}
        self.requests: list[dict[str, Any]] = []

    def queue(self, skill: str, *responses: httpx.Response | dict[str, Any] | str) -> None:
        for r in responses:
            if isinstance(r, httpx.Response):
                self.replies[skill].append(lambda r=r: r)
            else:
                content = r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
                self.replies[skill].append(
                    lambda c=content: httpx.Response(200, json=completion(c))
                )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        system = body["messages"][0]["content"]
        if "Intake 模块" in system:
            skill = "intake"
        elif "Dedup（查重）模块" in system:
            skill = "dedup"
        else:
            skill = "triage"
        if self.replies[skill]:
            return self.replies[skill].pop(0)()
        content = json.dumps(self.defaults[skill], ensure_ascii=False)
        return httpx.Response(200, json=completion(content, body["model"]))

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)
