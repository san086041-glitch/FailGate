"""CaseMachine：把事件应用到对应 Case 上。

外部事件（webhook）走 handle()：定位或创建 Repo/Case → 查转换表 → 写转换日志。
内部事件（能力模块完成、预算耗尽）由 pipeline 在自己的事务里调用 apply()。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from failgate.db import Case, Database, Repo, TransitionLog
from failgate.platforms.base import DomainEvent, User

from .commands import parse_command
from .states import CaseState
from .transitions import GuardContext, resolve

if TYPE_CHECKING:
    from failgate.index.store import IssueIndex

log = logging.getLogger(__name__)


# 实时查询事件发起人对仓库的权限；返回 None 表示查不了（例如没配置 GitHub App），退回 association
PermissionLookup = Callable[[DomainEvent], Awaitable[str | None]]


@dataclass(frozen=True)
class Outcome:
    case_id: int
    state: CaseState


def event_name(event: DomainEvent) -> str:
    if event.name == "comment.created":
        cmd = parse_command(event.body)
        if cmd is not None:
            return f"cmd.{cmd.verb}"
    return event.name


class CaseMachine:
    def __init__(
        self,
        db: Database,
        default_mode: str = "shadow",
        index: IssueIndex | None = None,
        permissions: PermissionLookup | None = None,
    ) -> None:
        self.db = db
        self.default_mode = default_mode
        # 查重语料：每个经过的 issue 都写进索引，供之后的新 issue 比较
        self.index = index
        self.permissions = permissions

    async def handle(self, event: DomainEvent) -> Outcome | None:
        """应用一个外部事件；发生状态转换时返回新状态，否则返回 None。"""
        # 过滤机器人（包括自己）触发的事件，避免自己触发自己
        if event.actor.is_bot or event.case is None:
            return None
        name = event_name(event)
        actor = event.actor
        if name.startswith("cmd.") and self.permissions is not None:
            # 命令要看发起人有没有写权限：在事务外实时查询，不信任评论里的任何说法
            actor = actor.model_copy(update={"permission": await self._permission(event)})
        async with self.db.session() as s, s.begin():
            repo = await self._repo(s, event)
            if repo.mode == "paused":
                return None
            case = await self._case(s, repo, event)
            if self.index is not None and case.kind == "issue" and event.name.startswith("issue."):
                await self.index.upsert(
                    s,
                    repo.id,
                    number=case.number,
                    title=case.title,
                    body=case.body,
                    state="closed" if event.name == "issue.closed" else "open",
                    url=f"https://github.com/{repo.full_name}/issues/{case.number}",
                    created_at=case.created_at,
                )
            state = await self.apply(s, case, name, actor=actor)
            return Outcome(case.id, state) if state is not None else None

    async def _permission(self, event: DomainEvent) -> str | None:
        assert self.permissions is not None
        try:
            return await self.permissions(event)
        except Exception:
            # 查不到就按"没有权限"处理（失败即拒绝），而不是退回宽松的 association
            log.exception("permission lookup failed for %s", event.actor.login)
            return "none"

    async def apply(
        self,
        s: AsyncSession,
        case: Case,
        name: str,
        *,
        actor: User | None = None,
        facts: dict[str, Any] | None = None,
    ) -> CaseState | None:
        ctx = GuardContext(actor=actor, facts={"author": case.author_login, **(facts or {})})
        current = CaseState(case.state)
        t = resolve(current, name, ctx)
        if t is None:
            log.debug("no transition: case=%s state=%s event=%s", case.id, current, name)
            return None
        s.add(TransitionLog(case_id=case.id, from_state=current, to_state=t.to, event=name,
                            actor=actor.login if actor is not None else None))
        case.state = t.to
        case.state_version += 1
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
                installation_id=event.installation_id,
            )
            try:
                # 新仓库的头几个事件可能在快车道里同时处理（不同 Case、不同的锁，ADR 0023）：
                # 在保存点里插入，撞上唯一约束说明别的任务刚建好，读它的那一行
                async with s.begin_nested():
                    s.add(repo)
            except IntegrityError:
                found = await s.scalar(
                    select(Repo).where(
                        Repo.platform == event.repo.platform,
                        Repo.full_name == event.repo.full_name,
                    )
                )
                assert found is not None
                repo = found
        elif event.installation_id and repo.installation_id != event.installation_id:
            # App 被卸载后重新安装，安装 ID 会变
            repo.installation_id = event.installation_id
        return repo

    async def _case(self, s: AsyncSession, repo: Repo, event: DomainEvent) -> Case:
        assert event.case is not None
        case = await s.scalar(
            select(Case).where(
                Case.repo_id == repo.id,
                Case.kind == event.case.kind,
                Case.number == event.case.number,
            ).with_for_update()
        )
        opened = event.name.endswith(".opened")
        if case is None:
            case = Case(
                repo_id=repo.id,
                kind=event.case.kind,
                number=event.case.number,
                state=CaseState.NEW,
                state_version=0,
                author_login=event.actor.login if opened else None,
                title=event.title,
                body=event.body,
                spent_usd=0.0,
            )
            s.add(case)
            await s.flush()
        elif event.name in {"issue.edited", "pull.edited"} or (opened and not case.title):
            case.title, case.body = event.title, event.body
        return case
