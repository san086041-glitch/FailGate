"""严格 FB/PA 回放（技术方案 18 节）：复现脚本在修复提交的父提交上失败、在修复提交上通过。

    复现回放的结果（每个 issue 的 L1 脚本或 L2 测试、当时的 Python、失败签名）
        │  GraphQL 找关闭 issue 的 PR → 修复提交 + 父提交
        ▼
    父提交、修复提交各自从源码构建环境（source.py），用 L1 时的同一个 Python
        │  每个版本跑 RUNS 次，结果必须一致
        ▼
    父提交：失败，且和当初复现时是同一个失败      修复提交：通过
        └──────────────── 两个都满足 = FB/PA ────────────────┘

和代理指标的区别：代理指标比的是"报告版本 vs 最新正式版"，中间隔着很多提交，
最新版不再失败可能是别的改动造成的；这里两份代码只差修复这一个提交。

不调用 LLM：脚本是现成的，"是不是同一个失败"用失败签名比较（judge.same_failure）。
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from pydantic import BaseModel

from failgate.index.trace import TraceSignature
from failgate.replay.fixes import FixCommit
from failgate.replay.metrics import wilson
from failgate.replay.repro import outcome as proxy_outcome
from failgate.repro.issue import IssueReproReport, L2IssueReport
from failgate.repro.judge import same_failure
from failgate.repro.sandbox import ExecResult

RUNS = 2  # 每个版本跑几次；结果不一致就记为 inconsistent，不算 FB/PA


class RunBrief(BaseModel):
    exit_code: int
    timed_out: bool = False
    oom_killed: bool = False
    same_failure: bool | None = None  # 只对失败的运行有意义：是不是 L1 时的那个失败
    duration_s: float = 0.0
    tail: str = ""
    log_dir: str | None = None

    @property
    def infra(self) -> bool:
        return self.timed_out or self.oom_killed

    @property
    def failed(self) -> bool:
        return self.exit_code != 0 and not self.infra


def brief(run: ExecResult, l1: TraceSignature | None, module: str | None) -> RunBrief:
    return RunBrief(
        exit_code=run.exit_code, timed_out=run.timed_out, oom_killed=run.oom_killed,
        same_failure=same_failure(run, l1, module) if run.failed else None,
        duration_s=run.duration_s, tail=run.output_tail(15), log_dir=run.log_dir,
    )


class FbpaCase(BaseModel):
    number: int
    title: str
    reported_version: str | None = None
    python: str | None = None
    proxy: str  # 原复现回放里的结果类别（replay.repro.outcome）
    fix: FixCommit | None = None
    pretend_version: str | None = None
    before: list[RunBrief] = []
    after: list[RunBrief] = []
    outcome: str = ""
    error: str | None = None


OUTCOME_LABELS = {
    "fb_pa": "修复前失败（同一个失败）+ 修复后通过（FB/PA）",
    "not_fail_before": "修复前没有失败",
    "before_mismatch": "修复前失败了，但不是 L1 时的那个失败",
    "fail_after": "修复后仍然失败",
    "inconsistent": "同一版本多次运行结果不一致",
    "inconclusive": "超时或内存超限",
    "setup_failed": "源码环境没搭起来",
    "no_fix": "没有修复提交（手动关闭等），不参与统计",
    "no_script": "当初没有复现（没有脚本或测试），不参与统计",
}
ELIGIBLE_EXCLUDED = ("no_fix", "no_script")


def classify(before: Sequence[RunBrief], after: Sequence[RunBrief]) -> str:
    """纯函数：两组运行结果 → 结果类别。每组内部必须一致，否则 inconsistent。"""
    runs = [*before, *after]
    if not before or not after:
        raise ValueError("修复前后都至少要有一次运行")
    if any(r.infra for r in runs):
        return "inconclusive"

    def state(r: RunBrief) -> str:
        if not r.failed:
            return "pass"
        return "same" if r.same_failure else "other"

    b, a = {state(r) for r in before}, {state(r) for r in after}
    if len(b) > 1 or len(a) > 1:
        return "inconsistent"
    bs, as_ = b.pop(), a.pop()
    if bs == "pass":
        return "not_fail_before"
    if bs == "other":
        return "before_mismatch"
    if as_ != "pass":
        return "fail_after"
    return "fb_pa"


class Candidate(BaseModel):
    """要做严格 FB/PA 的一份复现：L1 脚本或 L2 测试，以及它当初复现时的条件。"""

    number: int
    title: str
    reported_version: str | None = None
    python: str | None = None  # 当初复现用的 Python；修复前后都用它
    code: str | None = None  # 脚本或测试文件的内容；None = 当初没复现，不参与统计
    observed: TraceSignature | None = None  # 当初复现时的失败签名
    module: str | None = None
    proxy: str = "—"


def candidate_from_l1(report: IssueReproReport) -> Candidate:
    rep = report.repro.reported
    ok = report.repro.level.value == "L1" and rep is not None and report.agent is not None
    return Candidate(
        number=report.number, title=report.title, reported_version=report.repro.reported_version,
        python=rep.python if rep else None, module=report.repro.module,
        code=report.agent.final_script if ok and report.agent else None,
        observed=rep.verdict.observed if rep else None, proxy=proxy_outcome(report),
    )


def candidate_from_l2(report: L2IssueReport) -> Candidate:
    src, run = report.source, report.source.run
    ok = src.level.value == "L2" and run is not None and report.agent is not None
    return Candidate(
        number=report.number, title=report.title, reported_version=report.intake_version,
        python=src.python, module=src.module,
        code=report.agent.final_script if ok and report.agent else None,
        observed=run.verdict.observed if run else None,
    )


async def evaluate_case(
    cand: Candidate,
    *,
    find_fix: Callable[[int], Awaitable[FixCommit | None]],
    pretend: Callable[[FixCommit], Awaitable[str]],
    run_at: Callable[[str, str, str, str], Awaitable[list[ExecResult]]],
    setup_errors: tuple[type[BaseException], ...],
) -> FbpaCase:
    """一个 issue 的严格 FB/PA。run_at(提交, Python, 伪版本号, 代码) 返回该提交上的各次运行。"""
    case = FbpaCase(
        number=cand.number, title=cand.title, reported_version=cand.reported_version,
        python=cand.python, proxy=cand.proxy,
    )
    if not cand.code or not cand.python:
        case.outcome = "no_script"
        return case
    try:
        case.fix = await find_fix(cand.number)
        if case.fix is None:
            case.outcome = "no_fix"
            return case
        case.pretend_version = await pretend(case.fix)
        before = await run_at(case.fix.parent, cand.python, case.pretend_version, cand.code)
        after = await run_at(case.fix.sha, cand.python, case.pretend_version, cand.code)
    except setup_errors as e:
        case.outcome, case.error = "setup_failed", str(e)[:500]
        return case
    case.before = [brief(r, cand.observed, cand.module) for r in before]
    case.after = [brief(r, cand.observed, cand.module) for r in after]
    case.outcome = classify(case.before, case.after)
    return case


def summarize(cases: Sequence[FbpaCase]) -> dict[str, Any]:
    counts = Counter(c.outcome for c in cases)
    eligible = [c for c in cases if c.outcome not in ELIGIBLE_EXCLUDED]
    k, n = counts["fb_pa"], len(eligible)
    proxy_k = sum(c.proxy == "fb_pa" for c in eligible)
    return {
        "n": len(cases),
        "eligible": n,
        "outcomes": dict(counts),
        "fb_pa": k,
        "fb_pa_rate": k / n if n else 0.0,
        "fb_pa_ci": wilson(k, n),
        # 同一批 issue 上的代理指标，方便对照
        "proxy_fb_pa": proxy_k,
        "agree": sum((c.outcome == "fb_pa") == (c.proxy == "fb_pa") for c in eligible),
    }


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _runs(runs: Sequence[RunBrief]) -> str:
    def one(r: RunBrief) -> str:
        if r.infra:
            return "超时" if r.timed_out else "OOM"
        if not r.failed:
            return "过"
        return "败" if r.same_failure else "败≠"

    return "/".join(one(r) for r in runs) or "—"


def render(repo: str, cases: Sequence[FbpaCase], meta: dict[str, Any]) -> str:
    s = summarize(cases)
    lo, hi = s["fb_pa_ci"]
    kind = meta.get("kind", "L1 脚本")
    lines = [
        f"# {repo} 严格 FB/PA 回放（{kind}）",
        "",
        f"- 时间：{meta['started']}；来源：`{meta['source_run']}`",
        f"- 定义：原复现回放里的{kind}，放到**修复提交的父提交**上应失败"
        "（且和当初复现时是同一个失败），放到**修复提交**上应通过。"
        "两份代码都从源码构建，Python 用当初复现时的同一个。",
        f"- 每个版本跑 {meta['runs']} 次，结果必须一致；不调用 LLM。",
        "",
        "## 汇总",
        "",
        "| 指标 | 结果 |",
        "|---|---|",
        f"| 样本数 | {s['n']}，参与统计 {s['eligible']}（有{kind}且有修复提交） |",
        f"| **严格 FB/PA** | **{s['fb_pa']}/{s['eligible']} = {_pct(s['fb_pa_rate'])}**"
        f"（95% Wilson 区间 {_pct(lo)}–{_pct(hi)}） |",
        f"| 同一批 issue 上的代理 FB/PA | {s['proxy_fb_pa']}/{s['eligible']} |",
        f"| 严格和代理结论一致的 | {s['agree']}/{s['eligible']} |",
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
        "败 = 和当初复现时同一个失败；败≠ = 失败但不是同一个；过 = 通过。",
        "",
        "| # | 标题 | 报告版本 | Python | 修复 PR | 父提交 → 修复提交 "
        "| 修复前 | 修复后 | 严格 | 代理 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in cases:
        title = c.title.replace("|", "\\|")[:50]
        fix = c.fix
        pr = f"#{fix.pr}" if fix and fix.pr else "—"
        commits = f"`{fix.parent[:7]}` → `{fix.sha[:7]}`" if fix else "—"
        lines.append(
            f"| {c.number} | {title} | {c.reported_version or '—'} | {c.python or '—'} | {pr} | "
            f"{commits} | {_runs(c.before)} | {_runs(c.after)} | {c.outcome} | {c.proxy} |"
        )
    notes = [c for c in cases if c.outcome not in ("fb_pa", "no_script")]
    if notes:
        lines += ["", "## 不是 FB/PA 的", ""]
        for c in notes:
            lines.append(f"- #{c.number}（{c.outcome}）：{note_for(c)[:200]}")
    return "\n".join(lines) + "\n"


def _last_line(runs: Sequence[RunBrief]) -> str:
    return next((r.tail.strip().splitlines()[-1] for r in runs if r.tail.strip()), "")


def note_for(c: FbpaCase) -> str:
    if c.error:
        return c.error
    if c.outcome == "no_fix":
        return "issue 不是被 PR 或提交关闭的（手动关闭），没有可对照的修复提交"
    if c.outcome == "not_fail_before":
        return "脚本在父提交上正常退出：这个输入在修复 PR 之前已经不再触发 bug"
    if c.outcome == "fail_after":
        return f"修复提交上的失败：{_last_line(c.after)}"
    if c.outcome == "before_mismatch":
        return f"父提交上的失败：{_last_line(c.before)}"
    return _last_line([*c.before, *c.after])


def dump(cases: Sequence[FbpaCase], meta: dict[str, Any]) -> str:
    return json.dumps(
        {"meta": meta, "summary": summarize(cases),
         "cases": [c.model_dump(mode="json") for c in cases]},
        ensure_ascii=False, indent=2,
    )
