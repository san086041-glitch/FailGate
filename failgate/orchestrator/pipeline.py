"""Pipeline：状态转换之后，驱动当前阶段对应的能力模块，直到没有可运行的模块为止。

模型调用可能要几秒到几十秒，所以不在数据库事务里执行：
先读 Case 快照 → 事务外运行能力模块 → 新事务里写 Run、累计花费、发出 skill.done。
能力模块出错时记录一条 status=error 的 Run，Case 停在当前状态，等待 /failgate retry（M1 后半段）。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from failgate.db import Case, Database, Evidence, Repo, Run, TransitionLog, VerificationRecord
from failgate.index.store import IssueIndex
from failgate.llm import LLMClient
from failgate.platforms.base import Label
from failgate.policy.gate import PolicyGate
from failgate.report import render_summary
from failgate.repro.judge import RunRecord
from failgate.skills.base import (
    DEFAULT_LABELS,
    CommentSource,
    DocRetriever,
    IssueSnapshot,
    Skill,
    SkillContext,
    SkillResult,
)
from failgate.verify.engine import Verification
from failgate.verify.receipt import SealedTest
from failgate.verify.report import language_of, render_verification
from failgate.verify.store import hidden_row

from .machine import CaseMachine
from .states import CaseState

log = logging.getLogger(__name__)

# 自动打标签的最低置信度（技术方案第 11 节策略矩阵）
LABEL_MIN_CONFIDENCE = 0.7

# 读取仓库真实的标签表；返回 None 表示拿不到（没配置 App 等），退回 GitHub 默认标签
LabelSource = Callable[[Repo], Awaitable[list[Label] | None]]


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
        labels: LabelSource | None = None,
        docs: DocRetriever | None = None,
        comments: Callable[[Repo], CommentSource | None] | None = None,
    ) -> None:
        self.db = db
        self.machine = machine
        self.gate = gate
        self.llm = llm
        # 阶段 → (能力模块, 模型名)
        self.skills = skills
        self.case_budget_usd = case_budget_usd
        self.index = index
        self.labels = labels
        self.docs = docs
        # 仓库 → 读评论的函数（需要该仓库的安装令牌，所以按仓库绑定）
        self.comments_for = comments

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
            # 网络调用放在数据库会话之外
            labels = await self._repo_labels(repo)
            ctx.labels = tuple(lb.name for lb in labels)
            ctx.label_descriptions = {lb.name: lb.description for lb in labels}

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
                # 和 Run 在同一个事务里：要么都落库，要么都不落
                sealed_ids = {sealed.receipt.evidence_id for sealed in result.evidence
                              if await self._seal(s, repo, case, sealed)}
                for h in result.hidden:
                    if h.evidence_id in sealed_ids:
                        s.add(hidden_row(h))
                if result.verification is not None:
                    s.add(_verification_row(case_id, result.verification))
                case.spent_usd += result.cost_usd
                await self._effects(s, repo, case, skill.name, result)
                facts = {**result.facts, **self._pipeline_facts(repo, case)}
                new_state = await self.machine.apply(s, case, "skill.done", facts=facts)
                if new_state is not None and new_state not in self.skills:
                    # 流水线在这里停下（没有下一个自动阶段）：此时才生成汇总评论，
                    # 一次性包含前面所有阶段的结果，避免同一条评论在几秒内被反复编辑
                    outputs = {**ctx.prior, skill.name: result.output.model_dump(mode="json")}
                    await self._summary(s, repo, case, outputs)
            if new_state is None:
                return state

    def _pipeline_facts(self, repo: Repo, case: Case) -> dict[str, Any]:
        """由流水线（而不是能力模块）决定的事实：能不能复现、预算还够不够。

        能力模块不知道仓库配置和全局开关，放在这里算，状态机的守卫只看结果。
        """
        return {
            "repro_enabled": CaseState.REPRODUCING in self.skills and bool(repo.repro_package),
            "budget_ok": case.spent_usd < self.case_budget_usd,
        }

    async def _context(
        self, s: AsyncSession, case: Case, repo: Repo, entry: tuple[Skill, str]
    ) -> SkillContext:
        runs = await s.scalars(
            select(Run).where(Run.case_id == case.id, Run.status == "ok").order_by(Run.id)
        )
        prior = {r.skill: r.output or {} for r in runs}
        last = await s.scalar(
            select(TransitionLog).where(TransitionLog.case_id == case.id)
            .order_by(TransitionLog.id.desc()).limit(1)
        )
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
            docs=self.docs,
            comments=self.comments_for(repo) if self.comments_for else None,
            repo_config={
                "repro_package": repo.repro_package,
                "repro_import_name": repo.repro_import_name,
                "repro_source": repo.repro_source,
            },
            budget_left_usd=max(self.case_budget_usd - case.spent_usd, 0.0),
            actor=last.actor if last is not None else None,
        )

    async def _repo_labels(self, repo: Repo) -> list[Label]:
        """仓库真实的标签表（带说明）；拿不到时退回 GitHub 默认标签（没有说明）。"""
        defaults = [Label(name=n) for n in DEFAULT_LABELS]
        if self.labels is None:
            return defaults
        try:
            labels = await self.labels(repo)
        except Exception:
            log.warning(
                "failed to load labels for %s, using defaults", repo.full_name, exc_info=True
            )
            return defaults
        return defaults if labels is None else labels

    async def _effects(
        self,
        s: AsyncSession,
        repo: Repo,
        case: Case,
        skill_name: str,
        result: SkillResult,
    ) -> None:
        output = result.output.model_dump(mode="json")
        if skill_name == "triage":
            if output["labels"] and result.confidence >= LABEL_MIN_CONFIDENCE:
                await self.gate.propose(
                    s, repo=repo, case=case, action="set_labels", payload={"add": output["labels"]}
                )

    async def _seal(self, s: AsyncSession, repo: Repo, case: Case, sealed: SealedTest) -> bool:
        """证据挂在它所属 issue 的 Case 下（重新封存是在 PR 上发起的）；取代旧证据时，
        旧行的 superseded_by 指向新行——这是封存后唯一允许的修改。"""
        r = sealed.receipt
        owner = case
        if case.kind != "issue" or case.number != r.issue:
            found = await s.scalar(select(Case).where(
                Case.repo_id == repo.id, Case.kind == "issue", Case.number == r.issue))
            if found is None:
                log.warning("no issue case for evidence %s (#%s)", r.evidence_id, r.issue)
                return False
            owner = found
        s.add(_evidence_row(owner.id, sealed))
        if r.supersedes:
            old = await s.get(Evidence, r.supersedes)
            if old is not None:
                old.superseded_by = r.evidence_id
        return True

    async def _summary(
        self, s: AsyncSession, repo: Repo, case: Case, outputs: dict[str, dict[str, Any]]
    ) -> None:
        if case.kind == "pull":
            data = (outputs.get("verify") or {}).get("verification")
            if data is None:
                return
            v = Verification.model_validate(data)
            body = render_verification(v, language_of(case.title, case.body))
            await self.gate.propose(
                s, repo=repo, case=case, action="upsert_summary", payload={"body": body}
            )
            return
        if "triage" not in outputs:
            return
        body = render_summary(
            outputs.get("intake", {}),
            outputs["triage"],
            case.spent_usd,
            outputs.get("dedup"),
            outputs.get("answer"),
            outputs.get("repro"),
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


def _evidence_row(case_id: int, sealed: SealedTest) -> Evidence:
    r = sealed.receipt
    signed = sealed.signed()
    return Evidence(
        id=r.evidence_id, case_id=case_id, level=r.level, mode=r.mode, acceptance=r.acceptance,
        test_path=r.test_path, test_code=sealed.code, test_sha256=r.test_sha256,
        source_repo=r.source_repo, source_sha=r.source_sha, python=r.python, pytest=r.pytest,
        verdict=r.verdict, fail_rate=_fail_rate(r.runs), receipt=signed,
        receipt_sha256=signed["receipt_sha256"],
    )


def _verification_row(case_id: int, v: Verification) -> VerificationRecord:
    receipt = v.receipt()
    return VerificationRecord(
        case_id=case_id, pr_number=v.pr, base_sha=v.base_sha, head_sha=v.head_sha,
        verdict=v.verdict.value if v.verdict else None, receipt=receipt,
        receipt_sha256=receipt["receipt_sha256"],
    )


def _fail_rate(runs: list[RunRecord]) -> float | None:
    return round(sum(x.same_failure for x in runs) / len(runs), 4) if runs else None


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
