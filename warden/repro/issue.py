"""单个 issue 的完整复现：Intake → 准备报告版本的环境 → 复现 Agent → 最新版复查。

warden repro issue（调试单个 issue）和 warden replay repro（回放评测）共用这一个入口，
保证评测测的就是线上会跑的那条路径。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from warden.llm import LLMClient
from warden.repro.agent import AgentResult, AgentTask, ReproAgent, reproduce_with_agent
from warden.repro.config import PackageConfig
from warden.repro.package import IssueContext, PackageRepro, PackageReproducer
from warden.repro.semantic import SemanticJudge
from warden.repro.signature import extract_traceback_chain
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

    @property
    def total_cost_usd(self) -> float:
        agent = self.agent.cost_usd if self.agent else 0.0
        return round(self.intake_cost_usd + agent + self.judge_cost_usd, 6)


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
    intake_res = await IntakeSkill().run(SkillContext(
        issue=IssueSnapshot(repo=repo, number=number, title=title, body=body),
        llm=llm, model=small_model,
    ))
    intake = intake_res.output
    assert isinstance(intake, IntakeOutput)

    # 每个 issue 一个独立的评委，花费分开统计
    judge = SemanticJudge(llm, large_model)
    reproducer.judge = judge
    agent = ReproAgent(
        llm, large_model, reproducer, max_steps=max_steps, max_attempts=max_attempts,
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
        intake_cost_usd=intake_res.cost_usd, judge_cost_usd=round(judge.cost_usd, 6),
    )
