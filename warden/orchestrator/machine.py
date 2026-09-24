"""CaseMachine：把一个 DomainEvent 应用到对应 Case 上。

每次调用在一个事务里完成：定位或创建 Repo/Case → 查转换表 → 写转换日志 → 执行进入新状态的动作。
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from warden.db import Case, Database, Repo, TransitionLog
from warden.platforms.base import DomainEvent
from warden.policy.gate import PolicyGate

from .commands import parse_command
from .states import CaseState
from .transitions import GuardContext, resolve

log = logging.getLogger(__name__)

# M0 占位回复：证明"事件 → 状态机 → 策略层"这条链路打通；M1 由 Intake/Triage 的汇总评论取代
ACK_BODY = "RepoWarden 已收到这个 issue，正在整理信息。"


def event_name(event: DomainEvent) -> str:
    if event.name == "comment.created":
        cmd = parse_command(event.body)
        if cmd is not None:
            return f"cmd.{cmd.verb}"
    return event.name


class CaseMachine:
    def __init__(self, db: Database, gate: PolicyGate, default_mode: str = "shadow") -> None:
        self.db = db
        self.gate = gate
        self.default_mode = default_mode

    async def handle(self, event: DomainEvent) -> CaseState | None:
        """返回转换后的新状态；事件被忽略时返回 None。"""
        # 过滤机器人（包括自己）触发的事件，避免自己触发自己
        if event.actor.is_bot or event.case is None:
            return None
        async with self.db.session() as s, s.begin():
            repo = await self._repo(s, event)
            if repo.mode == "paused":
                return None
            case = await self._case(s, repo, event)
            name = event_name(event)
            ctx = GuardContext(actor=event.actor, facts={"author": case.author_login})
            current = CaseState(case.state)
            t = resolve(current, name, ctx)
            if t is None:
                log.debug("no transition: case=%s state=%s event=%s", case.id, current, name)
                return None
            s.add(TransitionLog(case_id=case.id, from_state=current, to_state=t.to, event=name))
            case.state = t.to
            case.state_version += 1
            await self._on_enter(s, repo, case, t.to)
            log.info("case %s: %s --%s--> %s", case.id, current, name, t.to)
            return t.to

    async def _repo(self, s: AsyncSession, event: DomainEvent) -> Repo:
        repo = await s.scalar(
            select(Repo).where(
                Repo.platform == event.repo.platform, Repo.full_name == event.repo.full_name
            )
        )
        if repo is None:
            repo = Repo(
                platform=event.repo.platform,
                full_name=event.repo.full_name,
                mode=self.default_mode,
            )
            s.add(repo)
            await s.flush()
        return repo

    async def _case(self, s: AsyncSession, repo: Repo, event: DomainEvent) -> Case:
        assert event.case is not None
        case = await s.scalar(
            select(Case).where(
                Case.repo_id == repo.id,
                Case.kind == event.case.kind,
                Case.number == event.case.number,
            )
        )
        if case is None:
            case = Case(
                repo_id=repo.id,
                kind=event.case.kind,
                number=event.case.number,
                state=CaseState.NEW,
                state_version=0,
                author_login=event.actor.login if event.name.endswith(".opened") else None,
            )
            s.add(case)
            await s.flush()
        return case

    async def _on_enter(self, s: AsyncSession, repo: Repo, case: Case, state: CaseState) -> None:
        if state is CaseState.INTAKE:
            await self.gate.propose(
                s, repo=repo, case=case, action="comment", payload={"body": ACK_BODY}
            )
