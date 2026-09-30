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
from typing import Any, Literal, TypeVar

import httpx
from opentelemetry.trace import SpanKind, StatusCode
from pydantic import BaseModel, ValidationError

from failgate import tracing

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
    # 推理模型的思考 token：已经算在 completion_tokens 里（按输出计费），单独记一份看占比
    reasoning_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
            self.cached_tokens + other.cached_tokens,
            self.reasoning_tokens + other.reasoning_tokens,
        )

    @classmethod
    def from_api(cls, data: dict[str, Any] | None) -> Usage:
        data = data or {}
        cached = data.get("prompt_cache_hit_tokens")
        if cached is None:
            cached = (data.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        reasoning = (data.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
        return cls(
            prompt_tokens=data.get("prompt_tokens", 0),
            completion_tokens=data.get("completion_tokens", 0),
            cached_tokens=cached or 0,
            reasoning_tokens=reasoning or 0,
        )


# 思考模式（DeepSeek：默认开启、强度 high）。None = 不传参数，用服务方的默认
Thinking = Literal["disabled", "low", "high", "max"]


def thinking_params(thinking: str | None) -> dict[str, Any]:
    """思考模式 → 请求体里的字段。disabled 关掉思考；low / high / max 是开启时的强度。"""
    if not thinking:
        return {}
    if thinking == "disabled":
        return {"thinking": {"type": "disabled"}}
    if thinking in ("low", "high", "max"):
        return {"thinking": {"type": "enabled"}, "reasoning_effort": thinking}
    raise ValueError(f"未知的思考模式：{thinking}（可选 disabled / low / high / max）")


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # 模型给出的原始 JSON 字符串，由调用方解析和校验

    def as_message(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass
class LLMResponse:
    text: str
    model: str
    usage: Usage
    latency_s: float
    attempts: int = 1
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 60.0,
        max_retries: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
        capture_content: bool = False,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )
        self._max_retries = max_retries
        # 链路里记不记 prompt 和回答（ADR 0025）；供应商名只用来标 span
        self._capture_content = capture_content
        self._provider = "deepseek" if "deepseek" in base_url else "openai"

    async def aclose(self) -> None:
        await self._http.aclose()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        temperature: float = 0.0,
        json_mode: bool = False,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        thinking: str | None = None,
    ) -> LLMResponse:
        """tools：OpenAI 格式的函数定义。模型要调用工具时，结果在 LLMResponse.tool_calls。

        thinking：推理模型的思考模式（disabled / low / high / max），None 用服务方默认。"""
        body: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if max_tokens:
            body["max_tokens"] = max_tokens
        if tools:
            body["tools"] = tools
        body.update(thinking_params(thinking))

        # OTel GenAI 语义约定：Langfuse 按这些属性把 span 识别成一次模型调用（generation）
        with tracing.tracer.start_as_current_span(
            f"chat {model}", kind=SpanKind.CLIENT,
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.system": self._provider,
                "gen_ai.provider.name": self._provider,
                "gen_ai.request.model": model,
                "gen_ai.request.temperature": temperature,
                "failgate.llm.thinking": thinking or "default",
                **({"gen_ai.request.max_tokens": max_tokens} if max_tokens else {}),
            },
        ) as span:
            # 记不记内容：全局开关，或者这条 trace 所属的仓库在白名单里（Baggage 带过来的标记）
            capture = self._capture_content or tracing.capturing()
            if capture:
                span.set_attribute("langfuse.observation.input", tracing.content(messages))
            try:
                resp = await self._chat(body, model)
            except LLMError as e:
                span.record_exception(e)
                span.set_status(StatusCode.ERROR)
                raise
            from .pricing import cost_usd  # pricing 反过来依赖本模块的 Usage

            cost = cost_usd(resp.model, resp.usage)
            span.set_attributes({
                "gen_ai.response.model": resp.model,
                "gen_ai.usage.input_tokens": resp.usage.prompt_tokens,
                "gen_ai.usage.output_tokens": resp.usage.completion_tokens,
                "gen_ai.usage.cache_read.input_tokens": resp.usage.cached_tokens,
                "gen_ai.usage.reasoning.output_tokens": resp.usage.reasoning_tokens,
                "failgate.cost_usd": cost,
                "langfuse.observation.cost_details": json.dumps({"total": cost}),
                "failgate.llm.attempts": resp.attempts,
                "failgate.llm.tool_calls": len(resp.tool_calls),
            })
            if capture:
                out = resp.text or [tc.as_message() for tc in resp.tool_calls]
                span.set_attribute("langfuse.observation.output", tracing.content(out))
            return resp

    async def _chat(self, body: dict[str, Any], model: str) -> LLMResponse:
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
                    message = data["choices"][0]["message"]
                    return LLMResponse(
                        text=message.get("content") or "",
                        model=data.get("model", model),
                        usage=Usage.from_api(data.get("usage")),
                        latency_s=time.monotonic() - started,
                        attempts=attempt,
                        raw=data,
                        tool_calls=[
                            ToolCall(
                                id=tc.get("id") or f"call_{i}",
                                name=tc["function"]["name"],
                                arguments=tc["function"].get("arguments") or "{}",
                            )
                            for i, tc in enumerate(message.get("tool_calls") or [])
                            if tc.get("function", {}).get("name")
                        ],
                    )
                last_error = f"HTTP {r.status_code}: {r.text[:300]}"
                if r.status_code not in _RETRY_STATUS:
                    break
            if attempt < self._max_retries:
                await asyncio.sleep(min(2 ** (attempt - 1), 8))
        raise LLMError(f"LLM 调用失败（{model}）：{last_error}")

    async def complete_json(
        self,
        messages: list[dict[str, Any]],
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
