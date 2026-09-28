"""复现接进流水线：DEDUPING → REPRODUCING → REPRODUCED / NEED_INFO，以及汇总评论。

真正的复现（Docker + Agent）换成假 runner，这里测的是编排：配置开关、预算、状态流转、
提问者补充评论后重新复现、汇总评论怎么写、哪些内容不能写出去。
"""

from __future__ import annotations

from typing import Any

from conftest import (
    PUBLIC_COMMENTS,
    REPO,
    Harness,
    _harness,
    comment_event,
    issue_event,
    make_settings,
)
from fake_llm import INTAKE_OK
from sqlalchemy import select

from failgate.db import Repo
from failgate.repro.agent import AgentResult, Attempt
from failgate.repro.evidence import EvidenceLevel
from failgate.repro.issue import IssueReproReport
from failgate.repro.judge import Verdict, VerdictKind
from failgate.repro.package import PackageRepro, VersionRun
from failgate.skills.repro import ReproRequest

SCRIPT = 'import mylib\nmylib.parse({"title": "x"})\n'


def version_run(kind: VerdictKind, runs: int = 4) -> VersionRun:
    return VersionRun(
        version="2.4.1", python="3.12", env_key="k" * 64, cache_hit=True,
        verdict=Verdict(kind=kind, reason="r", match=1.0, runs=runs,
                        fail_rate=1.0 if kind == VerdictKind.REPRODUCED else None),
    )


def reproduced(**kw: Any) -> IssueReproReport:
    repro = PackageRepro(
        package="mylib", module="mylib", reported_version="2.4.1", latest_version="2.6.0",
        reported=version_run(VerdictKind.REPRODUCED),
        latest=version_run(VerdictKind.NOT_REPRODUCED, runs=1), level=EvidenceLevel.L1,
        **kw,
    )
    agent = AgentResult(
        status="reproduced", final_script=SCRIPT, steps=9, cost_usd=0.012,
        prompt_tokens=30_000, completion_tokens=900, cached_tokens=25_000,
        attempts=[Attempt(n=1, name="repro.py", claim="c", script=SCRIPT,
                          kind=VerdictKind.REPRODUCED, reason="r", match=1.0)],
    )
    return _report(repro, agent)


def not_reproduced() -> IssueReproReport:
    repro = PackageRepro(
        package="mylib", module="mylib", reported_version="2.4.1", latest_version="2.6.0",
        reported=version_run(VerdictKind.UNRELATED_FAILURE, runs=1),
    )
    agent = AgentResult(status="not_reproduced", steps=30, cost_usd=0.02)
    return _report(repro, agent)


def setup_failed(error: str) -> IssueReproReport:
    return _report(PackageRepro(package="mylib", module="mylib", error=error), None)


def _report(repro: PackageRepro, agent: AgentResult | None) -> IssueReproReport:
    return IssueReproReport(
        repo=REPO, number=1, title="t", intake_version="2.4.1", intake_python="3.12.3",
        has_traceback=True, repro=repro, agent=agent, intake_cost_usd=0.0,
        judge_cost_usd=0.001, judge_prompt_tokens=500,
    )


class FakeRunner:
    def __init__(self, *reports: IssueReproReport) -> None:
        self.reports = list(reports)
        self.requests: list[ReproRequest] = []

    async def __call__(self, req: ReproRequest) -> IssueReproReport:
        self.requests.append(req)
        return self.reports.pop(0)


async def configure(h: Harness, package: str | None = "mylib") -> None:
    """仓库在第一个事件时才建；先建好并配上包名。"""
    async with h.failgate.db.session() as s, s.begin():
        s.add(Repo(platform="github", full_name=REPO, mode="shadow", repro_package=package))


async def case_detail(h: Harness) -> dict[str, Any]:
    cases = (await h.client.get("/api/cases")).json()
    return (await h.client.get(f"/api/cases/{cases[0]['id']}")).json()


def summary(case: dict[str, Any]) -> str:
    bodies = [e["payload"]["body"] for e in case["effects"] if e["action"] == "upsert_summary"]
    return bodies[-1]


async def run_issue(runner: FakeRunner, tmp_path, package: str | None = "mylib", **settings):
    async for h in _harness(make_settings(tmp_path, **settings), repro_runner=runner):
        await configure(h, package)
        await h.send("issues", issue_event("opened"), "d-1")
        await h.failgate.worker.drain()
        yield h


