"""L2 回放：在已修复的历史 bug 上，Agent 于 issue 创建时的提交写仓库内的失败测试，
再用严格 FB/PA（fbpa.py）检验这个测试能不能当修复的验收标准。

    issue 创建时的提交 ──Agent──→ L2 测试（在这份代码上失败，和报告一致）
                                     │
               修复提交的父提交 ─────┼──→ 应失败（同一个失败）
               修复提交 ─────────────┘──→ 应通过
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from typing import Any

from failgate.replay.fbpa import FbpaCase, note_for
from failgate.replay.metrics import wilson
from failgate.repro.issue import L2IssueReport


def outcome(r: L2IssueReport) -> str:
    """每个 issue 归到一个互斥的类别。"""
    if r.source.level.value == "L2":
        return "l2"
    a = r.agent
    if a is None:
        return "setup_failed"  # 源码、环境、预检没过
    if a.status == "gave_up":
        return "gave_up"
    return "not_reproduced"


OUTCOME_LABELS = {
    "l2": "L2：写出了在 issue 时的代码上失败、且和报告一致的测试",
    "not_reproduced": "没复现（提交用完 / 步数或预算用完）",
    "gave_up": "Agent 放弃",
    "setup_failed": "源码环境或 pytest 预检没过",
}


def summarize(reports: Sequence[L2IssueReport], cases: Sequence[FbpaCase]) -> dict[str, Any]:
    n = len(reports)
    counts = Counter(outcome(r) for r in reports)
    k = counts["l2"]
    agents = [r.agent for r in reports if r.agent is not None]
    fb = Counter(c.outcome for c in cases)
    with_fix = [c for c in cases if c.outcome not in ("no_fix", "no_script")]
    return {
        "n": n,
        "outcomes": dict(counts),
        "l2": k,
        "l2_rate": k / n if n else 0.0,
        "l2_ci": wilson(k, n),
        "with_traceback": sum(r.has_traceback for r in reports),
        "l2_with_traceback": sum(r.has_traceback and outcome(r) == "l2" for r in reports),
        # 严格 FB/PA：分母是"写出了 L2 测试、且有修复提交"的
        "fbpa_eligible": len(with_fix),
        "fb_pa": fb["fb_pa"],
        "fb_pa_ci": wilson(fb["fb_pa"], len(with_fix)),
        "fbpa_outcomes": dict(fb),
        # 端到端：所有样本里最终得到"能当修复验收标准的测试"的比例。没有修复提交的也算在
        # 分母里（验证不了就不算成功），是保守的口径
        "end_to_end_n": n,
        "total_cost_usd": round(sum(r.total_cost_usd for r in reports), 4),
        "mean_cost_usd": round(sum(r.total_cost_usd for r in reports) / n, 4) if n else 0.0,
        "mean_steps": round(sum(a.steps for a in agents) / len(agents), 1) if agents else 0.0,
        "mean_submits": round(sum(len(a.attempts) for a in agents) / len(agents), 2)
        if agents else 0.0,
        "mean_duration_s": round(sum(a.duration_s for a in agents) / len(agents), 1)
        if agents else 0.0,
    }


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def render(
    repo: str, reports: Sequence[L2IssueReport], cases: Sequence[FbpaCase], meta: dict[str, Any]
) -> str:
    s = summarize(reports, cases)
    lo, hi = s["l2_ci"]
    flo, fhi = s["fb_pa_ci"]
    e2e_n = s["end_to_end_n"]
    lines = [
        f"# {repo} L2 回放（仓库内的失败测试）",
        "",
        f"- 时间：{meta['started']}；模型：{meta['model']}；提示词 repro_test_v{meta['prompt']}",
        f"- 选样：{meta['selection']}",
        f"- 上限：每个 issue {meta['max_steps']} 步、{meta['max_attempts']} 次提交、"
        f"${meta['budget_usd']}",
        "- 做法：Agent 在 **issue 创建时** 默认分支上的提交里写测试；写出 L2 之后，"
        "把测试放到修复提交的父提交和修复提交上各跑 2 次（同一个 Python、同一个 pytest）。",
        "",
        "## 汇总",
        "",
        "| 指标 | 结果 |",
        "|---|---|",
        f"| 样本数 | {s['n']}（有 Python 堆栈的 {s['with_traceback']} 个） |",
        f"| **L2 复现率** | **{s['l2']}/{s['n']} = {_pct(s['l2_rate'])}**"
        f"（95% Wilson 区间 {_pct(lo)}–{_pct(hi)}） |",
        f"| 有堆栈的 issue 中 L2 | {s['l2_with_traceback']}/{s['with_traceback']} |",
        f"| **L2 测试的严格 FB/PA** | **{s['fb_pa']}/{s['fbpa_eligible']}**"
        f"（{_pct(flo)}–{_pct(fhi)}；分母是写出了 L2 且有修复提交的） |",
        f"| 端到端（能当修复验收标准的测试） | {s['fb_pa']}/{e2e_n}（全部样本，保守口径） |",
        f"| 平均步数 / 提交次数 / 耗时 | {s['mean_steps']} / {s['mean_submits']} / "
        f"{s['mean_duration_s']} 秒 |",
        f"| 花费 | 共 ${s['total_cost_usd']}，平均 ${s['mean_cost_usd']} / issue |",
        "",
        "## 结果分布",
        "",
        "| 结果 | 数量 |",
        "|---|---|",
    ]
    for key, label in OUTCOME_LABELS.items():
        lines.append(f"| {label} | {s['outcomes'].get(key, 0)} |")
    by_number = {c.number: c for c in cases}
    lines += [
        "",
        "## 逐条",
        "",
        "| # | 标题 | issue 时的提交 | Python / pytest | 堆栈 | Agent | 提交 | 判定 "
        "| 严格 FB/PA | 花费 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in reports:
        a, src = r.agent, r.source
        title = r.title.replace("|", "\\|")[:50]
        sha = f"`{src.sha[:7]}`" if src.sha else "—"
        env = f"{src.python or '—'} / {(src.pytest or '—').removeprefix('pytest==')}"
        kind = src.run.verdict.kind if src.run else (
            a.attempts[-1].kind if a and a.attempts else "—"
        )
        c = by_number.get(r.number)
        lines.append(
            f"| {r.number} | {title} | {sha} | {env} | {'有' if r.has_traceback else '无'} | "
            f"{a.status if a else '—'} | {len(a.attempts) if a else 0} | {kind} | "
            f"{c.outcome if c else '—'} | ${r.total_cost_usd:.4f} |"
        )
    lines += ["", "## 没写出 L2 的原因", ""]
    for r in reports:
        if outcome(r) == "l2":
            continue
        a = r.agent
        why = (a.give_up_reason if a and a.give_up_reason else None) or r.source.error or (
            a.attempts[-1].reason if a and a.attempts else (a.status if a else "")
        )
        lines.append(f"- #{r.number}（{outcome(r)}）：{(why or '')[:300]}")
    notes = [c for c in cases if c.outcome not in ("fb_pa", "no_script")]
    if notes:
        lines += ["", "## L2 测试不是严格 FB/PA 的", ""]
        for c in notes:
            lines.append(f"- #{c.number}（{c.outcome}）：{note_for(c)[:200]}")
    return "\n".join(lines) + "\n"


def dump(reports: Sequence[L2IssueReport], cases: Sequence[FbpaCase], meta: dict[str, Any]) -> str:
    return json.dumps(
        {"meta": meta, "summary": summarize(reports, cases),
         "reports": [r.model_dump(mode="json") for r in reports],
         "fbpa": [c.model_dump(mode="json") for c in cases]},
        ensure_ascii=False, indent=2,
    )
