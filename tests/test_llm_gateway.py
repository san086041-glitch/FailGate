"""多模型网关（ADR 0034）。

模型名解析、按厂商分发、思考参数、LiteLLM 后端、key 不外泄。不发真实请求。
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

from failgate.llm import LLMClient, LLMError, Usage
from failgate.llm.gateway import (
    LiteLLMClient,
    LLMGateway,
    ProviderConfig,
    bare_model,
    key_env,
    parse_providers,
    parse_spec,
)
from failgate.llm.pricing import cost_usd

SECRET = "sk-very-secret-value"


def test_parse_spec_and_bare_model():
    s = parse_spec("deepseek-flash")
    assert (s.provider, s.model) == ("deepseek", "deepseek-flash") and str(s) == "deepseek-flash"
    s = parse_spec("SiliconFlow:Qwen/Qwen3-32B")
    assert (s.provider, s.model) == ("siliconflow", "Qwen/Qwen3-32B")
    assert str(s) == "siliconflow:Qwen/Qwen3-32B"
    assert bare_model("siliconflow:Qwen/Qwen3-32B") == "Qwen/Qwen3-32B"
    assert bare_model("deepseek-flash") == "deepseek-flash"
    for bad in ["", "  ", ":x", "x:"]:
        with pytest.raises(ValueError):
            parse_spec(bad)


def test_parse_providers_validation():
    assert parse_providers("") == {}
    got = parse_providers(json.dumps({
        "SiliconFlow": {"base_url": "https://api.siliconflow.cn/v1", "api_key_env": "SF_KEY"},
        "anthropic": {"api_key_env": "ANTHROPIC_KEY", "backend": "litellm"},
    }))
    assert set(got) == {"siliconflow", "anthropic"}
    assert got["siliconflow"].backend == "openai" and got["siliconflow"].thinking is False
    with pytest.raises(ValueError, match="base_url"):
        parse_providers({"x": {"api_key_env": "K"}})  # openai 后端没写地址
    with pytest.raises(ValueError, match="配置不对"):
        parse_providers({"x": {"base_url": "https://a", "api_key_env": "K", "backend": "grpc"}})
    with pytest.raises(ValueError, match="JSON 对象"):
        parse_providers("[1, 2]")


def _ok(model: str) -> httpx.Response:
    return httpx.Response(200, json={
        "model": model, "choices": [{"message": {"content": "{\"x\": 1}"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2}})


def make_gateway(**providers: ProviderConfig) -> tuple[LLMGateway, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok(json.loads(request.content)["model"])

    transport = httpx.MockTransport(handler)
    default = LLMClient("https://api.deepseek.com", "k-default", transport=transport)
    env = {"SF_KEY": SECRET, "EMPTY_KEY": ""}
    gw = LLMGateway(default, providers, env=env, transport=transport)
    return gw, seen


SF = ProviderConfig(base_url="https://api.siliconflow.cn/v1", api_key_env="SF_KEY")


async def test_routes_by_prefix_and_keeps_default_untouched():
    gw, seen = make_gateway(siliconflow=SF)
    r1 = await gw.chat([{"role": "user", "content": "hi"}], model="deepseek-flash",
                       thinking="high")
    r2 = await gw.chat([{"role": "user", "content": "hi"}], model="siliconflow:Qwen/Qwen3-32B",
                       thinking="high")
    a, b = seen
    assert a.url.host == "api.deepseek.com" and a.headers["authorization"] == "Bearer k-default"
    body_a = json.loads(a.content)
    assert body_a["model"] == "deepseek-flash" and body_a["thinking"] == {"type": "enabled"}
    assert b.url.host == "api.siliconflow.cn" and b.headers["authorization"] == f"Bearer {SECRET}"
    body_b = json.loads(b.content)
    assert body_b["model"] == "Qwen/Qwen3-32B"
    assert "thinking" not in body_b and "reasoning_effort" not in body_b  # 别家不传思考参数
    assert r1.model == "deepseek-flash" and r2.model == "Qwen/Qwen3-32B"
    assert gw.providers == ["deepseek", "siliconflow"]


async def test_complete_json_goes_through_the_gateway():
    class Out(BaseModel):
        x: int

    gw, seen = make_gateway(siliconflow=SF)
    obj, usage, _ = await gw.complete_json([{"role": "user", "content": "x"}], Out,
                                           model="siliconflow:Qwen/Qwen3-32B")
    assert obj.x == 1 and usage.prompt_tokens == 10 and seen[0].url.host == "api.siliconflow.cn"


async def test_unknown_provider_and_missing_key_never_leak_secrets():
    gw, _ = make_gateway(siliconflow=SF, empty=ProviderConfig(
        base_url="https://e.example/v1", api_key_env="EMPTY_KEY"))
    with pytest.raises(LLMError, match="没有配置厂商 'openai'"):
        await gw.chat([], model="openai:gpt-x")
    with pytest.raises(LLMError, match="EMPTY_KEY") as e:
        await gw.chat([], model="empty:m")
    assert SECRET not in str(e.value)


async def test_provider_error_message_has_no_key():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    transport = httpx.MockTransport(handler)
    gw = LLMGateway(LLMClient("https://api.deepseek.com", "k", transport=transport),
                    {"siliconflow": SF}, env={"SF_KEY": SECRET}, transport=transport)
    with pytest.raises(LLMError) as e:
        await gw.chat([], model="siliconflow:m")
    assert "401" in str(e.value) and SECRET not in str(e.value)


async def test_litellm_backend_with_a_fake_module(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict[str, Any]] = []

    class Resp:
        def model_dump(self) -> dict[str, Any]:
            return {"model": "claude-x", "usage": {"prompt_tokens": 5, "completion_tokens": 1},
                    "choices": [{"message": {"content": "", "tool_calls": [
                        {"id": "t1", "function": {"name": "submit", "arguments": "{}"}}]}}]}

    async def acompletion(**kw: Any) -> Resp:
        calls.append(kw)
        return Resp()

    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(acompletion=acompletion))
    gw = LLMGateway(LLMClient("https://api.deepseek.com", "k"),
                    {"anthropic": ProviderConfig(api_key_env="A_KEY", backend="litellm")},
                    env={"A_KEY": SECRET})
    resp = await gw.chat([{"role": "user", "content": "hi"}], model="anthropic:claude-x",
                         tools=[{"type": "function", "function": {"name": "submit"}}],
                         thinking="high")
    assert isinstance(gw.client_for("anthropic"), LiteLLMClient)
    kw = calls[0]
    assert kw["model"] == "anthropic/claude-x" and kw["api_key"] == SECRET
    assert kw["tools"][0]["function"]["name"] == "submit" and "thinking" not in kw
    assert resp.tool_calls[0].name == "submit" and resp.usage.prompt_tokens == 5


async def test_litellm_missing_is_a_clear_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "litellm", None)  # import litellm → ImportError
    client = LiteLLMClient("anthropic", SECRET)
    with pytest.raises(LLMError, match="没有安装 litellm"):
        await client.chat([], model="claude-x")


def test_pricing_ignores_the_provider_prefix():
    u = Usage(prompt_tokens=1_000_000, completion_tokens=0)
    assert cost_usd("deepseek:deepseek-flash", u) == cost_usd("deepseek-flash", u) > 0
    assert cost_usd("siliconflow:unknown-model", u) == 0.0


def test_key_env_reads_dotenv_and_env_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    f = tmp_path / ".env"
    f.write_text("SF_KEY=from-file\nOTHER=1\n", encoding="utf-8")
    monkeypatch.setenv("SF_KEY", "from-env")
    env = key_env(f)
    assert env["SF_KEY"] == "from-env" and env["OTHER"] == "1"
    assert "OTHER" not in key_env(tmp_path / "missing.env")


def test_build_llm_wraps_only_when_providers_are_configured():
    from failgate.app import build_llm
    from failgate.settings import Settings

    plain = build_llm(Settings(_env_file=None, llm_api_key="k"))  # type: ignore[call-arg]
    assert type(plain) is LLMClient
    gw = build_llm(Settings(_env_file=None, llm_api_key="k",  # type: ignore[call-arg]
                            llm_providers=json.dumps({"siliconflow": SF.model_dump()})))
    assert isinstance(gw, LLMGateway)


def test_cli_routes_prints_no_secrets_and_flags_unknown_providers(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from failgate.cli import app

    monkeypatch.chdir(tmp_path)  # 不读仓库里真实的 .env
    monkeypatch.setenv("LLM_API_KEY", SECRET)
    monkeypatch.setenv("SF_KEY", SECRET)
    monkeypatch.setenv("LLM_PROVIDERS", json.dumps({"siliconflow": SF.model_dump()}))
    monkeypatch.setenv("LLM_MODEL_JUDGE", "siliconflow:Qwen/Qwen3-32B")
    res = CliRunner().invoke(app, ["llm", "routes"])
    assert res.exit_code == 0, res.output
    assert "siliconflow / Qwen/Qwen3-32B" in res.output and "key：有" in res.output
    assert SECRET not in res.output
    monkeypatch.setenv("LLM_MODEL_JUDGE", "openai:gpt-x")
    res = CliRunner().invoke(app, ["llm", "routes"])
    assert res.exit_code == 1 and "没有配置这个厂商" in res.output
