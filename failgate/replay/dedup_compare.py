"""查重评委的思考模式对比（ADR 0026）：同一批样本、同一套标准答案，并排比较质量和开销。

链路显示查重评委为一段 JSON 结论输出几千个 token（deepseek-flash 默认开思考、强度 high），
占提问类事件 75%–89% 的花费。要不要关掉思考，不能只看省了多少钱，要看判断质量有没有变差：

- 质量：精确率、端到端召回率、模型重排后前 5 命中率（和 ADR 0003 / 0007 同一套指标）；
- 开销：每次评委调用的耗时、输出 token（含推理）、推理 token、花费；
- 成对：以第一次运行为基准，同一个样本换一种模式后，"判对 → 判错"和"判错 → 判对"各多少
  （只数不一致的样本，用精确二项检验看是不是碰巧）。LLM 本身有波动，同一模式重跑也会有少量
  不一致，所以只看方向和幅度，不把一两个样本的差别当结论。
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .dataset import Labels
from .dedup import RunResult
from .metrics import Metrics, Record, decide, evaluate


@dataclass
class JudgeStats:
    n: int
    latency_p50: float | None
    latency_p95: float | None
    out_tokens_mean: float | None
    reasoning_mean: float | None
    cost_mean: float | None
    cost_total: float


def _pct(xs: Sequence[float], q: float) -> float:
    s = sorted(xs)
    return s[max(0, math.ceil(q * len(s)) - 1)]


def judge_stats(records: Sequence[Record]) -> JudgeStats:
    """只统计带开销记录的评委调用（2026-09-30 之后的缓存才有这几项）。"""
    rs = [r for r in records if r.judged and r.out_tokens is not None]
    lat = [r.latency_s for r in rs if r.latency_s is not None]
    cost = [r.judge_cost_usd for r in rs if r.judge_cost_usd is not None]
    return JudgeStats(
        n=len(rs),
        latency_p50=statistics.median(lat) if lat else None,
        latency_p95=_pct(lat, 0.95) if lat else None,
        out_tokens_mean=statistics.mean(r.out_tokens or 0 for r in rs) if rs else None,
        reasoning_mean=statistics.mean(r.reasoning_tokens or 0 for r in rs) if rs else None,
        cost_mean=statistics.mean(cost) if cost else None,
        cost_total=sum(cost),
    )


def correct(rec: Record, high: float, low: float, labels: Labels) -> bool:
    """这个样本判得对不对：正样本 = 判为重复且目标对（含复核确认的同根因）；对照 = 没被判为重复。"""
    verdict, cands = decide(rec, high, low, True)
    if rec.kind == "neg":
        return verdict != "duplicate" or labels.get(rec.issue, cands[0].number) == "duplicate"
    if verdict != "duplicate":
        return False
    target = cands[0].number
    return target in set(rec.gold) or labels.get(rec.issue, target) == "duplicate"


def binom_two_sided(k: int, n: int) -> float:
    """精确二项检验（p = 0.5）的双侧 p 值：n 个不一致里有 k 个偏向一边。"""
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(0, min(k, n - k) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


@dataclass
class Paired:
    common: int
    same_decision: int
    lost: int      # 基准判对、这一轮判错
    gained: int    # 基准判错、这一轮判对
    p_value: float


def paired(base: RunResult, other: RunResult, labels: Labels) -> Paired:
    b = {r.issue: r for r in base.records if r.judged}
    o = {r.issue: r for r in other.records if r.judged}
    common = sorted(set(b) & set(o))
    hb, lb = base.config.high, base.config.low
    ho, lo = other.config.high, other.config.low
    same = lost = gained = 0
    for n in common:
        vb, cb = decide(b[n], hb, lb, True)
        vo, co = decide(o[n], ho, lo, True)
        same += (vb, cb[0].number if vb == "duplicate" else None) == (
            vo, co[0].number if vo == "duplicate" else None)
        ok_b, ok_o = correct(b[n], hb, lb, labels), correct(o[n], ho, lo, labels)
        lost += ok_b and not ok_o
        gained += ok_o and not ok_b
    return Paired(common=len(common), same_decision=same, lost=lost, gained=gained,
                  p_value=binom_two_sided(min(lost, gained), lost + gained))


def _f(v: float | None, fmt: str) -> str:
    return "—" if v is None else format(v, fmt)


def _ci(m: Metrics, name: str) -> str:
    value = getattr(m, name if name != "rerank_top5" else "rerank_top5_rate")
    lo, hi = getattr(m, f"{name}_ci")
    return f"{value:.1%} [{lo:.0%}, {hi:.0%}]"


def render(runs: Sequence[tuple[str, RunResult]], labels: Labels,
           notes: Sequence[str] = ()) -> str:
    """runs：[(标签, 运行结果)]，第一个是成对比较的基准。"""
    base_label, base = runs[0]
    cfg = base.config
    lines = [f"# 查重评委的思考模式对比：{cfg.repo}", "",
             f"- 样本：正样本 {cfg.positives}、对照 {cfg.negatives}，seed={cfg.seed}，"
             f"提示词 v{cfg.prompt_version}，模型 `{cfg.model}`，"
             f"阈值 high={cfg.high}、low={cfg.low}",
             "- 每种模式各跑一次，Intake 结果共用同一份缓存（只变评委这一个变量）", ""]
    lines += list(notes) + ([""] if notes else [])
    lines += ["## 质量", "",
              "| 模式 | 精确率 | 端到端召回率 | 重排后前 5 命中 | 露出 | 对照被标记 |",
              "|---|---|---|---|---|---|"]
    metrics: dict[str, Metrics] = {}
    for label, run in runs:
        m = evaluate(run.records, high=run.config.high, low=run.config.low, gate=True,
                     labels=labels, recall_k=run.config.recall_k)
        metrics[label] = m
        lines.append(f"| {label} | {_ci(m, 'precision')} | {_ci(m, 'recall')} | "
                     f"{_ci(m, 'rerank_top5')} | {m.surfaced_rate:.1%} | "
                     f"{m.neg_flagged}/{m.n_neg} |")
    lines += ["", "## 开销（每次评委调用）", "",
              "| 模式 | 调用数 | 耗时 p50 / p95 | 输出 token | 其中推理 | 花费均值 | 花费合计 |",
              "|---|---|---|---|---|---|---|"]
    for label, run in runs:
        s = judge_stats(run.records)
        lines.append(f"| {label} | {s.n} | {_f(s.latency_p50, '.1f')} / "
                     f"{_f(s.latency_p95, '.1f')} s | {_f(s.out_tokens_mean, ',.0f')} | "
                     f"{_f(s.reasoning_mean, ',.0f')} | ${_f(s.cost_mean, '.5f')} | "
                     f"${s.cost_total:.4f} |")
    if len(runs) > 1:
        lines += ["", f"## 成对比较（以「{base_label}」为基准，同一批样本）", "",
                  "| 模式 | 共同样本 | 结论完全相同 | 判对 → 判错 | 判错 → 判对 | 精确二项检验 p |",
                  "|---|---|---|---|---|---|"]
        for label, run in runs[1:]:
            pr = paired(base, run, labels)
            lines.append(f"| {label} | {pr.common} | {pr.same_decision} | {pr.lost} | "
                         f"{pr.gained} | {pr.p_value:.2f} |")
        lines += ["", "LLM 有波动：同一模式重跑也会有少量样本结论不同，所以这里只看方向和幅度。"]
    return "\n".join(lines) + "\n"


def cascade(cheap: RunResult, expensive: RunResult, labels: Labels,
            thresholds: Sequence[float] = (0.5, 0.6, 0.7)) -> str:
    """离线模拟级联评委：便宜的评委先判，最高分 ≥ t 的再交给贵的（两次运行都已按样本缓存）。

    花费和耗时 = 便宜评委的 + 升级样本上贵评委的。"""
    c = {r.issue: r for r in cheap.records if r.judged and r.out_tokens is not None}
    e = {r.issue: r for r in expensive.records if r.judged and r.out_tokens is not None}
    common = sorted(set(c) & set(e))
    cfg = expensive.config

    def row(name: str, recs: list[Record], cost: list[float], lat: list[float]) -> str:
        m = evaluate(recs, high=cfg.high, low=cfg.low, gate=True, labels=labels,
                     recall_k=cfg.recall_k)
        return (f"| {name} | {m.precision:.1%} | {m.recall:.1%} | {m.neg_flagged}/{m.n_neg} | "
                f"${statistics.mean(cost):.5f} | {statistics.mean(lat):.1f} s |")

    def top(r: Record) -> float:
        return max((x.raw_score for x in r.candidates), default=0.0)

    lines = [f"## 级联模拟（共同样本 {len(common)}，不重新调用模型）", "",
             "| 方案 | 精确率 | 端到端召回率 | 对照被标记 | 平均花费 | 平均耗时 |",
             "|---|---|---|---|---|---|",
             row("全部用贵的", [e[n] for n in common],
                 [e[n].judge_cost_usd or 0 for n in common], [e[n].latency_s or 0 for n in common]),
             row("全部用便宜的", [c[n] for n in common],
                 [c[n].judge_cost_usd or 0 for n in common], [c[n].latency_s or 0 for n in common])]
    for t in thresholds:
        recs, cost, lat, up = [], [], [], 0
        for n in common:
            escalate = top(c[n]) >= t
            up += escalate
            recs.append(e[n] if escalate else c[n])
            extra_cost = (e[n].judge_cost_usd or 0) if escalate else 0
            extra_lat = (e[n].latency_s or 0) if escalate else 0
            cost.append((c[n].judge_cost_usd or 0) + extra_cost)
            lat.append((c[n].latency_s or 0) + extra_lat)
        lines.append(row(f"级联 t={t}（升级 {up}）", recs, cost, lat))
    return "\n".join(lines) + "\n"


def summary(runs: Sequence[tuple[str, RunResult]], labels: Labels) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for label, run in runs:
        m = evaluate(run.records, high=run.config.high, low=run.config.low, gate=True,
                     labels=labels, recall_k=run.config.recall_k)
        out[label] = {"precision": m.precision, "recall": m.recall,
                      "rerank_top5": m.rerank_top5_rate, "judge": judge_stats(run.records).__dict__}
    return out
