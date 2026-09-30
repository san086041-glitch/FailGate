"""可选的向量通道：任意 OpenAI 兼容的 /embeddings 接口（例如 bge-m3、text-embedding-v4）。

DeepSeek 没有提供 embedding 接口，所以默认不启用；配置 EMBED_* 后，索引时和查询时都会计算向量，
在召回阶段多一路"语义相似"通道，用来捕捉换了说法的重复（"读不出来" vs "加载失败"）。
向量以 JSON 存在 SQLite 里，相似度用纯 Python 余弦计算；规模上来后换 pgvector 的 HNSW 索引。
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Sequence

import httpx
from opentelemetry.trace import SpanKind

from failgate import tracing


class Embedder:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        retries: int = 4,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.model = model
        self.retries = retries
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
            transport=transport,
        )

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        # 向量调用也是一次模型调用：GenAI 语义约定里 operation 是 embeddings（ADR 0025）
        with tracing.tracer.start_as_current_span(
            f"embeddings {self.model}", kind=SpanKind.CLIENT,
            attributes={"gen_ai.operation.name": "embeddings", "gen_ai.request.model": self.model,
                        "failgate.embed.inputs": len(texts)},
        ):
            return await self._embed(texts)

    async def _embed(self, texts: Sequence[str]) -> list[list[float]]:
        # 批量建索引时容易碰到服务方的每分钟请求数 / token 数上限：429 和 5xx 退避重试
        for attempt in range(self.retries + 1):
            r = await self._http.post(
                "/embeddings", json={"model": self.model, "input": list(texts)}
            )
            if r.status_code in {429, 500, 502, 503, 504} and attempt < self.retries:
                wait = float(r.headers.get("retry-after", 2 ** (attempt + 1)))
                await self._sleep(min(wait, 60.0))
                continue
            r.raise_for_status()
            data = sorted(r.json()["data"], key=lambda d: d["index"])
            return [d["embedding"] for d in data]
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        await self._http.aclose()


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
