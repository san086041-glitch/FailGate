"""单个 issue 的完整复现：Intake → 准备报告版本的环境 → 复现 Agent → 最新版复查；
source 模式（L2）：Intake → issue 创建时的提交 → 源码环境 → Agent 写仓库内的失败测试。

warden repro issue（调试单个 issue）和 warden replay repro（回放评测）共用这一个入口，
保证评测测的就是线上会跑的那条路径。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx
from pydantic import BaseModel

from warden.llm import LLMClient, Usage
from warden.platforms.github_rest import GitHubRest
from warden.repro.agent import (
    AgentResult,
    AgentTask,
    ReproAgent,
    reproduce_with_agent,
    reproduce_with_tests,
)
from warden.repro.config import PackageConfig
from warden.repro.l2 import SourceRepro, TestReproducer
from warden.repro.package import IssueContext, PackageRepro, PackageReproducer
from warden.repro.semantic import SemanticJudge
from warden.repro.signature import extract_traceback_chain
from warden.repro.source import SourceError, SourceTree, fetch_github_tree
from warden.skills.base import IssueSnapshot, SkillContext
from warden.skills.intake import IntakeOutput, IntakeSkill


class IssueReproReport(BaseModel):
    repo: str
    number: int
    title: str
    intake_version: str | None
    intake_python: str | None
    has_traceback: bool
    repro: PackageRepro
    agent: AgentResult | None
    intake_cost_usd: float
    judge_cost_usd: float
    judge_prompt_tokens: int = 0
    judge_completion_tokens: int = 0
    judge_cached_tokens: int = 0

    @property
    def total_cost_usd(self) -> float:
        agent = self.agent.cost_usd if self.agent else 0.0
        return round(self.intake_cost_usd + agent + self.judge_cost_usd, 6)

    def repro_usage(self) -> Usage:
        """复现阶段（Agent + 评委，不含 Intake）的 token 用量。"""
        a = self.agent
        return Usage(
            (a.prompt_tokens if a else 0) + self.judge_prompt_tokens,
            (a.completion_tokens if a else 0) + self.judge_completion_tokens,
            (a.cached_tokens if a else 0) + self.judge_cached_tokens,
        )


async def reproduce_issue(
    *,
    repo: str,
    number: int,
    title: str,
    body: str,
    created_at: datetime | None = None,
    cfg: PackageConfig,
    llm: LLMClient,
    small_model: str,
    large_model: str,
    reproducer: PackageReproducer,
    max_steps: int,
    max_attempts: int,
    budget_usd: float,
    artifacts_dir: Path | None,
    check_latest: bool = True,
) -> IssueReproReport:
    """CLI 和回放用：先跑 Intake，再复现。"""
    intake_res = await IntakeSkill().run(SkillContext(
        issue=IssueSnapshot(repo=repo, number=number, title=title, body=body),
        llm=llm, model=small_model,
    ))
    intake = intake_res.output
    assert isinstance(intake, IntakeOutput)
    report = await reproduce_after_intake(
        repo=repo, number=number, title=title, body=body, created_at=created_at,
        intake=intake, cfg=cfg, llm=llm, model=large_model, reproducer=reproducer,
        max_steps=max_steps, max_attempts=max_attempts, budget_usd=budget_usd,
        artifacts_dir=artifacts_dir, check_latest=check_latest,
    )
    report.intake_cost_usd = intake_res.cost_usd
    return report


async def reproduce_after_intake(
    *,
    repo: str,
    number: int,
    title: str,
    body: str,
    created_at: datetime | None,
    intake: IntakeOutput,
    cfg: PackageConfig,
    llm: LLMClient,
    model: str,
    reproducer: PackageReproducer,
    max_steps: int,
    max_attempts: int,
    budget_usd: float,
    artifacts_dir: Path | None,
    check_latest: bool = True,
) -> IssueReproReport:
    """流水线用：Intake 已经跑过（结果在 Run 里），直接复现。CLI / 回放也走这里。"""
    # 每个 issue 一个独立的评委，花费分开统计；复制一个 reproducer 挂上它，
    # 不改共享的那一个（并发复现时互不干扰）
    judge = SemanticJudge(llm, model)
    scoped = PackageReproducer(
        reproducer.sandbox, reproducer.cache, reproducer.pypi,
        run_timeout_s=reproducer.run_timeout_s, judge=judge,
    )
    agent = ReproAgent(
        llm, model, scoped, max_steps=max_steps, max_attempts=max_attempts,
        budget_usd=budget_usd, artifacts_dir=artifacts_dir,
    )
    task = AgentTask(
        repo=repo, number=number,
        issue=IssueContext(title=title, body=body, expected=intake.expected, actual=intake.actual),
        # 完整异常链：判定器比较的是用户最终看到的那个异常（见 extract_traceback_chain）
        reported_traceback=extract_traceback_chain(body) or intake.traceback,
        reported_version_text=intake.reported_version,
        created_at=created_at,
    )
    repro, agent_result = await reproduce_with_agent(
        agent, cfg, task, env_python=intake.environment.python, check_latest=check_latest
    )
    return IssueReproReport(
        repo=repo, number=number, title=title,
        intake_version=intake.reported_version, intake_python=intake.environment.python,
        has_traceback=task.reported_traceback is not None,
        repro=repro, agent=agent_result,
        intake_cost_usd=0.0, judge_cost_usd=round(judge.cost_usd, 6),
        judge_prompt_tokens=judge.usage.prompt_tokens,
        judge_completion_tokens=judge.usage.completion_tokens,
        judge_cached_tokens=judge.usage.cached_tokens,
    )


# ---------------------------------------------------------------- source 模式（L2）


class L2IssueReport(BaseModel):
    """一个 issue 的 L2 复现：在 issue 创建时的提交上，Agent 写仓库内的失败测试。"""

    repo: str
    number: int
    title: str
    intake_version: str | None
    intake_python: str | None
    has_traceback: bool
    source: SourceRepro
    agent: AgentResult | None
    intake_cost_usd: float
    judge_cost_usd: float
    judge_prompt_tokens: int = 0
    judge_completion_tokens: int = 0
    judge_cached_tokens: int = 0

    @property
    def total_cost_usd(self) -> float:
        agent = self.agent.cost_usd if self.agent else 0.0
        return round(self.intake_cost_usd + agent + self.judge_cost_usd, 6)

    def repro_usage(self) -> Usage:
        """复现阶段（Agent + 评委，不含 Intake）的 token 用量。"""
        a = self.agent
        return Usage(
            (a.prompt_tokens if a else 0) + self.judge_prompt_tokens,
            (a.completion_tokens if a else 0) + self.judge_completion_tokens,
            (a.cached_tokens if a else 0) + self.judge_cached_tokens,
        )


async def reproduce_issue_l2(
    *,
    repo: str,
    number: int,
    title: str,
    body: str,
    created_at: datetime,
    cfg: PackageConfig,
    source_repo: str,
    gh: GitHubRest,
    llm: LLMClient,
    small_model: str,
    large_model: str,
    tester: TestReproducer,
    max_steps: int,
    max_attempts: int,
    budget_usd: float,
    artifacts_dir: Path | None,
) -> L2IssueReport:
    """CLI 和回放用：Intake → issue 创建时的提交（时间旅行）→ 源码环境 + 预检 → Agent 写测试。"""
    intake_res = await IntakeSkill().run(SkillContext(
        issue=IssueSnapshot(repo=repo, number=number, title=title, body=body),
        llm=llm, model=small_model,
    ))
    intake = intake_res.output
    assert isinstance(intake, IntakeOutput)
    report = await reproduce_l2_after_intake(
        repo=repo, number=number, title=title, body=body, created_at=created_at,
        intake=intake, cfg=cfg, source_repo=source_repo, gh=gh, llm=llm, model=large_model,
        tester=tester, max_steps=max_steps, max_attempts=max_attempts, budget_usd=budget_usd,
        artifacts_dir=artifacts_dir,
    )
    report.intake_cost_usd = intake_res.cost_usd
    return report


async def reproduce_l2_after_intake(
    *,
    repo: str,
    number: int,
    title: str,
    body: str,
    created_at: datetime | None,
    intake: IntakeOutput,
    cfg: PackageConfig,
    source_repo: str,
    gh: GitHubRest,
    llm: LLMClient,
    model: str,
    tester: TestReproducer,
    max_steps: int,
    max_attempts: int,
    budget_usd: float,
    artifacts_dir: Path | None,
) -> L2IssueReport:
    """流水线用：Intake 已经跑过。取 issue 创建时（没有创建时间就取现在）的源码，再写测试。"""
    report = new_l2_report(repo, number, title, body, intake, cfg, source_repo)
    try:
        if created_at is not None:
            tree = await fetch_github_tree(gh, source_repo, before=created_at)
        else:
            tree = await fetch_github_tree(gh, source_repo, "HEAD")
    except (SourceError, httpx.HTTPError) as e:
        report.source.error = f"拿不到 issue 创建时的源码：{e}"[:1000]
        return report
    return await reproduce_tree_l2(
        report, tree, title=title, body=body, created_at=created_at, intake=intake, cfg=cfg,
        llm=llm, model=model, tester=tester, max_steps=max_steps, max_attempts=max_attempts,
        budget_usd=budget_usd, artifacts_dir=artifacts_dir,
    )


def new_l2_report(
    repo: str, number: int, title: str, body: str, intake: IntakeOutput, cfg: PackageConfig,
    source_repo: str,
) -> L2IssueReport:
    return L2IssueReport(
        repo=repo, number=number, title=title, intake_version=intake.reported_version,
        intake_python=intake.environment.python,
        has_traceback=(extract_traceback_chain(body) or intake.traceback) is not None,
        source=SourceRepro(repo=source_repo, package=cfg.name, module=cfg.module),
        agent=None, intake_cost_usd=0.0, judge_cost_usd=0.0,
    )


async def reproduce_tree_l2(
    report: L2IssueReport,
    tree: SourceTree,
    *,
    title: str,
    body: str,
    created_at: datetime | None,
    intake: IntakeOutput,
    cfg: PackageConfig,
    llm: LLMClient,
    model: str,
    tester: TestReproducer,
    max_steps: int,
    max_attempts: int,
    budget_usd: float,
    artifacts_dir: Path | None,
    python: str | None = None,
    version: str | None = None,
) -> L2IssueReport:
    """在给定的源码树上写测试（fixture 仓库直接从这里进来）。

    python / version：指定 Python 和伪版本号（fixture 的包不在 PyPI 上，不能按发布记录推算）。
    """
    task = AgentTask(
        repo=report.repo, number=report.number,
        issue=IssueContext(title=title, body=body, expected=intake.expected, actual=intake.actual),
        reported_traceback=extract_traceback_chain(body) or intake.traceback,
        reported_version_text=intake.reported_version,
        created_at=created_at,
    )
    judge = SemanticJudge(llm, model)
    agent = ReproAgent(
        llm, model, tester.scoped(judge), max_steps=max_steps,
        max_attempts=max_attempts, budget_usd=budget_usd, artifacts_dir=artifacts_dir,
    )
    report.source, report.agent = await reproduce_with_tests(
        agent, cfg, task, tree, python=python or intake.environment.python, version=version,
    )
    report.judge_cost_usd = round(judge.cost_usd, 6)
    report.judge_prompt_tokens = judge.usage.prompt_tokens
    report.judge_completion_tokens = judge.usage.completion_tokens
    report.judge_cached_tokens = judge.usage.cached_tokens
    return report
