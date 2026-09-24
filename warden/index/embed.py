"""可选的向量通道：任意 OpenAI 兼容的 /embeddings 接口（例如 bge-m3、text-embedding-v4）。

DeepSeek 没有提供 embedding 接口，所以默认不启用；配置 EMBED_* 后，索引时和查询时都会计算向量，
在召回阶段多一路"语义相似"通道，用来捕捉换了说法的重复（"读不出来" vs "加载失败"）。
向量以 JSON 存在 SQLite 里，相似度用纯 Python 余弦计算；规模上来后换 pgvector 的 HNSW 索引。
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import httpx


class Embedder:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
            transport=transport,
        )

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        r = await self._http.post("/embeddings", json={"model": self.model, "input": list(texts)})
        r.raise_for_status()
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in data]

    async def aclose(self) -> None:
        await self._http.aclose()


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
