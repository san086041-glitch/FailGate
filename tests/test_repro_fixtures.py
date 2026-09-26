"""source 模式接进流水线，以及 fixture 仓库。

- 什么时候走 source 模式（needs_source、runner 的判断）；
- 流水线拿到 L2 结果后的状态和汇总评论（中英文、复现 / 没复现 / 偶发）；
- fixture 仓库的加载，以及用剧本式 LLM + 真实 Docker 跑通一个 fixture（CI 里零 LLM 花费）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from conftest import REPO, _harness, issue_event, make_settings
from packaging.version import Version
from test_repro_agent import ScriptedLLM, call
from test_repro_pipeline import FakeRunner, case_detail, summary

from warden.db import Repo
from warden.replay import fixtures as fx_mod
from warden.repro.agent import AgentResult, Attempt
from warden.repro.config import PackageConfig, needs_source
from warden.repro.envcache import EnvCache
from warden.repro.evidence import EvidenceLevel
from warden.repro.issue import L2IssueReport
from warden.repro.judge import Verdict, VerdictKind
from warden.repro.l2 import SourceRepro, TestReproducer
from warden.repro.package import VersionRun
from warden.repro.sandbox import DockerSandbox
from warden.settings import Settings
from warden.skills.intake import IntakeOutput
from warden.skills.repro import ReproOutput, ReproRequest, SandboxReproRunner

TEST_CODE = "import mylib\n\ndef test_parse():\n    mylib.parse({})\n"
RELEASED = [Version("2.4.0"), Version("2.4.1"), Version("2.6.0")]

# ---------------------------------------------------------------- 什么时候走 source 模式


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("mylib 2.4.1", False),  # 发布过：package 模式
        ("mylib, 2.4.2.dev27+g7fa1faf", True),  # 开发版
        ("main@3f2a9c1", True),  # 没有版本号，只有分支和提交
        ("mylib 2.5.0", True),  # 版本号合法，但 PyPI 上没有
        (None, False),  # 没报版本：沿用 package 模式（还能查最新版是否已修复）
        ("   ", False),
    ],
)
def test_needs_source(raw: str | None, expected: bool):
    assert needs_source(raw, "mylib", RELEASED) is expected


class FakePyPI:
    async def releases(self, name: str) -> dict[Version, object]:
        return dict.fromkeys(RELEASED)


def request(version: str | None, source: str | None) -> ReproRequest:
    intake = IntakeOutput.model_validate({
        "reported_version": version, "environment": {}, "repro_steps": [],
        "expected": None, "actual": None, "missing": [], "language": "en", "verifiability": 1.0,
    })
    return ReproRequest(repo=REPO, number=1, title="t", body="b", created_at=None,
                        intake=intake, cfg=PackageConfig(name="mylib"), budget_usd=0.5,
                        source_repo=source)


async def test_runner_uses_source_only_for_unreleased_versions_with_a_source_repo(tmp_path):
    runner = SandboxReproRunner(Settings(_env_file=None), llm=None)  # type: ignore[arg-type]

    class Rep:
        pypi = FakePyPI()

    runner._get_reproducer = lambda: Rep()  # type: ignore[method-assign,assignment,return-value]
    assert await runner._use_source(request("mylib 2.4.2.dev3", "acme/mylib")) is True
    assert await runner._use_source(request("mylib 2.4.1", "acme/mylib")) is False
    assert await runner._use_source(request("mylib 2.4.2.dev3", None)) is False


# ---------------------------------------------------------------- 流水线和汇总评论


def l2_report(*, level: str = "L2", kind: VerdictKind = VerdictKind.REPRODUCED,
              status: str = "reproduced", fail_rate: float | None = 1.0) -> L2IssueReport:
    run = VersionRun(version="3f2a9c1e00", python="3.12", env_key="k" * 64, cache_hit=True,
                     verdict=Verdict(kind=kind, reason="r", match=1.0, runs=4 if kind ==
                                     VerdictKind.REPRODUCED else 30, fail_rate=fail_rate))
    src = SourceRepro(
        repo="acme/mylib", sha="3f2a9c1e00" + "0" * 30, package="mylib", module="mylib",
        python="3.12", pytest="pytest==8.0.0", test_path="tests/test_warden_issue_1.py",
        level=EvidenceLevel(level), run=run if level == "L2" else None,
    )
    agent = AgentResult(
        status=status, final_script=TEST_CODE if level == "L2" else None, steps=20,  # type: ignore[arg-type]
        cost_usd=0.013, prompt_tokens=40_000, completion_tokens=1_000,
        attempts=[Attempt(n=1, name="tests/test_warden_issue_1.py", claim="c", script=TEST_CODE,
                          kind=kind, reason="r", match=1.0)],
        test_path="tests/test_warden_issue_1.py",
    )
    return L2IssueReport(repo=REPO, number=1, title="t", intake_version="2.4.2.dev3",
                         intake_python="3.12", has_traceback=True, source=src, agent=agent,
                         intake_cost_usd=0.0, judge_cost_usd=0.0)


async def run_source_issue(runner: FakeRunner, tmp_path):
    async for h in _harness(make_settings(tmp_path), repro_runner=runner):
        async with h.warden.db.session() as s, s.begin():
            s.add(Repo(platform="github", full_name=REPO, mode="shadow",
                       repro_package="mylib", repro_source="acme/mylib"))
        await h.send("issues", issue_event("opened"), "d-1")
        await h.warden.worker.drain()
        yield h


async def test_l2_result_reaches_reproduced_with_test_in_summary(tmp_path):
    runner = FakeRunner(l2_report())  # type: ignore[arg-type]
    async for h in run_source_issue(runner, tmp_path):
        assert runner.requests[0].source_repo == "acme/mylib"  # 仓库配置传到了 runner
        case = await case_detail(h)
        assert case["state"] == "REPRODUCED"
        out = next(r for r in case["runs"] if r["skill"] == "repro")["output"]
        assert out["mode"] == "source" and out["level"] == "L2"
        body = summary(case)
        env = "`acme/mylib@3f2a9c1`（issue 创建时的源码，Python 3.12）"
        assert f"已在 {env}上复现（证据等级 L2）" in body
        assert "`tests/test_warden_issue_1.py` 可以直接合进仓库" in body
        assert "<details><summary>仓库内的失败测试</summary>" in body and "mylib.parse({})" in body
        assert "最新版" not in body  # source 模式不做"最新版是否已修复"的判断


async def test_l2_miss_goes_to_need_info_without_leaking_internals(tmp_path):
    runner = FakeRunner(l2_report(level="NONE", kind=VerdictKind.NOT_REPRODUCED,  # type: ignore[arg-type]
                                  status="not_reproduced"))
    async for h in run_source_issue(runner, tmp_path):
        case = await case_detail(h)
        assert case["state"] == "NEED_INFO"
        body = summary(case)
        env = "`acme/mylib@3f2a9c1`（issue 创建时的源码，Python 3.12）"
        assert f"尝试在 {env}上写一个会失败的测试" in body
        assert "可能已经修复" in body


def test_render_english_and_flaky():
    from warden.report import _repro_lines

    out = ReproOutput.from_l2_report(
        l2_report(kind=VerdictKind.FLAKY, fail_rate=0.5), followups=0
    ).model_dump()
    from warden.report import _TEXT

    text = "\n".join(_repro_lines(out, _TEXT["en"]))
    assert "Reproduced on `acme/mylib@3f2a9c1` (source at the time this issue was opened, " \
           "Python 3.12), but flaky" in text
    assert "about 50% of 30 runs failed" in text
    assert "Failing test for the repository" in text


def test_setup_error_is_not_public():
    rep = l2_report(level="NONE")
    rep.agent = None
    rep.source.error = "空测试在这个环境里跑不通（exit=2）：/home/warden/src/conftest.py"
    out = ReproOutput.from_l2_report(rep, followups=0)
    assert out.public_error is None and out.error is not None


# ---------------------------------------------------------------- fixture 仓库


def test_fixtures_load():
    items = fx_mod.load_all()
    assert [f.name for f in items] == ["bug-flaky", "bug-keyerror", "bug-regression"]
    for f in items:
        assert f.tree.top_dir == "src" and f.tree.test_dir() == "tests"
        assert f.tree.tarball != f.fixed_tree.tarball  # fix/ 确实改了东西
        assert f.title and f.body and f.package and f.expect
    assert fx_mod.load_all(only=["bug-keyerror"])[0].number == 101


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory: pytest.TempPathFactory) -> DockerSandbox:
    sb = DockerSandbox(artifacts_dir=tmp_path_factory.mktemp("artifacts"))
    if asyncio.run(sb.server_version()) is None:
        pytest.skip("Docker 不可用")
    return sb


KEYERROR_TEST = '''from confkit import parse


def test_keys_before_first_section_go_to_default() -> None:
    assert parse("name = demo\\n[server]\\nhost = example.org\\n") == {
        "DEFAULT": {"name": "demo"},
        "server": {"host": "example.org"},
    }
'''


@pytest.mark.docker
async def test_fixture_end_to_end_with_scripted_llm(sandbox: DockerSandbox, tmp_path: Path):
    """真实 Docker、真实 pytest（要访问 PyPI 装 setuptools / pytest），LLM 按剧本走。"""
    from warden.repro.pypi import PyPIClient

    fx = fx_mod.load_all(only=["bug-keyerror"])[0]
    intake = IntakeOutput.model_validate({
        "reported_version": "confkit 0.3.0", "environment": {"python": "3.12"},
        "repro_steps": [], "expected": None, "actual": None, "missing": [], "language": "en",
        "verifiability": 1.0,
    })
    llm = ScriptedLLM([
        {"tool_calls": [call("search_code", {"pattern": "def test_", "path": "tests"})]},
        {"tool_calls": [call("write_test", {"content": KEYERROR_TEST})]},
        {"tool_calls": [call("run_test", {})]},
        {"tool_calls": [call("submit", {"claim": "keys before the first section: KeyError"})]},
    ])
    pypi = PyPIClient()
    tester = TestReproducer(sandbox, EnvCache(sandbox, tmp_path / "envcache.json"), pypi)
    try:
        report = await fx_mod.reproduce_fixture(
            fx, intake, llm=llm.client(), model="deepseek-flash", tester=tester, max_steps=10,
            max_attempts=2, budget_usd=0.5, artifacts_dir=None,
        )
        assert report.source.level == EvidenceLevel.L2, report.source.error
        run = report.source.run
        assert run is not None and run.verdict.kind == VerdictKind.REPRODUCED
        assert run.verdict.observed is not None
        assert run.verdict.observed.frames[-1] == "confkit/parser.py:_store"
        case = await fx_mod.fixture_fbpa(fx, report, tester, runs=1)
        assert case.outcome == "fb_pa", case
        result = fx_mod.FixtureResult(name=fx.name, kind=fx.kind, expect=fx.expect,
                                      report=report, fbpa=case)
        assert result.passed
        md = fx_mod.render([result], {"started": "t", "model": "m", "prompt": "1", "runs": 1})
        assert "1/1 通过验收" in md and "严格 FB/PA 1/1" in md
    finally:
        await pypi.aclose()
        # 删掉这次建的环境镜像（有 bug 的和打上 fix 的各一个）
        for entry in cache_entries(tmp_path / "envcache.json"):
            await sandbox.remove_image(entry)


def cache_entries(index: Path) -> list[str]:
    try:
        return [str(v["image"]) for v in json.loads(index.read_text()).values()]
    except (OSError, ValueError):
        return []
