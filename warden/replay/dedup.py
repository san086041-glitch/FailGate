"""查重回放评测。

1. 召回评测（全部可用配对，不调用模型）：同簇 issue 在召回结果中的名次 → recall@k
2. 判断评测（抽样，调用模型）：正样本 + 对照样本，各自跑 Intake + Dedup，记录模型的原始判断
3. 指标与阈值扫描在 metrics.py 里离线完成，不需要再调用模型

时间切片：每个被评测的 issue 只能看到在它之前创建的 issue（IssueIndex.search 的 before 参数）。
模型输出按 (模块, 版本, 模型, issue, 内容/语料指纹) 缓存到 eval/cache/，中断后重跑不重复花钱。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from warden.db import Database, IssueDoc, Repo
from warden.index.store import IssueIndex
from warden.index.trace import signature
from warden.llm import LLMClient
from warden.skills.base import IssueSnapshot, SkillContext
from warden.skills.dedup import DedupOutput, DedupSkill
from warden.skills.intake import IntakeOutput, IntakeSkill, extract_traceback

from .dataset import GoldSet, repo_slug
from .metrics import JudgedCandidate, Record

RECALL_DEPTH = 50


class RunConfig(BaseModel):
    repo: str
    positives: int = 100
    negatives: int = 50
    seed: int = 42
    prompt_version: str = "2"
    model: str = "deepseek-flash"
    recall_k: int = 8
    high: float = 0.85
    low: float = 0.5
    judge: bool = True
    # 对照样本至少要有这么多更早的 issue 可比较，否则"没找到重复"没有意义
    min_history: int = 50
    concurrency: int = 6


class RecallSummary(BaseModel):
    usable_pairs: int
    hits: dict[int, int]
    with_traceback: int


class RunResult(BaseModel):
    config: RunConfig
    started_at: datetime
    finished_at: datetime | None = None
    corpus_size: int
    corpus_fingerprint: str
    gold_pairs: int
    recall: RecallSummary
    records: list[Record] = Field(default_factory=list)
    cost_usd: float = 0.0
    model_calls: int = 0
    cached_calls: int = 0


def corpus_fingerprint(docs: list[IssueDoc]) -> str:
    h = hashlib.sha256()
    for d in sorted(docs, key=lambda d: d.number):
        h.update(f"{d.number}:{d.updated_at.isoformat()}\n".encode())
    return h.hexdigest()[:12]


class SkillCache:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, *parts: str) -> Path:
        key = hashlib.sha256("|".join(parts).encode()).hexdigest()[:24]
        return self.root / parts[0] / f"{key}.json"

    def get(self, *parts: str) -> dict[str, Any] | None:
        p = self._path(*parts)
        return json.loads(p.read_text("utf-8")) if p.exists() else None

    def put(self, value: dict[str, Any], *parts: str) -> None:
        p = self._path(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value, ensure_ascii=False), "utf-8")


def _content_hash(d: IssueDoc) -> str:
    return hashlib.sha256(f"{d.title}\n{d.body}".encode()).hexdigest()[:16]


async def run_dedup_replay(
    db: Database,
    llm: LLMClient | None,
    gold: GoldSet,
    cfg: RunConfig,
    *,
    cache_root: Path,
    progress: Callable[[str], None] = print,
) -> RunResult:
    async with db.session() as s:
        repo = await s.scalar(select(Repo).where(Repo.full_name == cfg.repo))
        if repo is None:
            raise RuntimeError(f"数据库里没有 {cfg.repo}，请先运行 warden index build")
        docs = list((await s.scalars(select(IssueDoc).where(IssueDoc.repo_id == repo.id))).all())
    by_num = {d.number: d for d in docs}
    clusters = gold.clusters()
    index = IssueIndex(db)
    fingerprint = corpus_fingerprint(docs)
    result = RunResult(
        config=cfg,
        started_at=datetime.now(UTC),
        corpus_size=len(docs),
        corpus_fingerprint=fingerprint,
        gold_pairs=len(gold.pairs),
        recall=RecallSummary(usable_pairs=0, hits={}, with_traceback=0),
    )

    # ---------- 正样本：同簇里有更早 issue 的重复 ----------
    positives: list[tuple[IssueDoc, list[int]]] = []
    for pair in gold.pairs:
        d = by_num.get(pair.duplicate)
        if d is None:
            continue
        earlier = sorted(
            n for n in clusters.members(d.number)
            if n != d.number and n in by_num and by_num[n].created_at < d.created_at
        )
        if earlier:
            positives.append((d, earlier))

    # ---------- 召回评测：全部正样本，不调用模型 ----------
    ranks: dict[int, int | None] = {}
    recalled_lists: dict[int, list[int]] = {}
    for d, earlier in positives:
        res = await index.search(
            repo.id, title=d.title, body=d.body, trace=signature(extract_traceback(d.body)),
            exclude_number=d.number, before=d.created_at, k=RECALL_DEPTH,
        )
        numbers = [r.number for r in res]
        target = set(earlier)
        ranks[d.number] = next((i + 1 for i, n in enumerate(numbers) if n in target), None)
        recalled_lists[d.number] = numbers[: cfg.recall_k]
    result.recall = RecallSummary(
        usable_pairs=len(positives),
        hits={
            k: sum(1 for r in ranks.values() if r is not None and r <= k)
            for k in (1, 3, 5, 8, 20, 50)
        },
        with_traceback=sum(1 for d, _ in positives if extract_traceback(d.body)),
    )
    progress(f"召回评测：{len(positives)} 个正样本，recall@{cfg.recall_k} = "
             f"{result.recall.hits[8] if cfg.recall_k == 8 else '…'}")

    rng = random.Random(cfg.seed)
    pos_sample = rng.sample(positives, min(cfg.positives, len(positives)))
    records = [
        Record(kind="pos", issue=d.number, title=d.title[:100], gold=earlier,
               recall_rank=ranks[d.number], recalled=recalled_lists[d.number])
        for d, earlier in pos_sample
    ]

    # ---------- 对照样本：不在任何重复簇里、没有被标成 duplicate、且有足够历史 ----------
    ordered = sorted(docs, key=lambda d: d.created_at)
    pool = [
        d for i, d in enumerate(ordered)
        if i >= cfg.min_history
        and d.number not in clusters
        and d.state_reason != "duplicate"
        and "duplicate" not in {lb.lower() for lb in (d.labels or [])}
    ]
    for d in rng.sample(pool, min(cfg.negatives, len(pool))):
        records.append(Record(kind="neg", issue=d.number, title=d.title[:100]))

    if not cfg.judge or llm is None:
        result.records = records
        result.finished_at = datetime.now(UTC)
        return result

    # ---------- 判断评测：Intake + Dedup，调用模型（带缓存） ----------
    cache = SkillCache(cache_root / repo_slug(cfg.repo))
    intake_skill = IntakeSkill()
    dedup_skill = DedupSkill(
        recall_k=cfg.recall_k, high=cfg.high, low=cfg.low, prompt_version=cfg.prompt_version
    )
    sem = asyncio.Semaphore(cfg.concurrency)
    done = 0

    async def judge(rec: Record) -> None:
        nonlocal done
        d = by_num[rec.issue]
        ctx = SkillContext(
            issue=IssueSnapshot(repo=cfg.repo, number=d.number, title=d.title, body=d.body,
                                repo_id=repo.id, created_at=d.created_at),
            llm=llm, model=cfg.model, retriever=index,
        )
        async with sem:
            try:
                ikey = ("intake", intake_skill.version, cfg.model, str(d.number), _content_hash(d))
                hit = cache.get(*ikey)
                if hit is None:
                    r = await intake_skill.run(ctx)
                    hit = {"output": r.output.model_dump(mode="json"), "cost": r.cost_usd}
                    cache.put(hit, *ikey)
                    result.model_calls += 1
                    rec.cost_usd += r.cost_usd
                else:
                    result.cached_calls += 1
                ctx.prior["intake"] = IntakeOutput.model_validate(hit["output"]).model_dump(
                    mode="json"
                )
                ctx.prior["triage"] = {"type": "bug"}
                dkey = ("dedup", dedup_skill.version, cfg.model, str(d.number),
                        _content_hash(d), fingerprint, str(cfg.recall_k))
                hit = cache.get(*dkey)
                if hit is None:
                    r = await dedup_skill.run(ctx)
                    hit = {"output": r.output.model_dump(mode="json"), "cost": r.cost_usd}
                    cache.put(hit, *dkey)
                    result.model_calls += 1
                    rec.cost_usd += r.cost_usd
                else:
                    result.cached_calls += 1
                    rec.cached = True
                out = DedupOutput.model_validate(hit["output"])
                rec.candidates = [
                    JudgedCandidate(
                        number=c.number, title=c.title[:100], raw_score=c.raw_score,
                        has_quotes=c.has_quotes, quotes_found=c.quotes_found,
                        same_root_cause=c.same_root_cause, reason=c.reason[:200],
                    )
                    for c in out.candidates
                ]
                rec.judged = True
            except Exception as e:  # 单个样本失败不影响整轮评测
                rec.error = f"{type(e).__name__}: {e}"[:300]
            done += 1
            if done % 10 == 0 or done == len(records):
                progress(f"  判断评测进度 {done}/{len(records)}")

    await asyncio.gather(*(judge(r) for r in records))
    result.records = records
    result.cost_usd = round(sum(r.cost_usd for r in records), 4)
    result.finished_at = datetime.now(UTC)
    return result