async def test_reproduced_bug_reaches_reproduced_with_evidence_in_summary(tmp_path):
    runner = FakeRunner(reproduced())
    async for h in run_issue(runner, tmp_path):
        case = await case_detail(h)
        assert case["state"] == "REPRODUCED"
        repro_run = next(r for r in case["runs"] if r["skill"] == "repro")
        assert repro_run["output"]["level"] == "L1" and repro_run["usd"] > 0.012
        req = runner.requests[0]
        # Intake 的结果原样交给复现；预算取 复现上限 和 Case 剩余预算 里较小的
        assert req.intake.reported_version == INTAKE_OK["reported_version"]
        assert req.cfg.name == "mylib" and 0 < req.budget_usd <= 0.5
        body = summary(case)
        assert "已在 `mylib==2.4.1`（Python 3.12）上复现（证据等级 L1）" in body
        assert "最新版 `2.6.0` 上没有复现，可能已经修复" in body
        assert "<details><summary>复现脚本</summary>" in body and SCRIPT.strip() in body
        # 复现了就不再向提问者要信息
        assert "请补充以下信息" not in body
        # 本地路径（对话记录）不会写进评论
        assert "artifacts" not in body


async def test_repo_without_package_is_triage_only(tmp_path):
    runner = FakeRunner()
    async for h in run_issue(runner, tmp_path, package=None):
        assert (await case_detail(h))["state"] == "TRIAGE_ONLY" and runner.requests == []


async def test_not_reproduced_asks_for_info_then_retries_with_author_comment(tmp_path):
    runner = FakeRunner(not_reproduced(), reproduced())
    async for h in run_issue(runner, tmp_path):
        case = await case_detail(h)
        assert case["state"] == "NEED_INFO"
        body = summary(case)
        assert "暂时没有成功" in body and "FailGate 会重新尝试" in body
        assert "请补充以下信息" in body  # 没复现：照常列出缺的信息

        # 陌生人评论不触发；提问者本人补充后重新复现，补充内容进了正文
        await h.send("issue_comment", comment_event("+1", login="bob"), "d-2")
        await h.failgate.worker.drain()
        assert len(runner.requests) == 1
        PUBLIC_COMMENTS[1] = [{
            "id": 9, "user": {"login": "alice", "type": "User"}, "author_association": "NONE",
            "body": "最小复现：mylib.parse({'title': 'x'})", "created_at": "2024-01-01T00:00:00Z",
        }]
        await h.send("issue_comment", comment_event("最小复现 …", login="alice"), "d-3")
        await h.failgate.worker.drain()
        assert len(runner.requests) == 2
        assert "mylib.parse({'title': 'x'})" in runner.requests[1].body
        case = await case_detail(h)
        assert case["state"] == "REPRODUCED"
        second = [r for r in case["runs"] if r["skill"] == "repro"][-1]
        assert second["output"]["followup_comments"] == 1


async def test_version_problem_is_explained_but_internal_errors_are_not_leaked(tmp_path):
    runner = FakeRunner(setup_failed("无法从 'main@abc' 解析出可安装的发布版本"))
    async for h in run_issue(runner, tmp_path):
        body = summary(await case_detail(h))
        assert "请确认准确的版本号" in body and "pip show mylib" in body

    runner = FakeRunner(setup_failed(
        "拉取镜像失败：python:3.13-slim：error getting credentials C:\\Users\\admin"
    ))
    (tmp_path / "b").mkdir()
    async for h in run_issue(runner, tmp_path / "b"):
        case = await case_detail(h)
        body = summary(case)
        assert case["state"] == "NEED_INFO" and "暂时无法自动复现" in body
        assert "credentials" not in body and "admin" not in body


async def test_disabled_globally_means_no_repro_stage(tmp_path):
    # 没注入 runner、REPRO_ENABLED 默认关闭：配了包也不复现
    async for h in _harness(make_settings(tmp_path)):
        await configure(h)
        await h.send("issues", issue_event("opened"), "d-1")
        await h.failgate.worker.drain()
        assert (await case_detail(h))["state"] == "TRIAGE_ONLY"
        assert h.failgate.repro_runner is None


async def test_repo_config_survives_repo_row_updates(tmp_path):
    runner = FakeRunner(reproduced())
    async for h in run_issue(runner, tmp_path):
        async with h.failgate.db.session() as s:
            repo = await s.scalar(select(Repo).where(Repo.full_name == REPO))
        # webhook 会更新 installation_id，但不能把复现配置冲掉
        assert repo is not None and repo.repro_package == "mylib" and repo.installation_id == 42
