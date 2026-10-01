"""多模型网关（W10，ADR 0034）：一个和 LLMClient 同样接口的路由器，按模型名把请求分给不同厂商。

各模块照旧调用 ``llm.chat(messages, model=...)``，只是 model 可以写成 ``厂商:模型``：

    deepseek-flash                      → 默认厂商（.env 里 LLM_BASE_URL / LLM_API_KEY）
    siliconflow:Qwen/Qwen3-32B          → LLM_PROVIDERS 里的 siliconflow（OpenAI 兼容）
    anthropic:claude-sonnet-5-5         → backend=litellm 的厂商，交给 LiteLLM 适配原生接口

不带前缀时和网关出现之前逐字节一样（同一个 LLMClient），回放缓存和旧报告不受影响。
厂商配置里只写 API key 所在的环境变量名，不写 key 本身；报错和日志里也不出现 key。

两种后端：
- openai：复用 LLMClient（httpx 直连 chat/completions），覆盖 DeepSeek、硅基流动、OpenAI、
  通义 / Kimi / 智谱的兼容模式、OpenRouter 等绝大多数厂商；
- litellm：原生接口不兼容的厂商（LiteLLMClient）。litellm 是可选依赖、不默认安装：
  它依赖多，2026-03 还出过 PyPI 投毒；真要用时锁版本 + 校验哈希再装（ADR 0034）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

from .client import LLMClient, LLMError, LLMResponse, ToolCall, Usage

DEFAULT_PROVIDER = "deepseek"
Backend = Literal["openai", "litellm"]


class ProviderConfig(BaseModel):
    """一个厂商。api_key_env 是环境变量名（不是 key）。"""

    base_url: str | None = None
    api_key_env: str
    backend: Backend = "openai"
    # litellm 的模型前缀（anthropic、gemini ……）；为空时用厂商名
    litellm_prefix: str | None = None
    # 是否接受 DeepSeek 风格的思考参数（thinking / reasoning_effort）；别家收到可能报 400
    thinking: bool = False
    timeout: float | None = None


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str

    def __str__(self) -> str:
        return self.model if self.provider == DEFAULT_PROVIDER else f"{self.provider}:{self.model}"


def parse_spec(spec: str) -> ModelSpec:
    """``厂商:模型`` → ModelSpec；没有冒号的是默认厂商。模型名里可以有斜杠（Qwen/Qwen3-32B）。"""
    spec = spec.strip()
    if not spec:
        raise ValueError("模型名为空")
    if ":" in spec:
        provider, model = spec.split(":", 1)
        if not provider or not model:
            raise ValueError(f"模型名格式应为 厂商:模型：{spec!r}")
        return ModelSpec(provider.strip().lower(), model.strip())
    return ModelSpec(DEFAULT_PROVIDER, spec)


def bare_model(spec: str) -> str:
    """计价、记录用的模型名（去掉厂商前缀）。"""
    return spec.split(":", 1)[1] if ":" in spec else spec


def key_env(env_file: str | os.PathLike[str] | None = ".env") -> dict[str, str]:
    """查各厂商 key 用的环境：.env 文件 + 进程环境变量（后者优先）。

    pydantic-settings 只把认识的字段读进 Settings，厂商的 key 是任意名字，所以单独读一遍 .env。"""
    from dotenv import dotenv_values

    out: dict[str, str] = {}
    if env_file and Path(env_file).is_file():
        out.update({k: v for k, v in dotenv_values(env_file).items() if v is not None})
    out.update(os.environ)
    return out


def parse_providers(raw: str | Mapping[str, Any] | None) -> dict[str, ProviderConfig]:
    """LLM_PROVIDERS：JSON 对象 {名字: {base_url, api_key_env, backend, ...}}。"""
    if not raw:
        return {}
    data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    if not isinstance(data, dict):
        raise ValueError("LLM_PROVIDERS 必须是 JSON 对象")
    out: dict[str, ProviderConfig] = {}
    for name, cfg in data.items():
        try:
            pc = ProviderConfig.model_validate(cfg)
        except ValidationError as e:
            raise ValueError(f"LLM_PROVIDERS.{name} 配置不对：{e}") from e
        if pc.backend == "openai" and not pc.base_url:
            raise ValueError(f"LLM_PROVIDERS.{name}：openai 后端要写 base_url")
        out[str(name).lower()] = pc
    return out


class LiteLLMClient(LLMClient):
    """原生接口不兼容的厂商：同样的 chat 接口（链路、计价、重试都在父类），请求交给 litellm。"""

    def __init__(self, prefix: str, api_key: str, *, base_url: str | None = None,
                 timeout: float = 60.0, max_retries: int = 3,
                 capture_content: bool = False) -> None:
        super().__init__(base_url or "https://litellm.invalid", api_key, timeout=timeout,
                         max_retries=max_retries, capture_content=capture_content)
        self._prefix = prefix
        self._api_key = api_key
        self._api_base = base_url
        self._timeout = timeout
        self._provider = prefix

    async def _chat(self, body: dict[str, Any], model: str) -> LLMResponse:
        try:
            import litellm  # type: ignore[import-not-found]
        except ImportError as e:  # 可选依赖，用到才要求装
            raise LLMError("这个厂商配置了 backend=litellm，但没有安装 litellm（见 ADR 0034："
                           "锁版本、校验哈希后安装）") from e
        import time

        started = time.monotonic()
        kwargs: dict[str, Any] = {k: v for k, v in body.items() if k != "model"}
        try:
            resp = await litellm.acompletion(
                model=f"{self._prefix}/{model}", api_key=self._api_key,
                api_base=self._api_base, timeout=self._timeout,
                num_retries=self._max_retries, **kwargs)
        except Exception as e:  # litellm 的异常类型很多，统一成 LLMError（不带 key）
            raise LLMError(f"LLM 调用失败（{self._prefix}:{model}）：{type(e).__name__}: "
                           f"{str(e)[:300]}") from e
        data = resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)
        message = data["choices"][0]["message"]
        return LLMResponse(
            text=message.get("content") or "",
            model=data.get("model") or model,
            usage=Usage.from_api(data.get("usage")),
            latency_s=time.monotonic() - started,
            raw=data,
            tool_calls=[
                ToolCall(id=tc.get("id") or f"call_{i}", name=tc["function"]["name"],
                         arguments=tc["function"].get("arguments") or "{}")
                for i, tc in enumerate(message.get("tool_calls") or [])
                if (tc.get("function") or {}).get("name")
            ],
        )


class LLMGateway(LLMClient):
    """按模型名分发到各厂商的 LLMClient。对外接口和 LLMClient 一样，所以各模块不用改。"""

    def __init__(self, default: LLMClient, providers: Mapping[str, ProviderConfig] | None = None,
                 *, env: Mapping[str, str] | None = None, timeout: float = 60.0,
                 capture_content: bool = False,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        # 不调父类 __init__：网关自己不发请求，只持有各厂商的客户端
        self._default = default
        self._transport = transport  # 测试用：替换 openai 后端的网络层
        self._providers = dict(providers or {})
        self._env = env if env is not None else os.environ
        self._timeout = timeout
        self._capture_content = capture_content
        self._clients: dict[str, LLMClient] = {DEFAULT_PROVIDER: default}

    @property
    def providers(self) -> list[str]:
        return [DEFAULT_PROVIDER, *sorted(p for p in self._providers if p != DEFAULT_PROVIDER)]

    def client_for(self, provider: str) -> LLMClient:
        if provider in self._clients:
            return self._clients[provider]
        cfg = self._providers.get(provider)
        if cfg is None:
            raise LLMError(f"没有配置厂商 {provider!r}（LLM_PROVIDERS 里有：{self.providers}）")
        key = self._env.get(cfg.api_key_env, "")
        if not key:
            raise LLMError(f"厂商 {provider} 的 API key 环境变量 {cfg.api_key_env} 是空的")
        timeout = cfg.timeout or self._timeout
        client: LLMClient
        if cfg.backend == "litellm":
            client = LiteLLMClient(cfg.litellm_prefix or provider, key, base_url=cfg.base_url,
                                   timeout=timeout, capture_content=self._capture_content)
        else:
            assert cfg.base_url is not None
            client = LLMClient(cfg.base_url, key, timeout=timeout, transport=self._transport,
                               capture_content=self._capture_content)
            client._provider = provider
        client._supports_thinking = cfg.thinking
        self._clients[provider] = client
        return client

    def resolve(self, spec: str) -> tuple[LLMClient, str]:
        s = parse_spec(spec)
        return self.client_for(s.provider), s.model

    async def chat(self, messages: list[dict[str, Any]], *, model: str,
                   **kwargs: Any) -> LLMResponse:
        client, bare = self.resolve(model)
        return await client.chat(messages, model=bare, **kwargs)

    async def aclose(self) -> None:
        for c in self._clients.values():
            await c.aclose()


class Routes(BaseModel):
    """各角色用哪个模型（`failgate llm routes` 打印的就是这个）。"""

    small: str
    large: str
    judge: str
    providers: list[str] = Field(default_factory=list)
