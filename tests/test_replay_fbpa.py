"""严格 FB/PA 回放：结果分类（纯函数）、单个 issue 的编排（假的 GitHub 和沙箱）、汇总和报告。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from warden.replay.fbpa import (
    FbpaCase,
    RunBrief,
    classify,
    dump,
    evaluate_case,
    render,
    summarize,
)
from warden.replay.fixes import FixCommit
from warden.repro.issue import IssueReproReport
from warden.repro.sandbox import ExecResult

HELD_OUT = Path("eval/runs/psf__black__repro__20260925-1519.json")


def held_out(number: int) -> IssueReproReport:
    data = json.loads(HELD_OUT.read_text(encoding="utf-8"))
    row = next(r for r in data["reports"] if r["number"] == number)
    return IssueReproReport.model_validate(row)


def b(exit_code: int = 0, same: bool | None = None, **kw: object) -> RunBrief:
    return RunBrief(exit_code=exit_code, same_failure=same, **kw)  # type: ignore[arg-type]


SAME, OTHER, PASS = b(1, True), b(1, False), b(0)


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        ([SAME, SAME], [PASS, PASS], "fb_pa"),
        ([PASS, PASS], [PASS, PASS], "not_fail_before"),
        ([OTHER, OTHER], [PASS, PASS], "before_mismatch"),
        ([SAME, SAME], [SAME, SAME], "fail_after"),
        ([SAME, SAME], [OTHER, OTHER], "fail_after"),
        ([SAME, PASS], [PASS, PASS], "inconsistent"),
        ([SAME, SAME], [PASS, SAME], "inconsistent"),
        ([SAME, OTHER], [PASS, PASS], "inconsistent"),  # 两次失败不是同一种
        ([b(124, timed_out=True)], [PASS], "inconclusive"),
        ([SAME], [b(137, oom_killed=True)], "inconclusive"),
    ],
)
def test_classify(before: list[RunBrief], after: list[RunBrief], expected: str):
    assert classify(before, after) == expected


def test_classify_needs_runs_on_both_sides():
    with pytest.raises(ValueError):
        classify([], [PASS])


FIX = FixCommit(pr=4086, sha="fix" + "0" * 37, parent="par" + "0" * 37)


async def run_case(report: IssueReproReport, outputs: dict[str, list[ExecResult]],
                   fix: FixCommit | None = FIX) -> tuple[FbpaCase, list[tuple[str, str]]]:
    calls: list[tuple[str, str]] = []

    async def find_fix(n: int) -> FixCommit | None:
        assert n == report.number
        return fix

    async def pretend(f: FixCommit) -> str:
        return "23.11.1.dev0"

    async def run_at(sha: str, python: str, version: str, script: str) -> list[ExecResult]:
        assert script == report.agent.final_script  # type: ignore[union-attr]
        assert version == "23.11.1.dev0"
        calls.append((sha, python))
        return outputs[sha]

    case = await evaluate_case(report, find_fix=find_fix, pretend=pretend, run_at=run_at,
                               setup_errors=(RuntimeError,))
    return case, calls


def failing_like_l1(report: IssueReproReport) -> ExecResult:
    """和 L1 复现时一样的失败输出（回放 JSON 里记录的输出结尾）。"""
    assert report.repro.reported is not None
    return ExecResult(phase="run", argv=["python", "repro.py"], exit_code=1,
                      stderr=report.repro.reported.output_tail)


def ok() -> ExecResult:
    return ExecResult(phase="run", argv=["python", "repro.py"], exit_code=0)


async def test_fb_pa_uses_same_python_as_l1_and_parent_then_fix():
    report = held_out(4062)
    fail = failing_like_l1(report)
    case, calls = await run_case(report, {FIX.parent: [fail, fail], FIX.sha: [ok(), ok()]})
    assert case.outcome == "fb_pa"
    assert all(r.same_failure for r in case.before)
    assert calls == [(FIX.parent, "3.12"), (FIX.sha, "3.12")]  # 控制变量：L1 时的 Python
    assert case.proxy == "fb_pa" and case.pretend_version == "23.11.1.dev0"


async def test_different_failure_before_fix_is_not_fb_pa():
    report = held_out(4062)
    other = ExecResult(phase="run", argv=[], exit_code=1, stderr=(
        'Traceback (most recent call last):\n  File "/workspace/repro.py", line 1, in <module>\n'
        "    import blak\nModuleNotFoundError: No module named 'blak'\n"
    ))
    case, _ = await run_case(report, {FIX.parent: [other, other], FIX.sha: [ok(), ok()]})
    assert case.outcome == "before_mismatch"


async def test_no_fix_commit_and_setup_errors():
    report = held_out(4062)
    case, calls = await run_case(report, {}, fix=None)
    assert case.outcome == "no_fix" and calls == []

    async def boom(*_: object) -> list[ExecResult]:
        raise RuntimeError("源码包超过上限")

    async def find(_: int) -> FixCommit:
        return FIX

    async def pretend(_: FixCommit) -> str:
        return "1"

    case = await evaluate_case(report, find_fix=find, pretend=pretend, run_at=boom,
                               setup_errors=(RuntimeError,))
    assert case.outcome == "setup_failed" and "上限" in (case.error or "")


async def test_issue_without_l1_script_is_skipped():
    report = held_out(4062)
    report.agent.final_script = None  # type: ignore[union-attr]
    case, calls = await run_case(report, {})
    assert case.outcome == "no_script" and calls == []


def test_summary_excludes_ineligible_and_compares_with_proxy():
    cases = [
        FbpaCase(number=1, title="a", proxy="fb_pa", outcome="fb_pa"),
        FbpaCase(number=2, title="b", proxy="fb_pa", outcome="not_fail_before"),
        FbpaCase(number=3, title="c", proxy="still_fails_latest", outcome="fb_pa"),
        FbpaCase(number=4, title="d", proxy="fb_pa", outcome="no_fix"),
        FbpaCase(number=5, title="e", proxy="not_reproduced", outcome="no_script"),
    ]
    s = summarize(cases)
    assert (s["n"], s["eligible"], s["fb_pa"], s["proxy_fb_pa"], s["agree"]) == (5, 3, 2, 2, 1)
    md = render("psf/black", cases, {"started": "t", "source_run": "x.json", "runs": 2})
    assert "**2/3 = 67%**" in md and "代理 FB/PA | 2/3" in md
    assert "#2（not_fail_before）" in md
    assert json.loads(dump(cases, {"runs": 2}))["summary"]["eligible"] == 3
