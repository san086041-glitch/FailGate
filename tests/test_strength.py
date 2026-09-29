"""考卷强度（ADR 0020）：改动行、变异体生成与抽样、运行结果分类、编排、报告。

编排用"进程内"的假沙箱：变异体真的被执行（exec 源码再跑考卷函数），只是不起容器。
真实 Docker 的端到端在文件末尾（fixture 仓库 keyerror 的修复）。
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from test_verify import BASE, FAIL, HEAD, PASS, FakeBench, exam, pf, pull

from failgate.repro.sandbox import DockerSandbox, ExecResult, SandboxError
from failgate.verify.engine import ClaimVerdict, ClaimVerifier
from failgate.verify.report import render_verification
from failgate.verify.strength import (
    COVERAGE_MARK,
    MutantResult,
    StrengthEvaluator,
    changed_lines,
    classify_mutant,
    executed_in_copy,
    grade,
    import_root,
    is_source_file,
    mutants_for_file,
    parse_coverage,
    sample,
    summarize,
)

BASE_CODE = 'def parse(d, key="name"):\n    return d[key]\n'
HEAD_CODE = (
    'def parse(d, key="name"):\n'
    "    # 缺键时返回 None\n"
    "    if key not in d:\n"
    "        return None\n"
    "    return d[key]\n"
)
PATH = "mylib/core.py"


def res(exit_code: int, out: str = "", **kw: Any) -> ExecResult:
    return ExecResult(phase="run", argv=["python"], exit_code=exit_code, stdout=out, **kw)


# ---------------------------------------------------------------- 改动行、源文件


def test_changed_lines_counts_inserted_and_replaced_code_but_not_comments_or_blanks():
    assert changed_lines(BASE_CODE, HEAD_CODE) == {3, 4}  # 第 2 行是注释
    assert changed_lines(None, "x = 1\n\ny = 2\n") == {1, 3}
    assert changed_lines(HEAD_CODE, HEAD_CODE) == set()
    # 改写一行
    assert changed_lines("a = 1\nb = 2\n", "a = 1\nb = 3\n") == {2}


@pytest.mark.parametrize(
    ("path", "ok"),
    [("mylib/core.py", True), ("src/black/linegen.py", True), ("tests/test_core.py", False),
     ("mylib/conftest.py", False), ("tests/helpers.py", False), ("docs/conf.py", False),
     ("setup.py", False), ("mylib/data.json", False)],
)
def test_is_source_file(path: str, ok: bool):
    assert is_source_file(path) is ok


def test_import_root():
    assert import_root("src/black/linegen.py") == "src"
    assert import_root("failgate_demo/text.py") == ""


# ---------------------------------------------------------------- 变异体


def test_mutants_only_touch_target_lines_and_keep_indentation():
    ms = mutants_for_file(PATH, HEAD_CODE, {3, 4}, python="3.12")
    assert ms
    assert {m.line for m in ms} <= {3, 4}
    ops = {m.operator for m in ms}
    assert {"ReturnNone", "AddNot"} & ops or "DeleteStatement" in ops
    for m in ms:
        # 变异后的整个文件仍能编译，未改动的行保持原样
        compile(m.code, PATH, "exec")
        old, new = HEAD_CODE.splitlines(), m.code.splitlines()
        assert [i for i, (a, b) in enumerate(zip(old, new, strict=False)) if a != b] == [m.line - 1]
    # 不产生重复的变异体
    assert len({m.code for m in ms}) == len(ms)


def test_curated_operators_skip_type_error_floods():
    code = 'def f(sep):\n    return sep * 3\n'
    ms = mutants_for_file("m/x.py", code, {2}, python="3.12") or []
    ops = {m.operator for m in ms}
    # 字符串乘法上不再生成 sep + 3、sep | 3 这类必然崩溃的替换
    floor = "ReplaceBinaryOperator_Mul_FloorDiv"
    assert not any(o.startswith("ReplaceBinaryOperator_Mul_") and o != floor
                   for o in ops)
    assert "NumberReplacer" in ops


def test_delete_statement_and_return_none():
    code = "def f(x):\n    y = x + 1\n    return y\n"
    ms = {m.operator: m for m in mutants_for_file("m/x.py", code, {2, 3}) or []}
    assert ms["DeleteStatement"].after == "pass"
    assert "    pass" in ms["DeleteStatement"].code
    assert ms["ReturnNone"].after == "return None"


def test_bare_annotations_are_not_deleted():
    code = "class A:\n    gen: int\n    n: int = 1\n"
    ops = [(m.line, m.operator) for m in mutants_for_file("m/x.py", code, {2, 3}) or []]
    assert (2, "DeleteStatement") not in ops
    assert (3, "DeleteStatement") in ops


def test_unparsable_source_returns_none():
    assert mutants_for_file("m/x.py", "def (:\n", {1}) is None


def test_sample_is_deterministic_and_round_robins_lines():
    code = "\n".join(f"x{i} = {i} + 1" for i in range(10)) + "\n"
    pool = mutants_for_file("m/x.py", code, set(range(1, 11))) or []
    a = sample(pool, 5, "seed")
    assert a == sample(pool, 5, "seed")
    assert len({m.line for m in a}) == 5  # 先每行各挑一个
    assert len(sample(pool, 1000, "seed")) == len(pool)


# ---------------------------------------------------------------- 运行结果


def test_classify_mutant():
    assert classify_mutant(res(0)) == "survived"
    assert classify_mutant(res(1, "E   AssertionError: assert 1 == 2")) == "killed_assert"
    assert classify_mutant(res(1, "TypeError: unsupported operand")) == "killed_crash"
    assert classify_mutant(res(124, timed_out=True)) == "killed_timeout"
    assert classify_mutant(res(2, "ERROR collecting ... NameError")) == "killed_crash"
    assert classify_mutant(res(5)) == "invalid"
    assert classify_mutant(res(137, oom_killed=True)) == "invalid"


def test_coverage_parsing_and_shadow_detection():
    cov_run = res(0, "1 passed\n" + COVERAGE_MARK + json.dumps({
        "/workspace/src/src/black/a.py": [1, 2],
        "/opt/venv/lib/python3.12/site-packages/black/b.py": [5],
    }))
    cov = parse_coverage(cov_run)
    assert cov is not None
    assert executed_in_copy(cov, "src/black/a.py") == {1, 2}
    assert executed_in_copy(cov, "src/black/b.py") is None  # 跑的是 site-packages 里的
    assert executed_in_copy(cov, "src/black/c.py") == set()  # 根本没执行到
    assert parse_coverage(res(0, "no marker")) is None


def test_grade_and_summary():
    assert grade(None) == "n/a"
    assert [grade(x) for x in (0.8, 0.79, 0.5, 0.49)] == ["strong", "medium", "medium", "weak"]
    rs = [MutantResult(path="a", line=1, operator="x", before="", after="", outcome=o)
          for o in ("killed_assert", "killed_crash", "survived", "invalid")]
    s = summarize(rs)
    assert (s.killed, s.survived, s.invalid, s.kill_rate, s.grade) == (2, 1, 1, 0.6667, "medium")
    assert summarize([rs[3]]).status == "n/a"


# ---------------------------------------------------------------- 编排（进程内假沙箱）


class InProcBench:
    """workspace 是工作区里的源码；run_mutant 真的 exec 当前源码并执行考卷函数。"""

    def __init__(self, exam_fn: str, *, executed: dict[str, list[int]] | None = None,
                 baseline_exit: int = 0, setup_broken: bool = False) -> None:
        self.exam_fn = exam_fn
        self.executed = executed
        self.baseline_exit = baseline_exit
        self.setup_broken = setup_broken
        self.workspace = {PATH: HEAD_CODE}
        self.puts: list[str] = []
        self.closed = False
        self.pythonpaths: set[str] = set()

    async def prepare_strength(self, repo: str, sha: str, exam: Any) -> str:
        if self.setup_broken:
            raise RuntimeError("pip install coverage failed")
        return sha

    async def open_strength(self, prepared: Any, exam: Any) -> str:
        return "ws"

    async def run_coverage(self, handle: Any, exam: Any, pythonpath: str, watch: list[str]
                           ) -> ExecResult:
        self.pythonpaths.add(pythonpath)
        self.watch = watch
        executed = self.executed or {f"/workspace/src/{PATH}": [1, 3, 4, 5]}
        return res(self.baseline_exit, COVERAGE_MARK + json.dumps(executed), duration_s=0.5)

    async def put_file(self, handle: Any, path: str, content: str) -> None:
        self.puts.append(path)
        self.workspace[path] = content

    async def run_mutant(self, handle: Any, exam: Any, pythonpath: str, timeout_s: int
                         ) -> ExecResult:
        ns: dict[str, Any] = {}
        try:
            exec(self.workspace[PATH], ns)  # noqa: S102 — 测试里执行自己写的代码
            exec(self.exam_fn, ns)  # noqa: S102
            ns["exam"]()
        except AssertionError as e:
            return res(1, f"AssertionError: {e}")
        except Exception as e:  # noqa: BLE001
            return res(1, f"{type(e).__name__}: {e}")
        return res(0, "1 passed")

    async def close_strength(self, handle: Any) -> None:
        self.closed = True


STRONG_EXAM = "def exam():\n    assert parse({}) is None\n    assert parse({'name': 1}) == 1\n"
# 只断言"不抛异常"：修复被改坏成返回别的值也察觉不到
WEAK_EXAM = "def exam():\n    parse({})\n"


def evaluate(bench: InProcBench, sources: dict[str, tuple[str | None, str]] | None = None):
    ev = StrengthEvaluator(bench)  # type: ignore[arg-type]
    return asyncio.run(ev.evaluate("acme/app", HEAD, exam(),
                                   sources or {PATH: (BASE_CODE, HEAD_CODE)}))


def test_strong_exam_kills_mutants_and_files_are_restored():
    bench = InProcBench(STRONG_EXAM)
    s = evaluate(bench)
    assert s.status == "ok", s
    assert s.changed_lines == 2 and s.executed_lines == 2
    assert s.kill_rate == 1.0 and s.grade == "strong"
    assert bench.workspace[PATH] == HEAD_CODE  # 每个变异体跑完都换回原文件
    assert bench.closed
    assert bench.pythonpaths == {"/workspace/src"}
    assert bench.watch == [PATH]  # 平铺布局：副本和 site-packages 里的相对路径相同


def test_weak_exam_leaves_survivors():
    s = evaluate(InProcBench(WEAK_EXAM))
    assert s.status == "ok"
    assert s.survived > 0 and s.kill_rate is not None and s.kill_rate < 1.0
    assert all(m.outcome != "invalid" for m in s.mutants)
    assert s.survivors[0].path == PATH


@pytest.mark.parametrize(
    ("bench", "sources", "reason"),
    [
        (InProcBench(STRONG_EXAM), {"tests/test_x.py": (None, "x = 1\n")}, "no_source_change"),
        (InProcBench(STRONG_EXAM, setup_broken=True), None, "setup"),
        (InProcBench(STRONG_EXAM, baseline_exit=1), None, "baseline_failed"),
        (InProcBench(STRONG_EXAM,
                     executed={"/usr/lib/python3.12/site-packages/mylib/core.py": [3]}),
         None, "shadow"),
        (InProcBench(STRONG_EXAM, executed={"/workspace/src/mylib/core.py": [1]}), None,
         "not_executed"),
    ],
)
def test_not_assessable_cases_say_why(bench, sources, reason):
    s = evaluate(bench, sources)
    assert (s.status, s.reason) == ("n/a", reason)


def test_unexecuted_changed_lines_are_listed():
    s = evaluate(InProcBench(STRONG_EXAM, executed={f"/workspace/src/{PATH}": [1, 3, 5]}))
    assert s.executed_lines == 1 and s.unexecuted == [f"{PATH}:4"]


# ---------------------------------------------------------------- 接进 ClaimVerify


class StrengthFakeBench(FakeBench, InProcBench):
    def __init__(self, exam_runs: dict[str, list[ExecResult]]) -> None:
        FakeBench.__init__(self, exam_runs=exam_runs,
                           files={BASE: {PATH: BASE_CODE}, HEAD: {PATH: HEAD_CODE}})
        InProcBench.__init__(self, WEAK_EXAM)


def test_strength_is_attached_only_when_layer1_passes_and_never_changes_the_verdict():
    pr = pull([pf(PATH)])
    bench = StrengthFakeBench({BASE: [FAIL, FAIL], HEAD: [PASS, PASS]})
    v = asyncio.run(ClaimVerifier(bench, strength=True).verify(pr, [7], {7: exam()}))
    c = v.claims[0]
    assert v.verdict == ClaimVerdict.VERIFIED
    assert c.strength is not None and c.strength.status == "ok" and c.strength.survived > 0
    zh = render_verification(v, "zh")
    assert "④ 考卷强度" in zh and "考卷察觉不到的改动" in zh
    en = render_verification(v, "en")
    assert "④ Test strength" in en
    assert v.receipt()["claims"][0]["strength"]["kill_rate"] == c.strength.kill_rate

    bench = StrengthFakeBench({BASE: [FAIL, FAIL], HEAD: [FAIL, FAIL]})
    v = asyncio.run(ClaimVerifier(bench, strength=True).verify(pr, [7], {7: exam()}))
    assert v.verdict == ClaimVerdict.REFUTED and v.claims[0].strength is None

    # 默认不算强度（旧的调用方不受影响）
    bench = StrengthFakeBench({BASE: [FAIL, FAIL], HEAD: [PASS, PASS]})
    v = asyncio.run(ClaimVerifier(bench).verify(pr, [7], {7: exam()}))
    assert v.claims[0].strength is None


def test_report_escapes_backticks_in_survivor_lines():
    from failgate.verify.report import _code

    assert "`" not in _code("x = `rm -rf`")
    assert _code("y" * 200).endswith("...")


# ---------------------------------------------------------------- 沙箱的环境变量白名单


@pytest.mark.parametrize("kv", ["LD_PRELOAD=/x.so", "PYTHONPATH=/etc",
                                "PYTHONPATH=/workspace/../etc", "PYTHONPATH=/workspace/src/..",
                                "PYTHONPATH=/workspace/src:/etc", "PYTHONSTARTUP=/workspace/x.py",
                                "PYTHONPATH=/workspace;id"])
def test_run_rejects_env_outside_whitelist(kv: str):
    from failgate.repro.sandbox import RUN_ENV

    assert RUN_ENV.fullmatch("PYTHONPATH=/workspace/src/src:/workspace/src")
    assert RUN_ENV.fullmatch("PYTHONPATH=/workspace/src/.hidden")
    with pytest.raises(SandboxError):
        asyncio.run(DockerSandbox().run("img", "vol", ["python", "-m", "pytest"], env=[kv]))


# ---------------------------------------------------------------- 真实 Docker


@pytest.fixture(scope="module")
def sandbox() -> DockerSandbox:
    sb = DockerSandbox()
    if asyncio.run(sb.server_version()) is None:
        pytest.skip("Docker 不可用")
    return sb


@pytest.mark.docker
def test_real_strength_on_fixture_fix(sandbox: DockerSandbox, tmp_path: Path):
    """fixture keyerror：有 bug 的代码当 base，打上 fix/ 当 PR。强度能算出来，
    而且变异体确实注入到了测试导入的模块里（coverage 确认执行的是工作区副本）。"""
    from test_repro_l2 import OfflinePyPI, rel
    from test_verify import FIXTURE, KEYERROR_TEST

    from failgate.repro.envcache import EnvCache
    from failgate.repro.l2 import TestReproducer
    from failgate.repro.source import SourceTree, pack_dir
    from failgate.verify.receipt import code_sha256
    from failgate.verify.workbench import SandboxWorkbench

    class PyPI(OfflinePyPI):
        async def releases(self, name: str):  # type: ignore[no-untyped-def]
            if name == "coverage":
                return dict([rel("7.6.1", "2024-08-04", ">=3.8")])
            return await super().releases(name)

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
                            PyPI())  # type: ignore[arg-type]
    bench = SandboxWorkbench(fetch, tester)
    e = exam(test_path="tests/test_failgate_issue_101.py", code=KEYERROR_TEST,
             test_sha256=code_sha256(KEYERROR_TEST), package="confkit", module="confkit",
             signature=None, pytest=None, version=None)
    v = asyncio.run(ClaimVerifier(bench, strength=True, max_mutants=8).verify(
        pull([pf("confkit/parser.py")]), [7], {7: e}))
    c = v.claims[0]
    assert v.verdict == ClaimVerdict.VERIFIED, c.reasons
    s = c.strength
    assert s is not None and s.status == "ok", s
    assert s.executed_lines > 0 and s.killed > 0
    assert s.invalid < len(s.mutants)
