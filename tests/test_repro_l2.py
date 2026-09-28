"""L2（仓库内的失败测试）：pytest 退出码判定、pytest 版本选择、测试会话、L2 回放汇总。

用假沙箱和剧本式 LLM 测控制流；真实 Docker 的集成测试在最后（本地 fixture 目录）。
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version
from test_repro_agent import REPORTED, TASK, ScriptedLLM, call
from test_repro_source import make_tar, tree_of, write_project

from failgate.replay import l2 as l2replay
from failgate.replay.fbpa import FbpaCase, candidate_from_l2
from failgate.repro.agent import TEST_TOOLS, ReproAgent, reproduce_with_tests
from failgate.repro.config import PackageConfig
from failgate.repro.envcache import Env, EnvCache
from failgate.repro.evidence import EvidenceLevel
from failgate.repro.issue import L2IssueReport
from failgate.repro.judge import VerdictKind
from failgate.repro.l2 import (
    PREPARE_ARGV,
    PROBE_TEST,
    RUN_PREFIXES,
    L2Unsupported,
    SourcePrepared,
    SourceRepro,
    TestReproducer,
    invalid_run,
    pick_pytest,
    pytest_argv,
    repo_test_file,
)
from failgate.repro.pypi import PackageNotFound, Release
from failgate.repro.sandbox import DockerSandbox, ExecResult, SandboxError, check_command
from failgate.repro.source import SRC_DIR, pack_dir

# ---------------------------------------------------------------- 纯函数


def res(code: int, stderr: str = "", **kw: Any) -> ExecResult:
    return ExecResult(phase="run", argv=[], exit_code=code, stderr=stderr, **kw)


@pytest.mark.parametrize("code", [2, 3, 4, 5])
def test_pytest_exit_codes_other_than_1_are_invalid(code: int):
    v = invalid_run(res(code))
    assert v is not None and v.kind == VerdictKind.UNRELATED_FAILURE
    assert f"退出码 {code}" in v.reason


@pytest.mark.parametrize("run", [res(0), res(1), res(124, timed_out=True)])
def test_normal_or_infra_runs_go_to_the_judge(run: ExecResult):
    assert invalid_run(run) is None


def rel(v: str, day: str, requires: str | None = None) -> tuple[Version, Release]:
    return Version(v), Release(
        version=Version(v), uploaded=datetime.fromisoformat(day).replace(tzinfo=UTC),
        requires_python=SpecifierSet(requires) if requires else None, yanked=False,
    )


def test_pick_pytest_time_travels_and_respects_python():
    releases = dict([
        rel("7.4.3", "2023-10-24", ">=3.7"), rel("8.0.0rc1", "2023-11-01", ">=3.8"),
        rel("7.4.4", "2023-12-31", ">=3.7"), rel("8.3.0", "2024-07-20", ">=3.8"),
        rel("9.0.0", "2025-11-01", ">=3.10"),
    ])
    day = datetime(2023, 11, 21, tzinfo=UTC)
    assert pick_pytest(releases, "3.12", day) == "pytest==7.4.3"  # 预发布和之后的都不算
    assert pick_pytest(releases, "3.9", None) == "pytest==8.3.0"  # 9.0 不支持 3.9
    assert pick_pytest({}, "3.12", day) == "pytest"


def test_repo_test_file_uses_repo_test_dir(tmp_path: Path):
    root = write_project(tmp_path / "p")
    (root / "test").mkdir()
    (root / "test" / "test_a.py").write_text("", encoding="utf-8")
    assert repo_test_file(tree_of(pack_dir(root)), 42) == "test/test_failgate_issue_42.py"
    bare = tree_of(make_tar({"t/src/x.py": b""}))
    assert repo_test_file(bare, None) == "tests/test_failgate_issue_0.py"


def test_run_whitelist_is_exact_for_copy_and_pytest_prefix():
    check_command(PREPARE_ARGV, RUN_PREFIXES)
    check_command(pytest_argv("tests/test_failgate_issue_1.py"), RUN_PREFIXES)
    for bad in (["python", "-c", "import os"], ["python", "x.py"], ["sh", "-c", "id"]):
        with pytest.raises(SandboxError):
            check_command(bad, RUN_PREFIXES)
    argv = pytest_argv("tests/test_failgate_issue_1.py")
    assert "--tb=native" in argv and "no:cacheprovider" in argv  # 签名解析和只读根文件系统都靠它们


# ---------------------------------------------------------------- 假沙箱上的 TestReproducer


TB_OUT = (
    "Traceback (most recent call last):\n"
    '  File "/workspace/src/tests/test_failgate_issue_7.py", line 3, in test_x\n'
    "    mylib.parse({})\n"
    '  File "/opt/venv/lib/python3.12/site-packages/mylib/core.py", line 9, in parse\n'
    "    return d[key]\nKeyError: 'name'\n"
)


class FakeTestSandbox:
    """PREPARE 总是成功；pytest 的结果按写入的测试内容决定。"""

    def __init__(self, outcomes: dict[str, ExecResult] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.files: dict[str, str] = {}
        self.runs: list[list[str]] = []
        self.volumes = 0

    async def create_workspace(self, key: str) -> str:
        self.volumes += 1
        return "ws"

    async def remove_workspace(self, volume: str) -> None:
        self.volumes -= 1

    async def copy_in(self, volume: str, src: Path, image: str) -> None:
        self.files.update(read_tree(src))

    async def run(self, image, volume, argv, *, allowed=(), **_: Any) -> ExecResult:
        check_command(argv, allowed or RUN_PREFIXES)
        self.runs.append(list(argv))
        if argv == PREPARE_ARGV:
            return res(0)
        if argv[:3] == ["python", "-c", argv[2]] and SRC_DIR in argv:  # CodeTools
            return ExecResult(phase="run", argv=list(argv), exit_code=0,
                              stdout='FAILGATE_TOOL{"hits": ["tests/test_core.py:1: import mylib"],'
                                     ' "truncated": false}')
        content = next(v for k, v in self.files.items() if k.endswith(".py"))
        for marker, outcome in self.outcomes.items():
            if marker in content:
                return outcome
        return res(0)


def read_tree(src: Path) -> dict[str, str]:
    out = {}
    for dirpath, _, names in os.walk(src):
        for n in names:
            full = Path(dirpath, n)
            out[full.relative_to(src).as_posix()] = full.read_text(encoding="utf-8")
    return out


def fake_prepared(tmp_path: Path) -> SourcePrepared:
    tree = tree_of(pack_dir(write_project(tmp_path / "p")))
    return SourcePrepared(
        cfg=PackageConfig(name="mylib"), tree=tree, python="3.12", version="1.0.1.dev0",
        pytest="pytest==8.0.0", env=Env(key="k" * 64, image="failgate-env:k", python="3.12",
                                        cache_hit=True),
        test_path="tests/test_failgate_issue_7.py",
    )


def make_tester(sb: FakeTestSandbox) -> TestReproducer:
    return TestReproducer(sb, None, None)  # type: ignore[arg-type]


async def test_evaluate_writes_test_into_repo_copy_and_judges(tmp_path: Path):
    sb = FakeTestSandbox({"BUG": res(1, TB_OUT), "IMPORTERR": res(2, "ImportError")})
    tester, prep = make_tester(sb), fake_prepared(tmp_path)

    run = await tester.evaluate(prep, "# BUG\nimport mylib", reported_traceback=REPORTED)
    assert run.verdict.kind == VerdictKind.REPRODUCED and run.verdict.runs == 4
    assert "src/tests/test_failgate_issue_7.py" in sb.files
    assert sb.runs[0] == PREPARE_ARGV  # 先放源码副本，再跑测试
    assert sb.runs[1] == pytest_argv("tests/test_failgate_issue_7.py")

    bad = await tester.evaluate(prep, "# IMPORTERR", reported_traceback=REPORTED)
    assert bad.verdict.kind == VerdictKind.UNRELATED_FAILURE and "退出码 2" in bad.verdict.reason

    ok = await tester.evaluate(prep, "# fine", reported_traceback=REPORTED)
    assert ok.verdict.kind == VerdictKind.NOT_REPRODUCED
    assert sb.volumes == 0  # 工作区都删掉了


# ---------------------------------------------------------------- 测试会话（Agent）


async def test_agent_writes_runs_and_submits_a_test(tmp_path: Path):
    sb = FakeTestSandbox({"BUG": res(1, TB_OUT)})
    tester, prep = make_tester(sb), fake_prepared(tmp_path)
    llm = ScriptedLLM([
        {"tool_calls": [call("search_code", {"pattern": "import mylib", "path": "tests"})]},
        {"tool_calls": [call("submit", {"claim": "还没写"})]},
        {"tool_calls": [call("write_test", {"content": "import mylib\n# BUG\n"})]},
        {"tool_calls": [call("run_test", {})]},
        {"tool_calls": [call("submit", {"claim": "parse 抛 KeyError"})]},
    ])
    agent = ReproAgent(llm.client(), "deepseek-flash", tester, max_steps=10)
    result = await agent.run(TASK, prep)
    assert result.status == "reproduced" and len(result.attempts) == 1
    assert result.final_script == "import mylib\n# BUG\n"
    assert result.test_path == "tests/test_failgate_issue_7.py"
    # 用的是测试会话的工具和提示词
    names = [t["function"]["name"] for t in llm.requests[0]["tools"]]
    assert names == [t["function"]["name"] for t in TEST_TOOLS]
    assert "pytest 测试文件" in llm.requests[0]["messages"][0]["content"]
    assert "tests/test_failgate_issue_7.py" in llm.requests[0]["messages"][1]["content"]
    # 代码工具看的是仓库源码树；没写测试就提交会被挡回
    assert any(SRC_DIR in argv for argv in sb.runs)
    tool_msgs = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] == "tool"]
    assert "还没有测试文件" in tool_msgs[1]
    assert "exit=1" in tool_msgs[3]
    assert sb.volumes == 0


async def test_run_test_explains_invalid_exit_codes(tmp_path: Path):
    sb = FakeTestSandbox({"SYNTAX": res(2, "SyntaxError")})
    llm = ScriptedLLM([
        {"tool_calls": [call("write_test", {"content": "# SYNTAX"})]},
        {"tool_calls": [call("run_test", {})]},
        {"tool_calls": [call("give_up", {"reason": "写不出来"})]},
    ])
    agent = ReproAgent(llm.client(), "deepseek-flash", make_tester(sb), max_steps=5)
    result = await agent.run(TASK, fake_prepared(tmp_path))
    assert result.status == "gave_up"
    tool_msgs = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] == "tool"]
    assert "判定器会判为无关失败" in tool_msgs[1]


class PreparingTester:
    """reproduce_with_tests 用：prepare 可以失败或返回假的环境。"""

    def __init__(self, sb: FakeTestSandbox, prepared: SourcePrepared | None) -> None:
        self.inner = make_tester(sb)
        self.prepared = prepared

    async def prepare(self, cfg, tree, *, number, python=None, **_: Any) -> SourcePrepared:
        if self.prepared is None:
            raise L2Unsupported("空测试在这个环境里跑不通")
        return self.prepared

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


async def test_reproduce_with_tests_reports_l2_or_setup_error(tmp_path: Path):
    prep = fake_prepared(tmp_path)
    sb = FakeTestSandbox({"BUG": res(1, TB_OUT)})
    llm = ScriptedLLM([
        {"tool_calls": [call("write_test", {"content": "# BUG"})]},
        {"tool_calls": [call("submit", {"claim": "x"})]},
    ])
    agent = ReproAgent(llm.client(), "m", PreparingTester(sb, prep))  # type: ignore[arg-type]
    out, result = await reproduce_with_tests(agent, prep.cfg, TASK, prep.tree)
    assert out.level == EvidenceLevel.L2 and result is not None
    assert (out.python, out.pytest, out.test_path) == ("3.12", "pytest==8.0.0", prep.test_path)

    agent = ReproAgent(ScriptedLLM([]).client(), "m", PreparingTester(sb, None))  # type: ignore[arg-type]
    out, result = await reproduce_with_tests(agent, prep.cfg, TASK, prep.tree)
    assert result is None and out.level == EvidenceLevel.NONE and "跑不通" in (out.error or "")


# ---------------------------------------------------------------- L2 回放汇总


def l2_report(number: int, *, level: str = "L2", status: str = "reproduced",
              tb: bool = True, error: str | None = None) -> L2IssueReport:
    from failgate.repro.agent import AgentResult
    from failgate.repro.judge import Verdict
    from failgate.repro.package import VersionRun

    run = VersionRun(version="abc", python="3.12", env_key="k", cache_hit=True,
                     verdict=Verdict(kind=VerdictKind.REPRODUCED, reason="ok"))
    src = SourceRepro(repo="o/r", sha="a" * 40, package="mylib", module="mylib",
                      python="3.12", pytest="pytest==8.0.0", test_path="tests/t.py",
                      level=EvidenceLevel(level), run=run if level == "L2" else None, error=error)
    agent = None if error else AgentResult(status=status, final_script="# t", cost_usd=0.01)  # type: ignore[arg-type]
    return L2IssueReport(repo="o/r", number=number, title=f"issue {number}",
                         intake_version="1.0", intake_python=None, has_traceback=tb,
                         source=src, agent=agent, intake_cost_usd=0.001, judge_cost_usd=0.0)


def test_l2_replay_summary_and_render():
    reports = [
        l2_report(1), l2_report(2), l2_report(3, level="NONE", status="gave_up", tb=False),
        l2_report(4, level="NONE", error="空测试在这个环境里跑不通"),
    ]
    assert [l2replay.outcome(r) for r in reports] == ["l2", "l2", "gave_up", "setup_failed"]
    cand = candidate_from_l2(reports[0])
    assert cand.code == "# t" and cand.python == "3.12" and cand.proxy == "—"
    assert candidate_from_l2(reports[2]).code is None
    cases = [
        FbpaCase(number=1, title="a", proxy="—", outcome="fb_pa"),
        FbpaCase(number=2, title="b", proxy="—", outcome="fail_after"),
        FbpaCase(number=3, title="c", proxy="—", outcome="no_script"),
        FbpaCase(number=4, title="d", proxy="—", outcome="no_script"),
    ]
    s = l2replay.summarize(reports, cases)
    assert (s["l2"], s["n"], s["fb_pa"], s["fbpa_eligible"], s["end_to_end_n"]) == (2, 4, 1, 2, 4)
    meta = {"started": "t", "model": "m", "prompt": "1", "selection": "x", "max_steps": 40,
            "max_attempts": 4, "budget_usd": 0.5, "runs": 2, "kind": "L2 测试"}
    md = l2replay.render("o/r", reports, cases, meta)
    assert "**2/4 = 50%**" in md and "**1/2**" in md
    assert "#4（setup_failed）：空测试" in md and "#2（fail_after）" in md
    assert json.loads(l2replay.dump(reports, cases, meta))["summary"]["l2"] == 2


# ---------------------------------------------------------------- 真实 Docker


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory: pytest.TempPathFactory) -> DockerSandbox:
    sb = DockerSandbox(artifacts_dir=tmp_path_factory.mktemp("artifacts"))
    if asyncio.run(sb.server_version()) is None:
        pytest.skip("Docker 不可用")
    return sb


class OfflinePyPI:
    """只为 prepare 提供 pytest 的发布列表，不访问网络（pytest 本身仍从 PyPI 装）。
    mylib 当作没发布到 PyPI 的项目：prepare 照样能推出伪版本号。"""

    async def releases(self, name: str) -> dict[Version, Release]:
        if name == "pytest":
            return dict([rel("8.3.3", "2024-09-10", ">=3.8")])
        raise PackageNotFound(f"PyPI 上没有这个包：{name}")


@pytest.mark.docker
async def test_real_l2_prepare_probe_and_judge(sandbox: DockerSandbox, tmp_path: Path):
    root = write_project(tmp_path / "p")
    (root / "mylib" / "core.py").write_text(
        "def parse(d, key='name'):\n    return d[key]\n", encoding="utf-8"
    )
    (root / "tests").mkdir()
    (root / "tests" / "conftest.py").write_text("", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        (root / "pyproject.toml").read_text(encoding="utf-8")
        + "[tool.pytest.ini_options]\naddopts = '--strict-markers'\n", encoding="utf-8",
    )
    tree = tree_of(pack_dir(root), day=datetime(2024, 10, 1, tzinfo=UTC))
    cache = EnvCache(sandbox, tmp_path / "envcache.json")
    tester = TestReproducer(sandbox, cache, OfflinePyPI())  # type: ignore[arg-type]
    prep = await tester.prepare(PackageConfig(name="mylib"), tree, number=7, python="3.12")
    try:
        assert prep.pytest == "pytest==8.3.3" and prep.test_path == "tests/test_failgate_issue_7.py"
        probe = await tester.run_once(prep, PROBE_TEST)
        assert probe.exit_code == 0
        bug = "from mylib.core import parse\n\ndef test_parse():\n    parse({})\n"
        reported = ('Traceback (most recent call last):\n  File "/x/site-packages/mylib/core.py", '
                    "line 2, in parse\nKeyError: 'name'\n")
        run = await tester.evaluate(prep, bug, reported_traceback=reported)
        assert run.verdict.kind == VerdictKind.REPRODUCED, run.output_tail
        assert run.verdict.observed is not None
        assert run.verdict.observed.frames == ["mylib/core.py:parse"]
        broken = await tester.evaluate(prep, "import no_such_module\n", reported_traceback=reported)
        assert broken.verdict.kind == VerdictKind.UNRELATED_FAILURE
        assert "退出码 2" in broken.verdict.reason
    finally:
        await sandbox.remove_image(prep.env.image)
