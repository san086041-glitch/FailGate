"""Pipeline：状态转换之后，驱动当前阶段对应的能力模块，直到没有可运行的模块为止。

模型调用可能要几秒到几十秒，所以不在数据库事务里执行：
先读 Case 快照 → 事务外运行能力模块 → 新事务里写 Run、累计花费、发出 skill.done。
能力模块出错时记录一条 status=error 的 Run，Case 停在当前状态，等待 /warden retry（M1 后半段）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from warden.db import Case, Database, Repo, Run
from warden.index.store import IssueIndex
from warden.llm import LLMClient
from warden.policy.gate import PolicyGate
from warden.report import render_summary
from warden.skills.base import IssueSnapshot, Skill, SkillContext, SkillResult

from .machine import CaseMachine
from .states import CaseState

log = logging.getLogger(__name__)

# 自动打标签的最低置信度（技术方案第 11 节策略矩阵）
LABEL_MIN_CONFIDENCE = 0.7


class Pipeline:
    def __init__(
        self,
        db: Database,
        machine: CaseMachine,
        gate: PolicyGate,
        llm: LLMClient,
        skills: dict[CaseState, tuple[Skill, str]],
        *,
        case_budget_usd: float,
        index: IssueIndex | None = None,
    ) -> None:
        self.db = db
        self.machine = machine
        self.gate = gate
        self.llm = llm
        # 阶段 → (能力模块, 模型名)
        self.skills = skills
        self.case_budget_usd = case_budget_usd
        self.index = index

    async def advance(self, case_id: int) -> CaseState:
        while True:
            async with self.db.session() as s:
                case = await s.get(Case, case_id)
                assert case is not None
                state = CaseState(case.state)
                entry = self.skills.get(state)
                if entry is None:
                    return state
                if case.spent_usd >= self.case_budget_usd:
                    await self.machine.apply(s, case, "budget.exceeded")
                    await s.commit()
                    return CaseState(case.state)
                repo = await s.get(Repo, case.repo_id)
                assert repo is not None
                ctx = await self._context(s, case, repo, entry)

            skill, model = entry
            started = datetime.now(UTC)
            try:
                result = await skill.run(ctx)
            except Exception as e:
                log.exception("skill %s failed on case %s", skill.name, case_id)
                await self._record_error(case_id, skill, model, started, e)
                return state

            async with self.db.session() as s, s.begin():
                case = await s.get(Case, case_id)
                repo = await s.get(Repo, case.repo_id) if case else None
                assert case is not None and repo is not None
                if CaseState(case.state) is not state:
                    # 运行期间 Case 被关闭或忽略，结果作废
                    return CaseState(case.state)
                s.add(_run_row(case_id, skill, result, started))
                case.spent_usd += result.cost_usd
                await self._effects(s, repo, case, skill.name, result, ctx)
                new_state = await self.machine.apply(s, case, "skill.done", facts=result.facts)
            if new_state is None:
                return state

    async def _context(
        self, s: AsyncSession, case: Case, repo: Repo, entry: tuple[Skill, str]
    ) -> SkillContext:
        runs = await s.scalars(
            select(Run).where(Run.case_id == case.id, Run.status == "ok").order_by(Run.id)
        )
        prior = {r.skill: r.output or {} for r in runs}
        return SkillContext(
            issue=IssueSnapshot(
                repo=repo.full_name,
                number=case.number,
                title=case.title,
                body=case.body,
                author=case.author_login,
                repo_id=repo.id,
                created_at=case.created_at,
            ),
            llm=self.llm,
            model=entry[1],
            prior=prior,
            retriever=self.index,
        )

    async def _effects(
        self,
        s: AsyncSession,
        repo: Repo,
        case: Case,
        skill_name: str,
        result: SkillResult,
        ctx: SkillContext,
    ) -> None:
        output = result.output.model_dump(mode="json")
        if skill_name == "triage":
            if output["labels"] and result.confidence >= LABEL_MIN_CONFIDENCE:
                await self.gate.propose(
                    s, repo=repo, case=case, action="set_labels", payload={"add": output["labels"]}
                )
        elif skill_name == "dedup":
            # 汇总评论在 M1 的最后一个自动阶段（查重）之后生成，一次性包含分诊和查重结果
            body = render_summary(
                ctx.prior.get("intake", {}), ctx.prior.get("triage", {}), case.spent_usd, output
            )
            await self.gate.propose(
                s, repo=repo, case=case, action="upsert_summary", payload={"body": body}
            )

    async def _record_error(
        self, case_id: int, skill: Skill, model: str, started: datetime, error: Exception
    ) -> None:
        async with self.db.session() as s, s.begin():
            s.add(
                Run(
                    case_id=case_id,
                    skill=skill.name,
                    skill_version=skill.version,
                    model=model,
                    status="error",
                    error=f"{type(error).__name__}: {error}"[:2000],
                    started_at=started,
                    ended_at=datetime.now(UTC),
                )
            )


def _run_row(case_id: int, skill: Skill, result: SkillResult, started: datetime) -> Run:
    output: dict[str, Any] = result.output.model_dump(mode="json")
    return Run(
        case_id=case_id,
        skill=skill.name,
        skill_version=skill.version,
        model=result.model,
        status="ok",
        output=output,
        confidence=result.confidence,
        tokens_in=result.usage.prompt_tokens,
        tokens_out=result.usage.completion_tokens,
        tokens_cached=result.usage.cached_tokens,
        usd=result.cost_usd,
        started_at=started,
        ended_at=datetime.now(UTC),
    )
