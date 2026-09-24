import json
from datetime import UTC, datetime

import httpx
import pytest
from fake_llm import completion
from pydantic import BaseModel

from warden.llm import LLMClient, LLMError, Usage
from warden.llm import client as client_mod
from warden.llm.pricing import cost_usd, is_deepseek_peak


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    async def instant(_):
        return None

    monkeypatch.setattr(client_mod.asyncio, "sleep", instant)


def make_client(*responses: httpx.Response) -> tuple[LLMClient, list[dict]]:
    queue = list(responses)
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return queue.pop(0)

    return LLMClient("http://llm.test", "k", transport=httpx.MockTransport(handler)), seen


class Answer(BaseModel):
    n: int


async def test_retries_on_429_then_succeeds():
    llm, seen = make_client(
        httpx.Response(429, text="slow down"),
        httpx.Response(200, json=completion("hi")),
    )
    r = await llm.chat([{"role": "user", "content": "x"}], model="m")
    assert r.text == "hi" and r.attempts == 2 and len(seen) == 2


async def test_does_not_retry_on_400():
    llm, seen = make_client(httpx.Response(400, text="bad request"))
    with pytest.raises(LLMError, match="HTTP 400"):
        await llm.chat([{"role": "user", "content": "x"}], model="m")
    assert len(seen) == 1


async def test_json_mode_repairs_invalid_output():
    llm, seen = make_client(
        httpx.Response(200, json=completion("not json")),
        httpx.Response(200, json=completion('{"n": "three"}')),
        httpx.Response(200, json=completion('{"n": 3}')),
    )
    obj, usage, _ = await llm.complete_json(
        [{"role": "user", "content": "x"}], Answer, model="m"
    )
    assert obj.n == 3
    assert usage.prompt_tokens == 3000  # 三次调用的用量累加
    assert seen[0]["response_format"] == {"type": "json_object"}
    # 修复轮次会把上一次的错误反馈给模型
    assert "不符合要求" in seen[1]["messages"][-1]["content"]


async def test_json_mode_gives_up_after_repairs():
    llm, _ = make_client(*[httpx.Response(200, json=completion("nope")) for _ in range(3)])
    with pytest.raises(LLMError, match="Answer"):
        await llm.complete_json([{"role": "user", "content": "x"}], Answer, model="m")


def test_usage_parses_openai_and_deepseek_formats():
    assert Usage.from_api({"prompt_tokens": 10, "prompt_cache_hit_tokens": 4}).cached_tokens == 4
    openai = {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 7}}
    assert Usage.from_api(openai).cached_tokens == 7
    assert Usage.from_api(None) == Usage()


def test_deepseek_peak_hours():
    assert is_deepseek_peak(datetime(2026, 9, 24, 2, 30, tzinfo=UTC))  # 周四 UTC 02:30
    assert not is_deepseek_peak(datetime(2026, 9, 24, 5, 0, tzinfo=UTC))
    assert not is_deepseek_peak(datetime(2026, 9, 26, 2, 30, tzinfo=UTC))  # 周六


def test_cost_counts_cache_and_off_peak_discount():
    usage = Usage(prompt_tokens=1_000_000, completion_tokens=1_000_000, cached_tokens=500_000)
    peak = datetime(2026, 9, 24, 2, 0, tzinfo=UTC)
    off = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    # 0.5M×0.006 + 0.5M×0.30 + 1M×1.20 = 0.003 + 0.15 + 1.2
    assert cost_usd("deepseek-flash", usage, peak) == pytest.approx(1.353)
    assert cost_usd("deepseek-flash", usage, off) == pytest.approx(1.353 / 2)
    assert cost_usd("unknown-model", usage, peak) == 0.0


def test_parse_json_object_tolerates_fences_and_trailing_text():
    from warden.llm.client import parse_json_object

    assert parse_json_object('```json\n{"n": 1}\n```') == {"n": 1}
    # 回放评测中真实遇到的情况：完整 JSON 之后还有内容
    assert parse_json_object('{"n": 1}\n{"n": 2}') == {"n": 1}
    with pytest.raises(json.JSONDecodeError):
        parse_json_object("not json at all")
