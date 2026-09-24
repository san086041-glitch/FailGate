"""OpenAI 兼容的 chat/completions 客户端（直接用 httpx，不依赖厂商 SDK）。

- 429 / 5xx / 网络错误按指数退避重试
- complete_json：JSON 模式 + Pydantic 校验，校验失败时把错误反馈给模型重试
- 用量统计兼容 OpenAI（prompt_tokens_details.cached_tokens）与 DeepSeek（prompt_cache_hit_tokens）
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
_RETRY_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


class LLMError(RuntimeError):
    pass


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def parse_json_object(text: str) -> Any:
    """解析模型输出的 JSON：容忍 ```json 代码围栏，以及 JSON 之后多余的文字。

    回放评测里遇到过模型在一个完整的 JSON 之后又输出了内容（json.loads 报 "Extra data"），
    连续修复两次仍然如此。取第一个完整的 JSON 对象，比整体判为失败更合理。
    """
    cleaned = _FENCE.sub("", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        if "Extra data" not in str(e):
            raise
        obj, _ = json.JSONDecoder().raw_decode(cleaned)
        return obj


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
            self.cached_tokens + other.cached_tokens,
        )

    @classmethod
    def from_api(cls, data: dict[str, Any] | None) -> Usage:
        data = data or {}
        cached = data.get("prompt_cache_hit_tokens")
        if cached is None:
            cached = (data.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        return cls(
            prompt_tokens=data.get("prompt_tokens", 0),
            completion_tokens=data.get("completion_tokens", 0),
            cached_tokens=cached or 0,
        )


@dataclass
class LLMResponse:
    text: str
    model: str
    usage: Usage
    latency_s: float
    attempts: int = 1
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 60.0,
        max_retries: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )
        self._max_retries = max_retries

    async def aclose(self) -> None:
        await self._http.aclose()

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        temperature: float = 0.0,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        body: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if max_tokens:
            body["max_tokens"] = max_tokens

        started = time.monotonic()
        last_error = ""
        for attempt in range(1, self._max_retries + 1):
            try:
                r = await self._http.post("/chat/completions", json=body)
            except httpx.TransportError as e:
                last_error = f"{type(e).__name__}: {e}"
            else:
                if r.status_code == 200:
                    data = r.json()
                    return LLMResponse(
                        text=data["choices"][0]["message"].get("content") or "",
                        model=data.get("model", model),
                        usage=Usage.from_api(data.get("usage")),
                        latency_s=time.monotonic() - started,
                        attempts=attempt,
                        raw=data,
                    )
                last_error = f"HTTP {r.status_code}: {r.text[:300]}"
                if r.status_code not in _RETRY_STATUS:
                    break
            if attempt < self._max_retries:
                await asyncio.sleep(min(2 ** (attempt - 1), 8))
        raise LLMError(f"LLM 调用失败（{model}）：{last_error}")

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        schema: type[T],
        *,
        model: str,
        repair_attempts: int = 2,
        **kwargs: Any,
    ) -> tuple[T, Usage, LLMResponse]:
        """返回 (校验后的对象, 累计用量, 最后一次响应)。"""
        msgs = list(messages)
        total = Usage()
        for attempt in range(repair_attempts + 1):
            resp = await self.chat(msgs, model=model, json_mode=True, **kwargs)
            total = total + resp.usage
            try:
                return schema.model_validate(parse_json_object(resp.text)), total, resp
            except (json.JSONDecodeError, ValidationError) as e:
                if attempt == repair_attempts:
                    raise LLMError(f"模型输出不符合 {schema.__name__}：{e}") from e
                log.info("结构化输出校验失败，第 %d 次修复", attempt + 1)
                msgs += [
                    {"role": "assistant", "content": resp.text},
                    {
                        "role": "user",
                        "content": f"上面的输出不符合要求：{e}\n请只输出修正后的完整 JSON。",
                    },
                ]
        raise AssertionError("unreachable")
