"""复现回放评测：在仓库历史上已经修复的 bug 上跑复现 Agent（技术方案第 18 节）。

选样规则是固定的（select_issues + 仓库的 selection.json，跑之前定好、和数据集一起
提交），避免挑样本。

指标：
- L1 复现率：在报告的版本上复现，并且失败特征和报告一致；
- FB/PA（代理指标）：在报告的版本上复现（fail before），同一个脚本在最新正式版上不再失败
  （pass after）。技术方案里的 FB/PA 用的是修复 PR 的前后提交；这里用"报告版本 vs 最新版"
  代替，前提是这些 issue 都已经以"完成"状态关闭。
- "最新版仍复现"：对已修复的 bug 本应很少出现。出现了，要么 bug 没修干净，要么脚本抓到的
  并不是这个 bug，需要人工看。
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from failgate.replay.metrics import wilson
from failgate.replay.selection import Selection, closed_since
from failgate.repro.issue import IssueReproReport


def select_issues(
    docs: Iterable[Any], *, rule: Selection, since: datetime, limit: int, offset: int = 0
) -> list[Any]:
    """已完成关闭、符合仓库选题规则（rule.is_repro）的 bug；按编号从新到旧，
    跳过前 offset 个（开发集），取 limit 个。"""
    out = []
    skipped = 0
    for d in sorted(docs, key=lambda d: d.number, reverse=True):
        if not closed_since(d, since) or not rule.is_repro(d.labels or []):
            continue
        if skipped < offset:
            skipped += 1
            continue
        out.append(d)
        if len(out) >= limit:
            break
    return out


def outcome(r: IssueReproReport) -> str:
    """每个 issue 归到一个互斥的结果类别。"""
    if r.repro.error and r.agent is None:
        return "setup_failed"  # 版本解析不了、环境装不上
    if r.repro.level.value == "L1":
        fixed = r.repro.fixed_in_latest
        if fixed is True:
            return "fb_pa"
        if fixed is False:
            return "still_fails_latest"
        return "l1_latest_unknown"
    if r.agent is not None and r.agent.status == "gave_up":
        return "gave_up"
    return "not_reproduced"


OUTCOME_LABELS = {
    "fb_pa": "L1 复现 + 最新版不再失败（FB/PA）",
    "still_fails_latest": "L1 复现，但最新版仍失败",
    "l1_latest_unknown": "L1 复现，最新版无法判断",
    "not_reproduced": "没复现（提交用完 / 步数或预算用完）",
    "gave_up": "Agent 放弃",
    "setup_failed": "环境没搭起来（版本 / 安装）",
}


def summarize(reports: Sequence[IssueReproReport]) -> dict[str, Any]:
    n = len(reports)
    counts = Counter(outcome(r) for r in reports)
    l1 = sum(counts[k] for k in ("fb_pa", "still_fails_latest", "l1_latest_unknown"))
    agents = [r.agent for r in reports if r.agent is not None]
    return {
        "n": n,
        "outcomes": dict(counts),
        "l1": l1,
        "l1_rate": l1 / n if n else 0.0,
        "l1_ci": wilson(l1, n),
        "fb_pa": counts["fb_pa"],
        "fb_pa_rate": counts["fb_pa"] / n if n else 0.0,
        "fb_pa_ci": wilson(counts["fb_pa"], n),
        "total_cost_usd": round(sum(r.total_cost_usd for r in reports), 4),
        "mean_cost_usd": round(sum(r.total_cost_usd for r in reports) / n, 4) if n else 0.0,
        "mean_steps": round(sum(a.steps for a in agents) / len(agents), 1) if agents else 0.0,
        "mean_submits": round(sum(len(a.attempts) for a in agents) / len(agents), 2)
        if agents else 0.0,
        "mean_duration_s": round(sum(a.duration_s for a in agents) / len(agents), 1)
        if agents else 0.0,
        "with_traceback": sum(r.has_traceback for r in reports),
        "substituted": sum(r.repro.substituted_for is not None for r in reports),
        "l1_substituted": sum(
            r.repro.substituted_for is not None and r.repro.level.value == "L1" for r in reports
        ),
        "l1_with_traceback": sum(
            r.has_traceback and r.repro.level.value == "L1" for r in reports
        ),
    }


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def render(repo: str, reports: Sequence[IssueReproReport], meta: dict[str, Any]) -> str:
    s = summarize(reports)
    lo, hi = s["l1_ci"]
    flo, fhi = s["fb_pa_ci"]
    lines = [
        f"# {repo} 复现回放评测",
        "",
        f"- 时间：{meta['started']}；模型：{meta['model']}；提示词 repro_agent_v{meta['prompt']}",
        f"- 选样：{meta['selection']}",
        f"- 上限：每个 issue {meta['max_steps']} 步、{meta['max_attempts']} 次提交、"
        f"${meta['budget_usd']}",
        "",
        "## 汇总",
        "",
        "| 指标 | 结果 |",
        "|---|---|",
        f"| 样本数 | {s['n']}（有 Python 堆栈的 {s['with_traceback']} 个） |",
        f"| L1 复现率 | {s['l1']}/{s['n']} = {_pct(s['l1_rate'])}"
        f"（95% Wilson 区间 {_pct(lo)}–{_pct(hi)}） |",
        f"| FB/PA（代理：报告版本失败、最新版通过） | {s['fb_pa']}/{s['n']} = "
        f"{_pct(s['fb_pa_rate'])}（{_pct(flo)}–{_pct(fhi)}） |",
        f"| 有堆栈的 issue 中 L1 | {s['l1_with_traceback']}/{s['with_traceback']} |",
        f"| 报告的是未发布版本、改用替代正式版的 | {s['substituted']} 个，其中 L1 "
        f"{s['l1_substituted']} 个 |",
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
    lines += [
        "",
        "## 逐条",
        "",
        "| # | 标题 | 版本 | 堆栈 | Agent | 提交 | 报告版本判定 | 最新版 | 花费 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in reports:
        a = r.agent
        rep = r.repro.reported.verdict.kind if r.repro.reported else (r.repro.error or "")[:40]
        lat = r.repro.latest.verdict.kind if r.repro.latest else "—"
        title = r.title.replace("|", "\\|")[:60]
        ver = r.repro.reported_version or r.intake_version
        if r.repro.substituted_for:
            ver = f"{ver}（替代 {r.repro.substituted_for[:24]}）"
        lines.append(
            f"| {r.number} | {title} | {ver} | "
            f"{'有' if r.has_traceback else '无'} | {a.status if a else '—'} | "
            f"{len(a.attempts) if a else 0} | {rep} | {lat} | ${r.total_cost_usd:.4f} |"
        )
    lines += ["", "## 失败原因（Agent 没复现的）", ""]
    for r in reports:
        a = r.agent
        if outcome(r) in ("not_reproduced", "gave_up", "setup_failed"):
            why = (a.give_up_reason if a and a.give_up_reason else None) or r.repro.error or (
                a.attempts[-1].reason if a and a.attempts else (a.status if a else "")
            )
            lines.append(f"- #{r.number}（{outcome(r)}）：{why}")
    return "\n".join(lines) + "\n"


def dump(reports: Sequence[IssueReproReport], meta: dict[str, Any]) -> str:
    return json.dumps(
        {"meta": meta, "summary": summarize(reports),
         "reports": [r.model_dump(mode="json") for r in reports]},
        ensure_ascii=False, indent=2,
    )
