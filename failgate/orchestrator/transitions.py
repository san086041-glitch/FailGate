"""状态转换表（技术方案第 5 节）。

表是纯数据 + 纯函数守卫，便于单测和评审；同一 (状态, 事件) 有多行时按顺序取第一个守卫通过的，
所以兜底行必须放在最后。skill.done 由能力模块完成时发出（见 orchestrator/pipeline.py）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from failgate.platforms.base import User

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
PR_RESULTS = frozenset({S.VERIFIED, S.REFUTED, S.INCONCLUSIVE, S.NO_CLAIM})

TABLE: tuple[Transition, ...] = (
    Transition(frozenset({S.NEW}), "issue.opened", S.INTAKE),
    Transition(frozenset({S.INTAKE}), "skill.done", S.TRIAGING),
    Transition(frozenset({S.TRIAGING}), "skill.done", S.DEDUPING),
    # 查重完成后的分流：提问 > 重复 > 可复现的 bug > 其余只分诊。
    # 提问排在重复前面：对提问者来说直接回答比"建议关闭为重复"有用，重复 issue 里维护者的回答
    # 本身也会作为答疑的资料被引用，查重结果照样写进汇总评论（实测案例见 ADR 0005）
    Transition(
        frozenset({S.DEDUPING}), "skill.done", S.ANSWERING,
        _fact("type", lambda v: v == "question"),
    ),
    Transition(
        frozenset({S.DEDUPING}), "skill.done", S.DUP_SUSPECTED,
        _fact("dup_high", lambda v: v is True),
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
    # PR Case：打开、推新提交、改描述（可能加了 fixes #N）、重新打开、维护者命令 → 核验
    Transition(frozenset({S.NEW}), "pull.opened", S.VERIFYING),
    Transition(frozenset({S.CLOSED}), "pull.reopened", S.VERIFYING),
    Transition(PR_RESULTS, "pull.synchronize", S.VERIFYING),
    Transition(PR_RESULTS, "pull.edited", S.VERIFYING),
    # 核验中出错（比如 GitHub 暂时连不上）会停在 VERIFYING，维护者可以手动再触发一次
    Transition(PR_RESULTS | {S.VERIFYING}, "cmd.verify", S.VERIFYING, _can_write),
    # 重新封存由有写权限的维护者决定，谁也不能给自己改评分标准
    Transition(PR_RESULTS, "cmd.reseal", S.RESEALING, _can_write),
    Transition(frozenset({S.RESEALING}), "skill.done", S.VERIFYING),
    Transition(frozenset({S.VERIFYING}), "skill.done", S.NO_CLAIM,
               _fact("verdict", lambda v: v is None)),
    Transition(frozenset({S.VERIFYING}), "skill.done", S.VERIFIED,
               _fact("verdict", lambda v: v == "VERIFIED")),
    Transition(frozenset({S.VERIFYING}), "skill.done", S.REFUTED,
               _fact("verdict", lambda v: v == "REFUTED")),
    Transition(frozenset({S.VERIFYING}), "skill.done", S.INCONCLUSIVE),
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
