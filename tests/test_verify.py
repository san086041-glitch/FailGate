"""ClaimVerify（ADR 0017）：声明解析、防篡改规则、相关测试挑选、三层判定和编排。

编排用假的 Workbench：按"提交 + 命令"返回事先准备好的运行结果，不起容器。
真实 Docker 的端到端在文件末尾（fixture 仓库：有 bug 的代码当 base，打上修复的当 head）。
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from failgate.repro.sandbox import DockerSandbox, ExecResult
from failgate.repro.signature import failure_signature
from failgate.verify.claims import parse_claims
from failgate.verify.engine import (
    ClaimVerdict,
    ClaimVerifier,
    Exam,
    ExamRun,
    PullRequest,
    SetupFailed,
    classify_head,
    judge_layer1,
)
from failgate.verify.receipt import check_receipt, code_sha256
from failgate.verify.related import module_of, outcomes, select_related_tests
from failgate.verify.report import render_verification
from failgate.verify.tamper import PullFile, tamper_signals

REPO = "acme/app"
EXAM_PATH = "tests/test_failgate_issue_7.py"
EXAM_CODE = "from mylib.core import parse\n\ndef test_parse():\n    parse({})\n"
TB = (
    "Traceback (most recent call last):\n"
    '  File "/workspace/src/tests/test_failgate_issue_7.py", line 4, in test_parse\n'
    "    parse({})\n"
    '  File "/opt/venv/lib/python3.12/site-packages/mylib/core.py", line 2, in parse\n'
    "    return d['name']\n"
    "KeyError: 'name'\n"
)
OTHER_TB = TB.replace("KeyError: 'name'", "TypeError: boom").replace("core.py", "other.py")


def exam(**kw: Any) -> Exam:
    args: dict[str, Any] = dict(
        evidence_id="e" * 32, issue=7, test_path=EXAM_PATH, code=EXAM_CODE,
        test_sha256=code_sha256(EXAM_CODE), receipt_sha256="r" * 64, package="mylib",
        module="mylib", python="3.12", pytest="pytest==8.3.3", version="0.0.0.dev0",
        signature=failure_signature(TB, "mylib"),
    )
    return Exam(**{**args, **kw})


def res(exit_code: int, out: str = "", **kw: Any) -> ExecResult:
    return ExecResult(phase="run", argv=["python", "-m", "pytest"], exit_code=exit_code,
                      stdout=out, **kw)


FAIL = res(1, TB + f"FAILED {EXAM_PATH}::test_parse - KeyError: 'name'\n")
PASS = res(0, f"PASSED {EXAM_PATH}::test_parse\n1 passed\n")
SKIP = res(0, f"SKIPPED [1] {EXAM_PATH}:3: flaky\n1 skipped\n")


# ---------------------------------------------------------------- 声明解析


@pytest.mark.parametrize(
    ("title", "body", "expected"),
    [
        ("Fix parser", "Fixes #7", [7]),
        ("fix: parser (closes #7)", "Resolves: #8\nAlso fixed #7", [7, 8]),
        ("Refactor", "See #7 and prefix#9, nothing fixed", []),
        ("x", "fixes acme/app#3, fixes other/repo#4", [3]),
        ("x", "unfixed #5", []),  # 关键字要是完整的词
        ("x", None, []),
    ],
)
def test_parse_claims(title, body, expected):
    assert parse_claims(title, body, REPO) == expected


# ---------------------------------------------------------------- 防篡改


def pf(name: str, status: str = "modified", patch: str | None = None, prev: str | None = None):
    return PullFile(filename=name, status=status, patch=patch, previous_filename=prev)


def kinds(signals) -> list[tuple[str, str]]:
    return [(s.level, s.kind) for s in signals]


def test_adding_the_exam_verbatim_is_fine_even_with_crlf():
    files = [pf("mylib/core.py"), pf(EXAM_PATH, "added")]
    code = EXAM_CODE.replace("\n", "\r\n")
    assert tamper_signals(files, test_path=EXAM_PATH, sealed_sha256=code_sha256(EXAM_CODE),
                          head_code=code) == []


@pytest.mark.parametrize(
    ("files", "head_code", "expected"),
    [
        ([pf(EXAM_PATH, "removed")], None, [("high", "exam_removed")]),
        ([pf("tests/other.py", "renamed", prev=EXAM_PATH)], None, [("high", "exam_renamed")]),
        ([pf(EXAM_PATH)], EXAM_CODE + "    assert True\n", [("high", "exam_modified")]),
        # PR 没动考卷，但 head 上同名文件内容不同（比如早就合进去过另一个版本）也算
        ([pf("mylib/core.py")], "def test_parse(): pass\n", [("high", "exam_modified")]),
        ([pf("tests/conftest.py")], None, [("medium", "conftest")]),
        ([pf("pyproject.toml", patch="+[tool.pytest.ini_options]\n+addopts = '-p no:x'")], None,
         [("medium", "pytest_config")]),
        ([pf("pyproject.toml", patch="+version = '2'")], None, []),
        ([pf("tests/test_core.py", patch="+@pytest.mark.skip\n def test_a(): ...")], None,
         [("medium", "skip_added")]),
        ([pf("tests/test_core.py", "removed")], None, [("medium", "test_removed")]),
    ],
)
def test_tamper_rules(files, head_code, expected):
    signals = tamper_signals(files, test_path=EXAM_PATH, sealed_sha256=code_sha256(EXAM_CODE),
                             head_code=head_code)
    assert kinds(signals) == expected


# ---------------------------------------------------------------- 相关测试


def test_module_of():
    assert module_of("mylib/core.py") == "mylib.core"
    assert module_of("src/mylib/__init__.py") == "mylib"
    assert module_of("docs/conf.py") == "docs.conf"
    assert module_of("README.md") is None
    assert module_of("my-lib/x.py") is None


def test_select_related_tests_ranks_name_then_import_then_package():
    tests = {
        "tests/test_other.py": "from mylib import core\n",  # 直接 import 改动的模块
        "tests/test_pkg.py": "from mylib import helpers\n",  # 只 import 了包
        "tests/test_core.py": "import json\n",  # 同名
        "tests/test_unrelated.py": "import os\n",
        EXAM_PATH: "from mylib.core import parse\n",  # 考卷本身不选
        "tests/conftest.py": "import mylib\n",
    }
    got = select_related_tests([pf("mylib/core.py")], tests, exclude=EXAM_PATH)
    assert got == ["tests/test_core.py", "tests/test_other.py", "tests/test_pkg.py"]
    assert select_related_tests([pf("README.md")], tests, exclude=EXAM_PATH) == []


def test_outcomes_parses_pytest_summary_with_or_without_src_prefix():
    out = (
        "PASSED src/tests/test_a.py::test_ok\n"
        "SKIPPED [1] tests/test_a.py:4: nope\n"
        "XFAIL tests/test_a.py::test_xf\n"
        "FAILED tests/test_a.py::test_bad - assert 1 == 2\n"
        "ERROR src/tests/test_b.py - ModuleNotFoundError\n"
    )
    assert outcomes(out) == {
        "tests/test_a.py::test_ok": "PASSED", "tests/test_a.py": "SKIPPED",
        "tests/test_a.py::test_xf": "XFAIL", "tests/test_a.py::test_bad": "FAILED",
        "tests/test_b.py": "ERROR",
    }


# ---------------------------------------------------------------- 第一层判定


def test_head_pass_requires_the_exam_to_actually_pass():
    e = exam()
    assert classify_head(PASS, e).outcome == "passed"
    assert classify_head(SKIP, e).outcome == "skipped"
    assert classify_head(res(0, "no tests ran"), e).outcome == "skipped"
    assert classify_head(res(5, "no tests collected"), e).outcome == "skipped"
    assert classify_head(res(0, f"XFAIL {EXAM_PATH}::test_parse\n"), e).outcome == "skipped"
    assert classify_head(FAIL, e).outcome == "failed_same"
    assert classify_head(res(2, "ERROR collecting"), e).outcome == "invalid"
    assert classify_head(res(124, "", timed_out=True), e).outcome == "infra"


def runs(*outcomes_: str) -> list[ExamRun]:
    return [ExamRun(exit_code=0, outcome=o) for o in outcomes_]  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("base", "head", "status", "word"),
    [
        (("failed_same", "failed_same"), ("passed", "passed"), "pass", "2/2 次通过"),
        (("failed_same", "failed_same"), ("failed_same", "failed_same"), "fail", "仍然失败"),
        (("failed_same", "failed_same"), ("passed", "skipped"), "fail", "被跳过"),
        (("passed", "passed"), ("passed", "passed"), "inconclusive", "就通过了"),
        (("failed_other", "failed_other"), ("passed", "passed"), "inconclusive", "不是封存时"),
        (("failed_same", "failed_same"), ("passed", "failed_same"), "inconclusive", "时过时不过"),
        (("failed_same", "infra"), ("passed", "passed"), "inconclusive", "超时"),
    ],
)
def test_judge_layer1(base, head, status, word):
    got = judge_layer1(runs(*base), runs(*head))
    assert got.status == status and word in got.reason


# ---------------------------------------------------------------- 编排（假的 Workbench）

BASE, HEAD = "b" * 40, "h" * 40


class FakeBench:
    """exam[提交] / tests[提交] 是依次返回的运行结果；files[提交] 是这个提交上的文件。"""

    def __init__(self, *, exam_runs: dict[str, list[ExecResult]],
                 test_runs: dict[str, list[ExecResult]] | None = None,
                 files: dict[str, dict[str, str]] | None = None,
                 broken: set[str] = frozenset()) -> None:  # type: ignore[assignment]
        self.exam_runs = {k: list(v) for k, v in exam_runs.items()}
        self.test_runs = {k: list(v) for k, v in (test_runs or {}).items()}
        self.files = files or {BASE: {}, HEAD: {}}
        self.broken = broken
        self.test_calls: list[tuple[str, list[str]]] = []

    async def prepare(self, repo: str, sha: str, exam: Exam) -> str:
        if sha in self.broken:
            raise SetupFailed("pip install failed")
        return sha

    def read_files(self, prepared: str, paths: set[str] | None = None) -> dict[str, str]:
        f = self.files.get(prepared, {})
        return dict(f) if paths is None else {p: s for p, s in f.items() if p in paths}

    async def run_exam(self, prepared: str, exam: Exam) -> ExecResult:
        return self.exam_runs[prepared].pop(0)

    async def run_tests(self, prepared: str, targets: list[str], timeout_s: int) -> ExecResult:
        self.test_calls.append((prepared, targets))
        return self.test_runs[prepared].pop(0)


def pull(files: list[PullFile] | None = None) -> PullRequest:
    return PullRequest(repo=REPO, number=12, title="Fix parse", body="Fixes #7", base_sha=BASE,
                       head_sha=HEAD, head_repo="fork/app",
                       files=files if files is not None else [pf("mylib/core.py")])


def verify(bench: FakeBench, pr: PullRequest | None = None, e: Exam | None = None):
    return asyncio.run(ClaimVerifier(bench).verify(pr or pull(), [7], {7: e or exam()}))


GOOD = {BASE: [FAIL, FAIL], HEAD: [PASS, PASS]}


def test_good_fix_is_verified_and_receipt_is_self_consistent():
    v = verify(FakeBench(exam_runs=GOOD))
    assert v.verdict == ClaimVerdict.VERIFIED
    c = v.claims[0]
    assert c.layer3 is not None and c.layer3.status == "none"
    receipt = v.receipt()
    assert receipt["schema"] == "failgate.verify/v1" and check_receipt(receipt) == []
    assert receipt["claims"][0]["exam_receipt_sha256"] == "r" * 64
    body = render_verification(v, "zh")
    assert "✅ 通过验收" in body and "这不等于\"修复一定正确\"" in body
    assert "合并基点 `bbbbbbb`：2/2 封存的失败；PR `hhhhhhh`：2/2 通过" in body
    assert "本地复验：`failgate verify acme/app#12`" in body
    assert json.loads(body.split("```json\n", 1)[1].split("\n```", 1)[0]) == receipt


def test_fix_that_does_not_make_the_exam_pass_is_refuted():
    v = verify(FakeBench(exam_runs={BASE: [FAIL, FAIL], HEAD: [FAIL, FAIL]}))
    assert v.verdict == ClaimVerdict.REFUTED and "仍然失败" in v.claims[0].reasons[0]


def test_skipping_the_exam_is_refuted_even_though_pytest_exits_zero():
    v = verify(FakeBench(exam_runs={BASE: [FAIL, FAIL], HEAD: [SKIP, SKIP]}))
    assert v.verdict == ClaimVerdict.REFUTED and "被跳过" in v.claims[0].reasons[0]


def test_editing_the_exam_is_refuted_even_if_it_would_pass():
    files = {BASE: {}, HEAD: {EXAM_PATH: "def test_parse():\n    pass\n"}}
    v = verify(FakeBench(exam_runs=GOOD, files=files),
               pull([pf("mylib/core.py"), pf(EXAM_PATH, "added")]))
    assert v.verdict == ClaimVerdict.REFUTED
    assert "篡改：head 上的考卷和封存的版本不一致" in v.claims[0].reasons


def test_conftest_change_is_flagged_but_not_refuted():
    v = verify(FakeBench(exam_runs=GOOD), pull([pf("mylib/core.py"), pf("tests/conftest.py")]))
    assert v.verdict == ClaimVerdict.VERIFIED
    assert "需要维护者留意" in render_verification(v, "zh")


def test_already_fixed_on_base_is_inconclusive():
    v = verify(FakeBench(exam_runs={BASE: [PASS, PASS], HEAD: [PASS, PASS]}))
    assert v.verdict == ClaimVerdict.INCONCLUSIVE and "合并基点上就通过了" in v.claims[0].reasons[0]


def test_no_sealed_exam_and_broken_environment_are_inconclusive():
    v = asyncio.run(ClaimVerifier(FakeBench(exam_runs={})).verify(pull(), [7], {7: None}))
    assert v.verdict == ClaimVerdict.INCONCLUSIVE and "没有封存的考卷" in v.claims[0].reasons[0]
    v = verify(FakeBench(exam_runs={}, broken={HEAD}))
    assert v.verdict == ClaimVerdict.INCONCLUSIVE
    assert "PR 的代码上环境搭不起来：pip install failed" in v.claims[0].reasons[0]


def test_pr_without_claims_has_no_verdict():
    v = asyncio.run(ClaimVerifier(FakeBench(exam_runs={})).verify(pull(), [], {}))
    assert v.verdict is None and "没有声明修复任何 issue" in render_verification(v, "zh")


RELATED = {"tests/test_core.py": "from mylib.core import parse\n", EXAM_PATH: EXAM_CODE}


def test_new_failure_in_related_tests_is_refuted_after_a_rerun():
    broke = res(1, "FAILED tests/test_core.py::test_name - KeyError\n")
    bench = FakeBench(
        exam_runs=GOOD, files={BASE: RELATED, HEAD: RELATED},
        test_runs={BASE: [res(0, "3 passed")], HEAD: [broke, broke]},
    )
    v = verify(bench)
    c = v.claims[0]
    assert v.verdict == ClaimVerdict.REFUTED and c.layer3 is not None
    assert c.layer3.new_failures == ["tests/test_core.py::test_name"]
    # 考卷不在相关测试里；重跑只跑新出现的失败
    assert bench.test_calls == [(HEAD, ["tests/test_core.py"]), (BASE, ["tests/test_core.py"]),
                                (HEAD, ["tests/test_core.py::test_name"])]


def test_failure_already_on_base_or_gone_on_rerun_is_not_a_regression():
    old = res(1, "FAILED tests/test_core.py::test_old - x\n")
    bench = FakeBench(
        exam_runs=GOOD, files={BASE: RELATED, HEAD: RELATED},
        test_runs={BASE: [old],
                   HEAD: [res(1, "FAILED tests/test_core.py::test_old - x\n"
                                 "FAILED tests/test_core.py::test_flaky - x\n"), res(0, "passed")]},
    )
    v = verify(bench)
    assert v.verdict == ClaimVerdict.VERIFIED
    assert v.claims[0].layer3 is not None and v.claims[0].layer3.status == "pass"


def test_related_test_added_by_the_pr_only_runs_on_head():
    head = {**RELATED, "tests/test_new.py": "import mylib.core\n"}
    bench = FakeBench(exam_runs=GOOD, files={BASE: RELATED, HEAD: head},
                      test_runs={BASE: [res(0, "")], HEAD: [res(0, "")]})
    verify(bench)
    assert bench.test_calls == [(HEAD, ["tests/test_core.py", "tests/test_new.py"]),
                                (BASE, ["tests/test_core.py"])]


# ---------------------------------------------------------------- 真实 Docker：fixture 仓库

FIXTURE = Path(__file__).parent.parent / "fixtures" / "repos" / "bug-keyerror"
KEYERROR_TEST = (
    "from confkit import parse\n\n\n"
    "def test_keys_before_first_section_go_to_default():\n"
    '    assert parse("name = demo\\n[server]\\nhost = x\\n")["DEFAULT"] == {"name": "demo"}\n'
)


@pytest.fixture(scope="module")
def sandbox() -> DockerSandbox:
    sb = DockerSandbox()
    if asyncio.run(sb.server_version()) is None:
        pytest.skip("Docker 不可用")
    return sb


@pytest.mark.docker
def test_real_verification_of_fixture_fix(sandbox: DockerSandbox, tmp_path: Path):
    """有 bug 的代码当合并基点，打上 fix/ 当 PR：考卷 base 失败、head 通过，相关测试没有回归。"""
    from test_repro_l2 import OfflinePyPI

    from failgate.repro.envcache import EnvCache
    from failgate.repro.l2 import TestReproducer
    from failgate.repro.source import SourceTree, pack_dir
    from failgate.verify.workbench import SandboxWorkbench

    base_dir = tmp_path / "base"
    shutil.copytree(FIXTURE / "repo", base_dir)
    head_dir = tmp_path / "head"
    shutil.copytree(base_dir, head_dir)
    shutil.copytree(FIXTURE / "fix", head_dir, dirs_exist_ok=True)
    trees = {
        sha: SourceTree(repo="fixture/keyerror", sha=sha, committed_at=None, tarball=pack_dir(d))
        for sha, d in ((BASE, base_dir), (HEAD, head_dir))
    }

    async def fetch(repo: str, sha: str) -> SourceTree:
        return trees[sha]

    tester = TestReproducer(sandbox, EnvCache(sandbox, tmp_path / "envcache.json"),
                            OfflinePyPI())  # type: ignore[arg-type]
    bench = SandboxWorkbench(fetch, tester)
    e = exam(test_path="tests/test_failgate_issue_101.py", code=KEYERROR_TEST,
             test_sha256=code_sha256(KEYERROR_TEST), package="confkit", module="confkit",
             signature=None, pytest=None, version=None)
    pr = pull([pf("confkit/parser.py")])
    v = asyncio.run(ClaimVerifier(bench).verify(pr, [7], {7: e}))
    c = v.claims[0]
    assert c.layer1 is not None and c.layer3 is not None
    assert v.verdict == ClaimVerdict.VERIFIED, (c.reasons, c.layer1, c.layer3)
    assert c.layer3.files == ["tests/test_parser.py"]

    # PR 没修代码（head 和 base 一样）：考卷在 head 上仍失败 → 驳回
    trees[HEAD] = trees[BASE]
    v = asyncio.run(ClaimVerifier(bench).verify(pr, [7], {7: e}))
    assert v.verdict == ClaimVerdict.REFUTED


# ---------------------------------------------------------------- 从 evidence 表取考卷


async def test_latest_exam_takes_the_newest_unsuperseded_acceptance_test(tmp_path):
    from conftest import REPO as HARNESS_REPO
    from sqlalchemy import select
    from test_repro_fixtures import TEST_CODE, l2_report, run_source_issue
    from test_repro_pipeline import FakeRunner

    from failgate.db import Evidence
    from failgate.verify.store import latest_exam

    async for h in run_source_issue(FakeRunner(l2_report()), tmp_path):  # type: ignore[arg-type]
        db = h.failgate.db
        async with db.session() as s:
            got = await latest_exam(s, HARNESS_REPO, 1)
            assert await latest_exam(s, HARNESS_REPO, 2) is None
            ev = (await s.scalars(select(Evidence))).one()
        assert got is not None and got.evidence_id == ev.id and got.code == TEST_CODE
        assert (got.test_path, got.module, got.python, got.pytest) == (
            "tests/test_failgate_issue_1.py", "mylib", "3.12", "pytest==8.0.0")
        assert got.receipt_sha256 == ev.receipt_sha256
        async with db.session() as s, s.begin():
            row = await s.get(Evidence, ev.id)
            assert row is not None
            row.superseded_by = "f" * 32  # 被重新封存取代之后，不再是有效的考卷
        async with db.session() as s:
            assert await latest_exam(s, HARNESS_REPO, 1) is None
