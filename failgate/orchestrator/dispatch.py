"""两条车道的调度逻辑（W6，ADR 0023）：和队列后端无关。

基线测量（eval/reports/w6__latency_baseline__20260929.md）显示延迟全在排队上：
一个核验（约 1 分钟）挡住后面所有的分诊；快任务自己（约 18 秒、全是等网络）也会压垮
单个 worker。所以拆成两条车道：

    events 车道（并发 N）：webhook 事件 → 状态机 → 快阶段（Intake / 分诊 / 查重 / 答疑）
                          → 写回平台；走到沙箱阶段就停下，投一个沙箱任务
    sandbox 车道（并发 M）：复现 / 核验 / 重新封存 → 之后的阶段 → 写回平台

- 同一个 Case 的事件在 events 车道里用 Case 锁串行（保证顺序、不重复跑同一阶段）；
  不同 Case 可以同时处理。
- 沙箱任务不拿 Case 锁（一跑就是几分钟，会把这个 Case 的新事件全堵住）。
  并发改动靠 Pipeline 写结果时比对 state_version：运行期间 Case 被关闭或重新触发，
  这次结果作废，由新事件投的新任务负责。
- 队列是"至少一次"投递（Redis 后端的 worker 崩溃后会重投）：事件处理完会在 deliveries
  表上记 finished_at，重投时跳过；沙箱任务带着投递时的 state_version，过期就跳过。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from opentelemetry.trace import StatusCode
from sqlalchemy import select

from failgate import tracing
from failgate.db import Case, Database, Delivery
from failgate.ingress.dedupe import mark_delivery, now
from failgate.platforms.base import DomainEvent
from failgate.policy.executor import EffectExecutor

from .machine import CaseMachine
from .pipeline import Pipeline
from .states import CaseState

log = logging.getLogger(__name__)

SANDBOX_STATES = frozenset({CaseState.REPRODUCING, CaseState.VERIFYING, CaseState.RESEALING})

# 投沙箱任务：(case_id, 投递时的 state_version, trace 上下文)
SandboxEnqueue = Callable[[int, int, dict[str, str]], Awaitable[None]]


class CaseLocks(Protocol):
    def hold(self, key: str) -> AbstractAsyncContextManager[None]: ...


class LocalCaseLocks:
    """进程内的 Case 锁（本地后端和测试用）；多进程部署用 Redis 锁。"""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._users: dict[str, int] = {}

    @contextlib.asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._users[key] = self._users.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._users[key] -= 1
            if not self._users[key]:
                # 没人在用就删掉，锁表不会随 Case 数量无限增长
                del self._users[key], self._locks[key]


def case_key(event: DomainEvent) -> str | None:
    if event.case is None:
        return None
    ref = event.case
    return f"{ref.repo.platform}:{ref.repo.full_name}:{ref.kind}:{ref.number}"


class Dispatcher:
    def __init__(
        self,
        db: Database,
        machine: CaseMachine,
        pipeline: Pipeline | None,
        executor: EffectExecutor | None,
        locks: CaseLocks,
        enqueue_sandbox: SandboxEnqueue,
    ) -> None:
        self.db = db
        self.machine = machine
        self.pipeline = pipeline
        self.executor = executor
        self.locks = locks
        self.enqueue_sandbox = enqueue_sandbox

    async def handle_event(self, event: DomainEvent) -> None:
        """events 车道的一个任务。异常都在这里吞掉并记日志，不让队列重试一个坏事件。"""
        if await self._already_done(event.delivery_id):
            log.info("delivery %s already handled, skipped", event.delivery_id)
            return
        case_id: int | None = None
        key = case_key(event)
        attrs: dict[str, str | int] = {"failgate.delivery_id": event.delivery_id,
                                       "failgate.event": event.name}
        if key:
            attrs |= {tracing.CASE: key, tracing.SESSION: key}
        # 接在 webhook 入口的 span 下面（上下文跟着事件进了队列）
        with tracing.tracer.start_as_current_span(
            f"event {event.name}", context=tracing.extract(event.trace), attributes=attrs,
        ) as span:
            await self._mark(event, started_at=now())
            try:
                async with self.locks.hold(key) if key else contextlib.nullcontext():
                    outcome = await self.machine.handle(event)
                    if outcome is None:
                        return
                    case_id = outcome.case_id
                    span.set_attribute("failgate.case_id", case_id)
                    state = outcome.state
                    if self.pipeline is not None:
                        state = await self.pipeline.advance(case_id, stop_at=SANDBOX_STATES)
                    # 本轮快阶段提出的写操作（标签、汇总评论）在这里统一发出
                    if self.executor is not None:
                        await self.executor.flush(case_id)
                    span.set_attribute("failgate.state", str(state))
                    if (state in SANDBOX_STATES and self.pipeline is not None
                            and self.pipeline.runs(state)):
                        version = await self._version(case_id)
                        tracing.event("enqueue_sandbox", state=str(state), version=version)
                        await self.enqueue_sandbox(case_id, version, tracing.inject())
            except Exception as e:
                span.record_exception(e)
                span.set_status(StatusCode.ERROR)
                log.exception("failed to handle event %s (%s)", event.delivery_id, event.name)
            finally:
                await self._mark(event, finished_at=now(), case_id=case_id)

    async def run_sandbox(self, case_id: int, version: int,
                          trace: dict[str, str] | None = None) -> None:
        """sandbox 车道的一个任务：从沙箱阶段一直跑到流水线停下。

        trace 是投任务时的上下文：沙箱任务的 span 接在触发它的那个事件下面，
        即使中间隔着 Redis、换了进程。"""
        if self.pipeline is None:
            return
        with tracing.tracer.start_as_current_span(
            "sandbox job", context=tracing.extract(trace),
            attributes={"failgate.case_id": case_id, "failgate.state_version": version},
        ) as span:
            current = await self._version(case_id)
            if current != version:
                # 投递之后 Case 又被改过（关闭、重新触发）：由后来的任务负责
                span.set_attribute("failgate.status", "stale")
                log.info("sandbox job for case %s is stale (v%s, now v%s)",
                         case_id, version, current)
                return
            try:
                state = await self.pipeline.advance(case_id)
                span.set_attribute("failgate.state", str(state))
                if self.executor is not None:
                    await self.executor.flush(case_id)
            except Exception as e:
                span.record_exception(e)
                span.set_status(StatusCode.ERROR)
                log.exception("sandbox job failed on case %s", case_id)

    async def _version(self, case_id: int) -> int:
        async with self.db.session() as s:
            v = await s.scalar(select(Case.state_version).where(Case.id == case_id))
        return int(v or 0)

    async def _already_done(self, delivery_id: str) -> bool:
        async with self.db.session() as s:
            done = await s.scalar(
                select(Delivery.finished_at).where(Delivery.delivery_id == delivery_id))
        return done is not None

    async def _mark(self, event: DomainEvent, **fields: object) -> None:
        try:
            await mark_delivery(self.db, event.delivery_id, **fields)
        except Exception:
            # 计时失败不能影响事件处理
            log.exception("failed to record timing for %s", event.delivery_id)
