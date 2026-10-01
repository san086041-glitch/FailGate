"""修复 Agent 提升实验的纯函数：金标准的推导、判定、汇总（ADR 0028）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from failgate.replay import fix_eval as fe
from failgate.repro.sandbox import ExecResult
from failgate.verify.tamper import PullFile


def run(stdout: str, exit_code: int = 1, **kw: Any) -> ExecResult:
    return ExecResult(phase="run", argv=["python", "-m", "pytest"], exit_code=exit_code,
                      stdout=stdout, **kw)


PARENT = """..F.F
FAILED tests/test_format.py::test_simple_format[pep_572] - AssertionError
FAILED tests/test_format.py::test_simple_format[flaky] - AssertionError
2 failed, 1200 passed, 3 skipped in 31.20s
"""
FIX = """....F
FAILED tests/test_format.py::test_simple_format[flaky] - AssertionError
1 failed, 1201 passed, 3 skipped in 30.02s
"""


def gold() -> fe.Gold:
    return fe.derive_gold(run(PARENT), run(FIX), targets=["tests/test_format.py"],
                          test_files=["tests/data/cases/pep_572.py"], removed=[])


def test_gold_targets_prefers_changed_test_modules():
    files = [PullFile(filename="tests/test_format.py", status="modified"),
             PullFile(filename="tests/data/cases/x.py", status="added"),
             PullFile(filename="tests/conftest.py", status="modified"),
             PullFile(filename="tests/test_old.py", status="removed")]
    assert fe.gold_targets(files, "tests") == ["tests/test_format.py"]
    # 只改了测试数据：跑整个测试目录
    assert fe.gold_targets([files[1]], "tests") == ["tests"]


def test_deps_changed_detects_new_requirements_only():
    added = PullFile(filename="pyproject.toml", status="modified",
                     patch='@@ -69,6 +69,7 @@\n   "platformdirs>=2",\n+  "pytokens>=0.1.10",\n')
    assert fe.deps_changed([added]) == ["pyproject.toml"]
    # 改工具配置不算
    config = PullFile(filename="pyproject.toml", status="modified",
                      patch='@@ -1 +1 @@\n-target-version = ["py38"]\n+target-version = ["py39"]\n'
                            '+  "--strict-markers",\n')
    assert fe.deps_changed([config]) == []
    req = PullFile(filename="docs/requirements.txt", status="modified", patch="+sphinx==7.0\n")
    assert fe.deps_changed([req]) == ["docs/requirements.txt"]
    nested = PullFile(filename="tests/data/pyproject.toml", status="modified",
                      patch='+  "foo>=1",\n')
    assert fe.deps_changed([nested]) == []
    assert fe.deps_changed([PullFile(filename="src/black/x.py", status="modified",
                                     patch='+  "a>=1",\n')]) == []


def test_is_test_change():
    assert fe.is_test_change("tests/data/cases/x.py", "tests")
    assert fe.is_test_change("src/pkg/test_util.py", "tests")
    assert not fe.is_test_change("src/black/linegen.py", "tests")


def test_counts_parses_the_last_summary_line():
    assert fe.counts(PARENT) == {"failed": 2, "passed": 1200, "skipped": 3}
    assert fe.counts("1 passed, 2 errors in 1s")["error"] == 2
    assert fe.counts("no summary here") == {}


def test_derive_gold_f2p_is_parent_failures_minus_fix_failures():
    g = gold()
    assert g.status == "ok"
    assert g.f2p == ["tests/test_format.py::test_simple_format[pep_572]"]
    assert g.fail_fix == ["tests/test_format.py::test_simple_format[flaky]"]
    assert g.counts_fix["passed"] == 1201


def test_derive_gold_without_f2p_is_unusable():
    g = fe.derive_gold(run(FIX), run(FIX), targets=["tests"], test_files=[], removed=[])
    assert g.status == "no_f2p" and g.f2p == []


def test_derive_gold_invalid_runs_are_infra():
    g = fe.derive_gold(run("", exit_code=2), run(FIX), targets=["tests"], test_files=[],
                       removed=[])
    assert g.status == "infra" and g.reason == "parent:exit_2"
    g = fe.derive_gold(run(PARENT), run(FIX, timed_out=True), targets=["tests"],
                       test_files=[], removed=[])
    assert g.reason == "fix:timeout"


def test_judge_resolved_when_failures_are_a_subset_of_the_fix():
    assert fe.judge(run(FIX), gold())["resolved"]
    # 偶发失败的那个在上游修复上也失败，不算 Agent 的错
    only_flaky = "FAILED tests/test_format.py::test_simple_format[flaky]\n1 failed, 1 passed\n"
    assert fe.judge(run(only_flaky), gold())["resolved"]
    ok = fe.judge(run("1205 passed in 3s\n", exit_code=0), gold())
    assert ok["resolved"] and ok["f2p_passed"] == 1


def test_judge_unresolved_and_regressions():
    j = fe.judge(run(PARENT), gold())
    assert not j["resolved"] and j["f2p_passed"] == 0 and j["broken_n"] == 0
    broke = FIX.replace("1 failed", "2 failed") + \
        "FAILED tests/test_format.py::test_other - AssertionError\n2 failed, 3 passed\n"
    j = fe.judge(run(broke), gold())
    assert not j["resolved"] and j["broken"] == ["tests/test_format.py::test_other"]
    assert j["f2p_passed"] == 1


def test_judge_collection_error_counts_as_failure():
    out = "ERROR tests/test_format.py - ImportError: cannot import\n1 error in 0.5s\n"
    j = fe.judge(run(out, exit_code=1), gold())
    assert j["valid"] and not j["resolved"] and j["broken"] == ["tests/test_format.py"]


def test_judge_invalid_run():
    j = fe.judge(run("", exit_code=0, timed_out=True), gold())
    assert not j["valid"] and j["reason"] == "timeout" and not j["resolved"]


def test_load_hidden(tmp_path: Path):
    p = tmp_path / "h.jsonl"
    rows = [{"number": 1, "sealed": True, "code": "def test_a(): pass\n", "tests": ["test_a"]},
            {"number": 2, "sealed": False}]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    got = fe.load_hidden(p)
    assert list(got) == [1] and got[1].tests == ["test_a"]
    assert fe.load_hidden(tmp_path / "missing.jsonl") == {}


def _row(number: int, rep: int, arm: str, *, resolved: bool, passed: bool = False,
         hidden: dict[str, Any] | None = None, valid: bool = True) -> dict[str, Any]:
    return {"type": "run", "number": number, "rep": rep, "arm": arm,
            "fix": {"status": "passed" if passed else "failed", "passed": passed,
                    "cost_usd": 0.03, "steps": 50, "duration_s": 300.0,
                    "files": ["src/black/x.py"], "denied": 0},
            "gold": {"valid": valid, "resolved": resolved, "reason": "" if valid else "timeout",
                     "f2p_passed": int(resolved), "f2p_total": 1, "broken_n": 0},
            "hidden": hidden}


def test_summarize_pairs_and_cross_tab():
    rows = [
        {"type": "gold", "number": 1, "gold": gold().model_dump()},
        _row(1, 1, "exam", resolved=True, passed=True,
             hidden={"total": 4, "failed": [], "flagged": False}),
        _row(1, 1, "control", resolved=False),
        _row(1, 2, "exam", resolved=False, passed=True,
             hidden={"total": 4, "failed": ["test_h"], "flagged": True}),
        _row(1, 2, "control", resolved=False),
        _row(2, 1, "exam", resolved=True, passed=True),
        _row(2, 1, "control", resolved=True),
        _row(2, 2, "exam", resolved=False, valid=False),
    ]
    s = fe.summarize(rows)
    assert s["arms"]["exam"]["n"] == 3 and s["arms"]["exam"]["resolved"] == 2
    assert s["arms"]["control"]["resolved"] == 1
    assert s["paired"] == {"gained": 1, "lost": 0, "same": 2, "p": 1.0}
    assert s["false_pass"] == 1 and s["exam_passed"] == 3
    assert s["hidden"] == {"n": 2, "caught": 1, "missed": 0, "false_alarm": 0, "good": 1}
    assert s["invalid_runs"] == 1
    text = fe.render(rows, {"repo": "psf/black", "started": "t", "source": "s"})
    assert "差值 +33 个百分点" in text and "被隐藏考卷抓到 1" in text
    assert "无效（timeout）" in text
    assert "## 规划 → 修改的交接" not in text  # 没有交接组就不出这一节


def test_arm_names_with_handoff():
    assert fe.valid_arm("exam") and fe.valid_arm("exam:notes") and fe.valid_arm("control:continue")
    assert not fe.valid_arm("exam:magic") and not fe.valid_arm("other")
    assert fe.arm_base("exam:notes") == "exam" and fe.arm_handoff("exam:notes") == "notes"
    assert fe.arm_handoff("exam") == "reset"


def test_summarize_handoff_arms_against_reset():
    def with_reads(row: dict[str, Any], reads: int, rereads: int, first: int) -> dict[str, Any]:
        row["fix"].update(edit_reads=reads, rereads=rereads, first_edit_step=first)
        return row

    rows = [
        with_reads(_row(1, 1, "exam", resolved=False, passed=True), 10, 7, 50),
        with_reads(_row(1, 1, "exam:notes", resolved=True, passed=True), 4, 1, 30),
        with_reads(_row(1, 2, "exam", resolved=True, passed=True), 8, 5, 40),
        with_reads(_row(1, 2, "exam:notes", resolved=True, passed=True), 6, 1, 20),
    ]
    s = fe.summarize(rows)
    assert list(s["arms"]) == ["exam", "exam:notes"]
    assert s["diff"] is None and s["paired"] is None  # 没有对照组
    assert s["handoff_pairs"]["exam:notes"] == {"gained": 1, "lost": 0, "same": 1, "p": 1.0}
    assert s["arms"]["exam"]["rereads"] == 12 and s["arms"]["exam"]["edit_reads"] == 18
    assert s["arms"]["exam:notes"]["first_edit"] == 25
    text = fe.render(rows, {"repo": "psf/black", "started": "t", "source": "s"})
    assert "## 规划 → 修改的交接" in text
    assert "| exam | reset（从空白开始） | 18 | 12 | 67% | 45 |" in text
    assert "1 / 0 / 1，p = 1.00" in text
    assert "实验组（给封存考卷），交接 notes" in text


def test_patch_edits_reads_the_transcript(tmp_path: Path):
    t = tmp_path / "fix.json"
    t.write_text(json.dumps({"result": {"edits": {"src/a.py": "x = 1\n"}}}), encoding="utf-8")
    assert fe.patch_edits({"fix": {"transcript_path": str(t)}}) == {"src/a.py": "x = 1\n"}
    assert fe.patch_edits({"fix": {"transcript_path": None}}) == {}


def test_summarize_and_render_verify():
    rows = [
        {"number": 4296, "rep": 1, "gold_resolved": False, "broken_n": 8, "verdict": "REFUTED",
         "reasons": ["layer3:new_failures"],
         "layer3": {"status": "fail", "new_failures": ["a", "b"]}},
        {"number": 4261, "rep": 1, "gold_resolved": False, "broken_n": 0, "verdict": "VERIFIED",
         "reasons": [], "layer3": {"status": "none", "new_failures": []}},
        {"number": 4399, "rep": 1, "gold_resolved": True, "broken_n": 0, "verdict": "VERIFIED",
         "reasons": [], "layer3": {"status": "pass", "new_failures": []}},
    ]
    s = fe.summarize_verify(rows)
    assert (s["bad"], s["bad_refuted"], s["good"], s["good_refuted"]) == (2, 1, 1, 0)
    text = fe.render_verify(rows)
    assert text.startswith(fe.VERIFY_HEADING) and "被驳回 1" in text
    assert "2（第三层 fail）" in text


def test_claim_from_row_feeds_the_feedback():
    from failgate.fix.feedback import feedback_from_claim

    row = {"number": 4296, "verdict": "REFUTED", "reasons": ["layer3:new_failures"],
           "layer3": {"status": "fail", "reason": "new_failures",
                      "files": ["tests/test_format.py"],
                      "new_failures": ["tests/test_format.py::test_simple_format[comments3]"]}}
    fb = feedback_from_claim(fe.claim_from_row(row, "tests/test_failgate_issue_4296.py"))
    assert fb.actionable and fb.must_pass == [
        "tests/test_format.py::test_simple_format[comments3]"]


def test_render_feedback():
    rows = [
        {"number": 4296, "rep": 1, "round": 1, "status": "passed", "passed": True,
         "cost_usd": 0.04, "gold": {"valid": True, "resolved": False, "f2p_passed": 0,
                                    "f2p_total": 1, "broken_n": 2},
         "verdict": "REFUTED", "final": False},
        {"number": 4296, "rep": 1, "round": 2, "status": "passed", "passed": True,
         "cost_usd": 0.05, "gold": {"valid": True, "resolved": True, "f2p_passed": 1,
                                    "f2p_total": 1, "broken_n": 0},
         "verdict": "VERIFIED", "final": True},
    ]
    text = fe.render_feedback(rows)
    assert "1 个被驳回的补丁里，重修后金标准修好 1 个" in text and "$0.0900" in text
