"""查重回放的指标、置信区间与阈值扫描。

记录（Record）里只存模型判断的原始事实（raw_score、引文是否找到、same_root_cause），
等级和结论都通过 skills.dedup.finalize 按阈值重算。所以换阈值不需要重新调用模型：
一次评测跑完，可以离线扫描任意多组阈值。

指标定义：
- 正样本（维护者确认的重复）：判为 duplicate 且目标在同一重复簇 → 判对；
  目标不在簇里但经人工复核确认也是同一根因 → 判对（另选目标）；其余目标 → 指错；否则漏判
- 对照样本（不在任何重复簇里的 issue）：判为 duplicate 记为"被标记"。很多重复从未被标注，
  所以按人工复核结果细分：确认是重复的算对，其余（确认不是、模棱两可、未复核）一律保守地算错
- 精确率 = 判对的 duplicate / 全部 duplicate 结论；召回率 = TP / 正样本数
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from failgate.skills.dedup import DedupCandidate, finalize

from .dataset import Labels


class JudgedCandidate(BaseModel):
    """模型对一个候选的判断，只保留重算阈值需要的字段（不含 issue 正文）。"""

    number: int
    title: str = ""
    raw_score: float
    has_quotes: bool
    quotes_found: bool
    same_root_cause: bool | None = None
    reason: str = ""

    def to_candidate(self) -> DedupCandidate:
        return DedupCandidate(
            number=self.number,
            title=self.title,
            state="",
            raw_score=self.raw_score,
            has_quotes=self.has_quotes,
            quotes_found=self.quotes_found,
            same_root_cause=self.same_root_cause,
            reason=self.reason,
            recall={},
        )


class Record(BaseModel):
    kind: Literal["pos", "neg"]
    issue: int
    title: str = ""
    # 正样本：同一重复簇里、在该 issue 之前创建的其他 issue（任何一个都算找对）
    gold: list[int] = Field(default_factory=list)
    # 正样本：同簇 issue 在召回结果中的最好名次（前 50 名以外为 None）
    recall_rank: int | None = None
    recalled: list[int] = Field(default_factory=list)
    judged: bool = False
    candidates: list[JudgedCandidate] = Field(default_factory=list)
    error: str | None = None
    cost_usd: float = 0.0
    cached: bool = False
    # 查重评委这一次调用（ADR 0026）：耗时、输出 token（含推理）、推理 token、花费
    latency_s: float | None = None
    out_tokens: int | None = None
    reasoning_tokens: int | None = None
    judge_cost_usd: float | None = None


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """二项比例的 Wilson 95% 置信区间。样本小、比例接近 0 或 1 时比正态近似可靠得多。"""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


@dataclass
class Metrics:
    high: float
    low: float
    gate: bool
    n_pos: int
    n_pos_recalled: int
    n_neg: int
    tp: int
    tp_alt: int
    wrong_target: int
    wrong_target_unreviewed: int
    missed: int
    surfaced: int
    neg_flagged: int
    neg_flagged_confirmed_dup: int
    neg_flagged_unreviewed: int
    precision: float
    precision_ci: tuple[float, float]
    recall: float
    recall_ci: tuple[float, float]
    recall_given_recalled: float
    surfaced_rate: float
    # 技术方案里的"查重 recall@5"：模型重新打分之后，正确的 issue 排在前 5 名以内的比例
    # （不看阈值，只看排序；分母是全部正样本，没被召回的也算没排进去）
    rerank_top5: int = 0
    rerank_top5_rate: float = 0.0
    rerank_top5_ci: tuple[float, float] = (0.0, 0.0)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def decide(
    rec: Record, high: float, low: float, gate: bool
) -> tuple[Literal["duplicate", "related", "none"], list[DedupCandidate]]:
    cands, verdict = finalize(
        [c.to_candidate() for c in rec.candidates], high=high, low=low, gate=gate
    )
    return verdict, cands


def evaluate(
    records: Sequence[Record],
    *,
    high: float,
    low: float,
    gate: bool = True,
    labels: Labels | None = None,
    recall_k: int = 8,
) -> Metrics:
    labels = labels or Labels()
    pos = [r for r in records if r.kind == "pos" and r.judged]
    neg = [r for r in records if r.kind == "neg" and r.judged]
    tp = tp_alt = wrong = wrong_unreviewed = missed = surfaced = top5 = 0
    for r in pos:
        verdict, cands = decide(r, high, low, gate)
        gold = set(r.gold)
        top5 += bool(gold & {c.number for c in cands[:5]})
        if verdict == "duplicate":
            if cands[0].number in gold:
                tp += 1
            else:
                # 标准答案不完整：目标不在簇里，也可能是同一根因的另一个 issue
                label = labels.get(r.issue, cands[0].number)
                if label == "duplicate":
                    tp_alt += 1
                else:
                    wrong += 1
                    wrong_unreviewed += label is None
        else:
            missed += 1
        # "露出"：正确的 issue 出现在评论里（作为可能重复或相关 issue，最多展示 3 个）
        shown = [c.number for c in cands if c.level in {"duplicate", "related"}][:3]
        surfaced += bool(gold & set(shown))
    flagged = confirmed = unreviewed = 0
    for r in neg:
        verdict, cands = decide(r, high, low, gate)
        if verdict != "duplicate":
            continue
        flagged += 1
        label = labels.get(r.issue, cands[0].number)
        if label == "duplicate":
            confirmed += 1
        elif label is None:
            unreviewed += 1
    hits = tp + tp_alt
    correct = hits + confirmed
    claimed = hits + wrong + flagged
    n_recalled = sum(1 for r in pos if r.recall_rank is not None and r.recall_rank <= recall_k)
    tp_recalled = 0
    for r in pos:
        if r.recall_rank is not None and r.recall_rank <= recall_k:
            verdict, cands = decide(r, high, low, gate)
            tp_recalled += verdict == "duplicate" and (
                cands[0].number in set(r.gold)
                or labels.get(r.issue, cands[0].number) == "duplicate"
            )
    return Metrics(
        high=high,
        low=low,
        gate=gate,
        n_pos=len(pos),
        n_pos_recalled=n_recalled,
        n_neg=len(neg),
        tp=tp,
        tp_alt=tp_alt,
        wrong_target=wrong,
        wrong_target_unreviewed=wrong_unreviewed,
        missed=missed,
        surfaced=surfaced,
        neg_flagged=flagged,
        neg_flagged_confirmed_dup=confirmed,
        neg_flagged_unreviewed=unreviewed,
        precision=correct / claimed if claimed else 0.0,
        precision_ci=wilson(correct, claimed),
        recall=hits / len(pos) if pos else 0.0,
        recall_ci=wilson(hits, len(pos)),
        recall_given_recalled=tp_recalled / n_recalled if n_recalled else 0.0,
        surfaced_rate=surfaced / len(pos) if pos else 0.0,
        rerank_top5=top5,
        rerank_top5_rate=top5 / len(pos) if pos else 0.0,
        rerank_top5_ci=wilson(top5, len(pos)),
    )


DEFAULT_HIGHS = tuple(round(0.6 + 0.05 * i, 2) for i in range(8))  # 0.60 … 0.95


def sweep(
    records: Sequence[Record],
    *,
    labels: Labels | None = None,
    highs: Iterable[float] = DEFAULT_HIGHS,
    gates: Iterable[bool] = (True, False),
    low: float = 0.5,
) -> list[Metrics]:
    return [
        evaluate(records, high=h, low=low, gate=g, labels=labels)
        for g in gates
        for h in highs
    ]


def recommend(results: Sequence[Metrics], *, min_precision: float = 0.9) -> Metrics | None:
    """在精确率 ≥ 目标值的组合里选召回最高的；召回相同时选更保守（阈值更高、开启闸门）的。"""
    ok = [m for m in results if m.precision >= min_precision and m.tp + m.tp_alt > 0]
    if not ok:
        return None
    return max(ok, key=lambda m: (m.recall, m.high, m.gate))
