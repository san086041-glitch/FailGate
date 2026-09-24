"""状态转换表（技术方案第 5 节）。

表是纯数据 + 纯函数守卫，便于单测和评审；同一 (状态, 事件) 有多行时按顺序取第一个守卫通过的，
所以兜底行必须放在最后。skill.done 由能力模块完成时发出（见 orchestrator/pipeline.py）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from warden.platforms.base import User

from .states import ACTIVE, CaseState

S = CaseState
PROVEN_LEVELS = frozenset({"L1", "L2", "L3"})


@dataclass(frozen=True)
class GuardContext:
    actor: User | None = None
    facts: Mapping[str, Any] = field(default_factory=dict)


Guard = Callable[[GuardContext], bool]


@dataclass(frozen=True)
class Transition:
    src: frozenset[CaseState]
    event: str
    to: CaseState
    guard: Guard | None = None


def _can_write(ctx: GuardContext) -> bool:
    return ctx.actor is not None and ctx.actor.can_write


def _is_author(ctx: GuardContext) -> bool:
    return ctx.actor is not None and ctx.actor.login == ctx.facts.get("author")


def _fact(key: str, pred: Callable[[Any], bool]) -> Guard:
    return lambda ctx: pred(ctx.facts.get(key))


def _all(*guards: Guard) -> Guard:
    return lambda ctx: all(g(ctx) for g in guards)


_budget_ok = _fact("budget_ok", lambda v: v is not False)

TABLE: tuple[Transition, ...] = (
    Transition(frozenset({S.NEW}), "issue.opened", S.INTAKE),
    Transition(frozenset({S.INTAKE}), "skill.done", S.TRIAGING),
    Transition(frozenset({S.TRIAGING}), "skill.done", S.DEDUPING),
    # 查重完成后的分流：重复 > 提问 > 可复现的 bug > 其余只分诊
    Transition(
        frozenset({S.DEDUPING}), "skill.done", S.DUP_SUSPECTED,
        _fact("dup_high", lambda v: v is True),
    ),
    Transition(
        frozenset({S.DEDUPING}), "skill.done", S.ANSWERING,
        _fact("type", lambda v: v == "question"),
    ),
    Transition(
        frozenset({S.DEDUPING}), "skill.done", S.REPRODUCING,
        _all(
            _fact("type", lambda v: v == "bug"),
            _fact("repro_enabled", lambda v: v is True),
            _budget_ok,
        ),
    ),
    Transition(frozenset({S.DEDUPING}), "skill.done", S.TRIAGE_ONLY),
    Transition(frozenset({S.ANSWERING}), "skill.done", S.ANSWERED),
    Transition(
        frozenset({S.REPRODUCING}), "skill.done", S.REPRODUCED,
        _fact("evidence_level", lambda v: v in PROVEN_LEVELS),
    ),
    Transition(frozenset({S.REPRODUCING}), "skill.done", S.NEED_INFO),
    # 提问者补充信息后重新复现
    Transition(frozenset({S.NEED_INFO}), "comment.created", S.REPRODUCING, _is_author),
    Transition(frozenset({S.REPRODUCED}), "cmd.fix", S.FIXING, _all(_can_write, _budget_ok)),
    Transition(
        frozenset({S.FIXING}), "skill.done", S.PR_OPENED,
        _fact("fbpa_passed", lambda v: v is True),
    ),
    Transition(frozenset({S.FIXING}), "skill.done", S.FAILED),
    # 任意活跃状态
    Transition(ACTIVE, "budget.exceeded", S.FAILED),
    Transition(ACTIVE, "issue.closed", S.CLOSED),
    Transition(ACTIVE, "pull.closed", S.CLOSED),
    Transition(ACTIVE, "cmd.ignore", S.IGNORED, _can_write),
    Transition(frozenset({S.CLOSED}), "issue.reopened", S.NEW),
)


def resolve(
    state: CaseState, event: str, ctx: GuardContext, table: tuple[Transition, ...] = TABLE
) -> Transition | None:
    for t in table:
        if state in t.src and t.event == event and (t.guard is None or t.guard(ctx)):
            return t
    return None
