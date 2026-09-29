"""隐藏考卷（ADR 0021）：解析与挑题、删题、出题 + 封存流程、核验时运行、接进 ClaimVerify、
只加不改。沙箱和 LLM 都用假的；真实效果见演示仓库和 black 的评测记录。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from test_verify import BASE, FAIL, HEAD, PASS, FakeBench, exam, pf, pull

from failgate.repro.sandbox import ExecResult
from failgate.verify.engine import ClaimVerdict, ClaimVerifier
from failgate.verify.hidden import (
    HiddenDraft,
    HiddenExam,
    failure_types,
    hidden_path,
    is_hidden_path,
    list_tests,
    prune,
    run_hidden,
    seal_hidden,
    select_tests,
)
from failgate.verify.receipt import check_receipt, code_sha256
from failgate.verify.report import render_verification

HPATH = "tests/test_failgate_issue_7_hidden.py"
CODE = '''"""hidden"""
from mylib.core import parse


def helper():
    return {}


def test_hidden_a():
    parse({})


@pytest.mark.slow
def test_hidden_b():
    parse({"x": 1})


def test_hidden_c():
    assert parse({"name": 1}) == 2
'''


def section(name: str, tb: str) -> str:
    return f"{'_' * 10} {name} {'_' * 10}\n{tb}\n"


def tb(exc: str) -> str:
    return ("Traceback (most recent call last):\n"
            '  File "/workspace/src/tests/x.py", line 3, in t\n    f()\n' + exc)


def run_out(results: dict[str, str], types: dict[str, str], *, exit_code: int | None = None,
            **kw: Any) -> ExecResult:
    """results：{题名: PASSED/FAILED}；types：{题名: 失败时的异常行}。"""
    body = "=== FAILURES ===\n" + "".join(section(n, tb(types[n])) for n in types)
    body += "=== short test summary info ===\n"
    body += "".join(f"{s} {HPATH}::{n}\n" for n, s in results.items())
    code = exit_code if exit_code is not None else (1 if "FAILED" in results.values() else 0)
    return ExecResult(phase="run", argv=["python"], exit_code=code, stdout=body, **kw)


# ---------------------------------------------------------------- 纯函数


def test_paths_and_names():
    assert hidden_path("tests/test_failgate_issue_7.py") == HPATH
    assert is_hidden_path(HPATH) and not is_hidden_path("tests/test_failgate_issue_7.py")
    assert list_tests(CODE) == ["test_hidden_a", "test_hidden_b", "test_hidden_c"]
    assert list_tests("def (:") == []


def test_failure_types_come_from_traceback_sections():
    out = run_out({}, {"test_hidden_a": "KeyError: 'name'",
                       "test_hidden_b": "black.parsing.InvalidInput: Cannot parse",
                       "test_hidden_c": "AssertionError: assert 1 == 2"}).stdout
    assert failure_types(out) == {"test_hidden_a": "KeyError", "test_hidden_b": "InvalidInput",
                                  "test_hidden_c": "AssertionError"}


def test_select_keeps_only_failures_with_the_sealed_exception_type():
    run = run_out(
        {"test_hidden_a": "FAILED", "test_hidden_b": "FAILED", "test_hidden_c": "PASSED"},
        {"test_hidden_a": "KeyError: 'name'", "test_hidden_b": "TypeError: boom"},
    )
    names = ["test_hidden_a", "test_hidden_b", "test_hidden_c", "test_hidden_d"]
    kept, dropped = select_tests(run, HPATH, names, "KeyError")
    assert kept == ["test_hidden_a"]
    assert dropped == {"test_hidden_b": "other_failure:TypeError",
                       "test_hidden_c": "passed_on_buggy", "test_hidden_d": "not_run"}
    # 封存时没有签名：任何失败都算
    assert select_tests(run, HPATH, names, None)[0] == ["test_hidden_a", "test_hidden_b"]


def test_prune_removes_tests_with_decorators_and_keeps_the_rest():
    out = prune(CODE, ["test_hidden_c"])
    assert list_tests(out) == ["test_hidden_c"]
    assert "def helper" in out and "from mylib.core import parse" in out
    assert "@pytest.mark.slow" not in out
    compile(out, HPATH, "exec")


# ---------------------------------------------------------------- 出题 + 封存


class FakeWriter:
    model = "fake-model"

    def __init__(self, code: str) -> None:
        self.code = code
        self.cost_usd = 0.0
        self.seen: dict[str, str] = {}

    async def write(self, **kw: str) -> HiddenDraft:
        self.seen = kw
        self.cost_usd += 0.002
        return HiddenDraft(rule="missing keys return None", code=self.code)


class HiddenBench:
    """按调用顺序返回事先准备好的运行结果；记下每次跑的是哪个文件、在哪个提交上。"""

    def __init__(self, runs: list[ExecResult], broken: bool = False) -> None:
        self.runs = list(runs)
        self.broken = broken
        self.calls: list[tuple[str, str, str]] = []

    async def prepare(self, repo: str, sha: str, exam: Any) -> str:
        if self.broken:
            raise RuntimeError("pip install failed")
        return sha

    async def run_exam(self, prepared: str, e: Any) -> ExecResult:
        self.calls.append((prepared, e.test_path, e.code))
        return self.runs.pop(0)


def seal(bench: HiddenBench, writer: FakeWriter):
    return asyncio.run(seal_hidden(bench, writer, exam(), repo="acme/app", title="t", body="b",  # type: ignore[arg-type]
                                   source_repo="acme/app", source_sha=BASE))


KEYERR = "KeyError: 'name'"


def test_seal_drops_bad_tests_then_confirms_and_signs_a_receipt():
    first = run_out({"test_hidden_a": "FAILED", "test_hidden_b": "FAILED",
                     "test_hidden_c": "FAILED"},
                    {"test_hidden_a": KEYERR, "test_hidden_b": KEYERR,
                     "test_hidden_c": "AssertionError: assert None == 2"})
    confirm = run_out({"test_hidden_a": "FAILED", "test_hidden_b": "FAILED"},
                      {"test_hidden_a": KEYERR, "test_hidden_b": KEYERR})
    bench = HiddenBench([first, confirm])
    writer = FakeWriter(CODE)
    out = seal(bench, writer)
    assert out.reason == "ok" and out.hidden is not None
    h = out.hidden
    assert h.tests == ["test_hidden_a", "test_hidden_b"]
    assert list_tests(h.code) == h.tests
    assert out.dropped == {"test_hidden_c": "other_failure:AssertionError"}
    # 两次都在 issue 时的代码（source_sha）上、隐藏考卷自己的路径上跑
    assert [(c[0], c[1]) for c in bench.calls] == [(BASE, HPATH), (BASE, HPATH)]
    # 出题人只拿到 issue 和公开考卷
    assert set(writer.seen) == {"title", "body", "exam_path", "exam_code", "package"}
    r = h.receipt
    assert check_receipt(r, h.code) == []
    assert r["tests"] == 2 and r["validated_on"]["source_sha"] == BASE
    assert "code" not in r and "test_hidden_a" not in str(r)  # 收据里没有题目


def test_seal_drops_tests_that_stop_failing_on_the_confirm_run():
    first = run_out({"test_hidden_a": "FAILED", "test_hidden_b": "FAILED"},
                    {"test_hidden_a": KEYERR, "test_hidden_b": KEYERR})
    confirm = run_out({"test_hidden_a": "PASSED", "test_hidden_b": "FAILED"},
                      {"test_hidden_b": KEYERR})
    out = seal(HiddenBench([first, confirm]), FakeWriter(CODE))
    assert out.hidden is not None and out.hidden.tests == ["test_hidden_b"]
    assert out.dropped["test_hidden_a"] == "confirm:passed_on_buggy"


@pytest.mark.parametrize(
    ("bench", "code", "reason"),
    [
        (HiddenBench([]), "x = 1\n", "no_tests"),
        (HiddenBench([], broken=True), CODE, "setup"),
        (HiddenBench([run_out({"test_hidden_a": "PASSED"}, {})]), CODE, "none_kept"),
    ],
)
def test_seal_reports_why_nothing_was_sealed(bench, code, reason):
    out = seal(bench, FakeWriter(code))
    assert out.hidden is None and out.reason == reason


# ---------------------------------------------------------------- 核验时运行


HIDDEN = HiddenExam(hidden_id="h" * 32, evidence_id="e" * 32, test_path=HPATH, code=CODE,
                    test_sha256=code_sha256(CODE), tests=["test_hidden_a", "test_hidden_b"])
ALL_PASS = run_out({"test_hidden_a": "PASSED", "test_hidden_b": "PASSED"}, {})
ONE_FAIL = run_out({"test_hidden_a": "PASSED", "test_hidden_b": "FAILED"},
                   {"test_hidden_b": "AssertionError: x"})


def hidden_run(*runs: ExecResult):
    return asyncio.run(run_hidden(HiddenBench(list(runs)), HEAD, exam(), HIDDEN))


def test_run_hidden():
    r = hidden_run(ALL_PASS)
    assert (r.status, r.passed, r.failed, r.suspicious) == ("ok", 2, 0, False)
    r = hidden_run(ONE_FAIL, ONE_FAIL)  # 失败要重跑确认
    assert (r.passed, r.failed, r.suspicious) == (1, 1, True)
    assert hidden_run(ONE_FAIL, ALL_PASS).failed == 0  # 偶发失败不算
    assert hidden_run(run_out({}, {}, exit_code=124, timed_out=True)).reason == "infra"
    assert hidden_run(run_out({}, {}, exit_code=2)).reason == "invalid"


class HiddenFakeBench(FakeBench):
    """公开考卷按 FakeBench 的剧本返回；隐藏考卷的路径另给结果。"""

    def __init__(self, hidden_runs: list[ExecResult], **kw: Any) -> None:
        super().__init__(**kw)
        self.hidden_runs = list(hidden_runs)

    async def run_exam(self, prepared: str, e: Any) -> ExecResult:
        if is_hidden_path(e.test_path):
            return self.hidden_runs.pop(0)
        return await super().run_exam(prepared, e)


def test_hidden_failures_are_a_hint_that_never_changes_the_verdict():
    e = exam(hidden=HIDDEN)
    bench = HiddenFakeBench([ONE_FAIL, ONE_FAIL], exam_runs={BASE: [FAIL, FAIL],
                                                             HEAD: [PASS, PASS]})
    v = asyncio.run(ClaimVerifier(bench).verify(pull([pf("mylib/core.py")]), [7], {7: e}))
    c = v.claims[0]
    assert v.verdict == ClaimVerdict.VERIFIED
    assert c.hidden is not None and c.hidden.suspicious
    zh, en = render_verification(v, "zh"), render_verification(v, "en")
    assert "1/2 道没有通过" in zh and "疑似只迎合了公开考卷" in zh
    assert "1/2 failed" in en
    # 报告和收据里都没有题目内容
    for text in (zh, en, str(v.receipt())):
        assert "parse({\"x\": 1})" not in text and "test_hidden_b" not in text

    # 第一层没通过就不跑隐藏考卷
    bench = HiddenFakeBench([], exam_runs={BASE: [FAIL, FAIL], HEAD: [FAIL, FAIL]})
    v = asyncio.run(ClaimVerifier(bench).verify(pull([pf("mylib/core.py")]), [7], {7: e}))
    assert v.verdict == ClaimVerdict.REFUTED and v.claims[0].hidden is None


# ---------------------------------------------------------------- 存储：只加不改


async def test_hidden_exam_is_loaded_with_the_exam_and_cannot_be_changed(tmp_path):
    from conftest import REPO as HARNESS_REPO
    from sqlalchemy import select
    from test_repro_fixtures import l2_report, run_source_issue
    from test_repro_pipeline import FakeRunner

    from failgate.db import Evidence, HiddenExamRecord, SealedEvidenceError
    from failgate.verify.store import hidden_row, latest_exam

    async for h in run_source_issue(FakeRunner(l2_report()), tmp_path):  # type: ignore[arg-type]
        db = h.failgate.db
        async with db.session() as s:
            ev = (await s.scalars(select(Evidence))).one()
            assert (await latest_exam(s, HARNESS_REPO, 1)).hidden is None  # type: ignore[union-attr]
        sealed = HIDDEN.model_copy(update={"evidence_id": ev.id,
                                           "receipt": {"receipt_sha256": "r" * 64}})
        async with db.session() as s, s.begin():
            s.add(hidden_row(sealed))
        async with db.session() as s:
            got = await latest_exam(s, HARNESS_REPO, 1)
        assert got is not None and got.hidden is not None
        assert (got.hidden.code, got.hidden.tests) == (CODE, HIDDEN.tests)
        with pytest.raises(SealedEvidenceError):
            async with db.session() as s, s.begin():
                row = await s.get(HiddenExamRecord, HIDDEN.hidden_id)
                assert row is not None
                row.test_code = "def test_hidden_a(): pass\n"


# ---------------------------------------------------------------- 流水线：封存考卷时自动出题


class HiddenRunner:
    """复现 runner：返回 L2 报告，并且会出隐藏题（结果由 make 决定）。"""

    def __init__(self, report: Any, make: Any) -> None:
        from test_repro_pipeline import FakeRunner

        self.inner = FakeRunner(report)
        self.requests = self.inner.requests
        self.make = make
        self.hidden_calls: list[Any] = []

    async def __call__(self, req: Any) -> Any:
        return await self.inner(req)

    async def hidden(self, req: Any, sealed: Any) -> Any:
        self.hidden_calls.append((req, sealed))
        return self.make(sealed)


def _sealed_hidden(sealed: Any) -> Any:
    from failgate.verify.hidden import HiddenSeal

    h = HIDDEN.model_copy(update={"evidence_id": sealed.receipt.evidence_id,
                                  "receipt": {"receipt_sha256": "r" * 64}})
    return HiddenSeal(hidden=h, cost_usd=0.01)


async def test_pipeline_seals_hidden_exam_with_the_evidence_and_publishes_only_the_hash(tmp_path):
    from sqlalchemy import select
    from test_repro_fixtures import l2_report, run_source_issue
    from test_repro_pipeline import case_detail, summary

    from failgate.db import Evidence, HiddenExamRecord

    runner = HiddenRunner(l2_report(), _sealed_hidden)
    async for h in run_source_issue(runner, tmp_path):  # type: ignore[arg-type]
        req, sealed = runner.hidden_calls[0]
        assert sealed.receipt.acceptance and req.title  # 出题人拿到 issue 和刚封存的考卷
        async with h.failgate.db.session() as s:
            ev = (await s.scalars(select(Evidence))).one()
            row = (await s.scalars(select(HiddenExamRecord))).one()
        assert row.evidence_id == ev.id and row.test_code == CODE
        case = await case_detail(h)
        out = next(r for r in case["runs"] if r["skill"] == "repro")["output"]
        assert (out["hidden_tests"], out["hidden_sha256"]) == (2, HIDDEN.test_sha256)
        body = summary(case)
        sha = HIDDEN.test_sha256[:12]
        assert f"隐藏考卷：2 道变体题已封存，题目不公开（sha256 `{sha}`）" in body
        assert "test_hidden_a" not in body and 'parse({"x": 1})' not in body


def _none_kept(sealed: Any) -> Any:
    from failgate.verify.hidden import HiddenSeal

    return HiddenSeal(reason="none_kept")


def _llm_down(sealed: Any) -> Any:
    raise RuntimeError("LLM down")


@pytest.mark.parametrize(("make", "reason"), [(_none_kept, "none_kept"),
                                              (_llm_down, "error:RuntimeError")])
async def test_hidden_exam_failure_never_blocks_the_public_exam(tmp_path, make, reason):
    from sqlalchemy import select
    from test_repro_fixtures import l2_report, run_source_issue
    from test_repro_pipeline import case_detail, summary

    from failgate.db import Evidence, HiddenExamRecord

    runner = HiddenRunner(l2_report(), make)
    async for h in run_source_issue(runner, tmp_path):  # type: ignore[arg-type]
        async with h.failgate.db.session() as s:
            assert (await s.scalars(select(Evidence))).one() is not None
            assert (await s.scalars(select(HiddenExamRecord))).all() == []
        case = await case_detail(h)
        assert case["state"] == "REPRODUCED"
        out = next(r for r in case["runs"] if r["skill"] == "repro")["output"]
        assert out["hidden_reason"] == reason and out["hidden_sha256"] is None
        assert "隐藏考卷" not in summary(case)
