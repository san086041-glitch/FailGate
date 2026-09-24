"""IssueIndex：维护查重语料，并做多路召回 + RRF 融合。

召回通道（每路各自排序，互不比较分数）：
- lexical  BM25（标题权重 ×2 + 正文前 4000 字符；去掉 HTML 注释，即模板里的填写说明）
- trace    报错堆栈签名相似度（只有双方都有堆栈时才参与）
- semantic 向量余弦相似度（配置了 embedding 接口才启用）

融合用 RRF（Reciprocal Rank Fusion）：score(d) = Σ_c 1 / (k + rank_c(d))，k = 60。
不同通道的原始分数量纲完全不同（BM25 无上界、相似度在 0–1），直接加权求和需要先归一化，
而且权重很难调；RRF 只用名次，天然免归一化，对某一路的异常高分也不敏感。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from warden.db import Database, IssueDoc
from warden.skills.intake import extract_traceback

from .bm25 import BM25
from .embed import Embedder, cosine
from .text import boilerplate_lines, strip_boilerplate, tokenize
from .trace import TraceSignature, signature, similarity

log = logging.getLogger(__name__)

MAX_BODY_CHARS = 4000
CHANNEL_DEPTH = 50  # 每路最多取前 50 名参与融合
MIN_TRACE_SIM = 0.3


@dataclass
class Recalled:
    number: int
    title: str
    body: str
    state: str
    state_reason: str | None
    labels: list[str]
    url: str | None
    rrf: float
    ranks: dict[str, int] = field(default_factory=dict)
    channel_scores: dict[str, float] = field(default_factory=dict)


def _naive_utc(dt: datetime) -> datetime:
    """SQLite 读回来的时间不带时区（按 UTC 存储），调用方传入的可能带时区。
    Python 不允许直接比较两者，统一成无时区的 UTC 再比较。"""
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


def doc_tokens(title: str, body: str, boilerplate: frozenset[str] = frozenset()) -> list[str]:
    t = tokenize(title)
    return t + t + tokenize(strip_boilerplate(body, boilerplate)[:MAX_BODY_CHARS])


def rrf(rankings: Mapping[str, Sequence[int]], k: int = 60) -> dict[int, float]:
    """rankings：通道名 → 按相关性排好序的文档下标列表。"""
    fused: dict[int, float] = {}
    for ranked in rankings.values():
        for rank, idx in enumerate(ranked, start=1):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (k + rank)
    return fused


@dataclass
class _LexicalIndex:
    boilerplate: frozenset[str]
    bm25: BM25


class IssueIndex:
    def __init__(
        self,
        db: Database,
        embedder: Embedder | None = None,
        rrf_k: int = 60,
        *,
        strip_template_lines: bool = False,
    ) -> None:
        self.db = db
        self.embedder = embedder
        self.rrf_k = rrf_k
        # 是否额外去掉从语料里学出的模板固定行。在 psf/black 的 218 个真实重复对上实测：
        # 只去 HTML 注释 recall@8 = 94/218，再去模板行反而降到 91/218。模板标题
        # （"Describe the bug" / "Describe the style change"）其实携带了 issue 类别信息，
        # 所以默认关闭，保留开关供其他仓库实验
        self.strip_template_lines = strip_template_lines
        # 语料版本 → 已建好的词法索引。分词（及可选的模板行学习）占一次查询的大头
        # （2800 个 issue 约 440ms），语料不变时直接复用；有 issue 新增或修改时版本号变化，自动重建
        self._lexical_cache: dict[tuple[int, ...], _LexicalIndex] = {}

    async def upsert(
        self,
        s: AsyncSession,
        repo_id: int,
        *,
        number: int,
        title: str,
        body: str,
        state: str = "open",
        state_reason: str | None = None,
        labels: Sequence[str] = (),
        url: str | None = None,
        created_at: datetime | None = None,
    ) -> IssueDoc:
        doc = await s.scalar(
            select(IssueDoc).where(IssueDoc.repo_id == repo_id, IssueDoc.number == number)
        )
        if doc is None:
            doc = IssueDoc(repo_id=repo_id, number=number)
            if created_at is not None:
                doc.created_at = created_at
            s.add(doc)
        if (doc.title, doc.body) != (title, body):
            doc.embedding = None  # 内容变了，向量需要重算
        doc.title, doc.body, doc.state = title, body, state
        doc.state_reason, doc.labels, doc.url = state_reason, list(labels), url
        sig = signature(extract_traceback(body))
        doc.trace_sig = sig.model_dump() if sig else None
        return doc

    async def search(
        self,
        repo_id: int,
        *,
        title: str,
        body: str,
        trace: TraceSignature | None,
        exclude_number: int,
        before: datetime | None,
        k: int,
    ) -> list[Recalled]:
        async with self.db.session() as s:
            q = select(IssueDoc).where(IssueDoc.repo_id == repo_id).order_by(IssueDoc.id)
            docs = list((await s.scalars(q)).all())
        # 候选可见性：排除自己；只和"当时已经存在"的 issue 比较（回放评测时防止用到未来的 issue）。
        # 索引本身建在全量语料上并缓存，过滤只作用在候选上
        cutoff = _naive_utc(before) if before is not None else None
        visible = [
            d.number != exclude_number and (cutoff is None or _naive_utc(d.created_at) < cutoff)
            for d in docs
        ]
        if not any(visible):
            return []

        def masked(values: list[float]) -> list[float]:
            return [v if ok else 0.0 for v, ok in zip(values, visible, strict=True)]

        rankings: dict[str, list[int]] = {}
        scores: dict[str, dict[int, float]] = {}

        lex = self._lexical(repo_id, docs)
        lexical = lex.bm25.scores(doc_tokens(title, body, lex.boilerplate))
        self._add_channel("lexical", masked(lexical), rankings, scores, min_score=1e-9)

        if trace is not None:
            sims = [
                similarity(trace, TraceSignature(**d.trace_sig) if d.trace_sig else None)
                for d in docs
            ]
            self._add_channel("trace", masked(sims), rankings, scores, min_score=MIN_TRACE_SIM)

        if self.embedder is not None:
            try:
                vectors = await self._embeddings(docs)
                (qv,) = await self.embedder.embed([f"{title}\n{body[:MAX_BODY_CHARS]}"])
                cos = [cosine(qv, v) if v else 0.0 for v in vectors]
                self._add_channel("semantic", masked(cos), rankings, scores, min_score=1e-9)
            except Exception:
                # 向量通道是增强项，失败时降级为其余通道，不影响查重
                log.exception("semantic channel failed; falling back to lexical/trace")

        fused = rrf(rankings, self.rrf_k)
        top = sorted(fused, key=lambda i: fused[i], reverse=True)[:k]
        return [
            Recalled(
                number=docs[i].number,
                title=docs[i].title,
                body=docs[i].body,
                state=docs[i].state,
                state_reason=docs[i].state_reason,
                labels=list(docs[i].labels or []),
                url=docs[i].url,
                rrf=round(fused[i], 6),
                ranks={c: r.index(i) + 1 for c, r in rankings.items() if i in r},
                channel_scores={c: round(sc[i], 4) for c, sc in scores.items() if i in sc},
            )
            for i in top
        ]

    def _lexical(self, repo_id: int, docs: list[IssueDoc]) -> _LexicalIndex:
        # 语料版本：文档数 + id 之和 + 最后更新时间；有 issue 新增或修改就会变。
        # 取舍：IDF 等统计量来自全量语料，回放时会包含"未来 issue"的词频统计
        # （候选本身仍严格按时间过滤）。这点泄漏只影响词的权重，换来的是整轮回放只需建一次索引
        newest = max(d.updated_at for d in docs).timestamp()
        key = (repo_id, len(docs), sum(d.id for d in docs), int(newest * 1e6))
        cached = self._lexical_cache.get(key)
        if cached is None:
            bp = (
                boilerplate_lines([d.body for d in docs])
                if self.strip_template_lines
                else frozenset()
            )
            cached = _LexicalIndex(bp, BM25([doc_tokens(d.title, d.body, bp) for d in docs]))
            if len(self._lexical_cache) >= 8:  # 只保留少量版本，避免回放时内存增长
                self._lexical_cache.pop(next(iter(self._lexical_cache)))
            self._lexical_cache[key] = cached
        return cached

    @staticmethod
    def _add_channel(
        name: str,
        values: list[float],
        rankings: dict[str, list[int]],
        scores: dict[str, dict[int, float]],
        *,
        min_score: float,
    ) -> None:
        ranked = sorted(
            (i for i, v in enumerate(values) if v >= min_score), key=lambda i: -values[i]
        )[:CHANNEL_DEPTH]
        if ranked:
            rankings[name] = ranked
            scores[name] = {i: values[i] for i in ranked}

    async def _embeddings(self, docs: list[IssueDoc]) -> list[list[float] | None]:
        assert self.embedder is not None
        missing = [d for d in docs if not d.embedding]
        for start in range(0, len(missing), 64):
            batch = missing[start : start + 64]
            vecs = await self.embedder.embed(
                [f"{d.title}\n{d.body[:MAX_BODY_CHARS]}" for d in batch]
            )
            async with self.db.session() as s, s.begin():
                for d, v in zip(batch, vecs, strict=True):
                    d.embedding = v
                    await s.merge(d)
        return [d.embedding for d in docs]

