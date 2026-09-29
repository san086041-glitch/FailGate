"""事件队列与 worker。

M0/M1 用进程内 asyncio.Queue：进程重启会丢失未处理的事件（deliveries 表里仍有记录）。
之后换成 Redis 队列（arq），并加上 Case 级分布式锁。
"""

from __future__ import annotations

import asyncio
import logging

from failgate.db import Database
from failgate.ingress.dedupe import mark_delivery, now
from failgate.platforms.base import DomainEvent
from failgate.policy.executor import EffectExecutor

from .machine import CaseMachine
from .pipeline import Pipeline

log = logging.getLogger(__name__)

EventQueue = asyncio.Queue[DomainEvent]


class Worker:
    def __init__(
        self,
        queue: EventQueue,
        machine: CaseMachine,
        pipeline: Pipeline | None = None,
        executor: EffectExecutor | None = None,
        db: Database | None = None,
    ) -> None:
        self.queue = queue
        self.machine = machine
        self.pipeline = pipeline
        self.executor = executor
        # 给了 db 就在 deliveries 表上记开始 / 结束时间（W6 排队延迟测量）
        self.db = db

    async def run_forever(self) -> None:
        while True:
            event = await self.queue.get()
            await self._process(event)

    async def drain(self) -> int:
        """处理完队列里当前所有事件，返回处理条数（测试和 CLI 调试用）。"""
        n = 0
        while not self.queue.empty():
            await self._process(self.queue.get_nowait())
            n += 1
        return n

    async def _process(self, event: DomainEvent) -> None:
        case_id: int | None = None
        await self._mark(event, started_at=now())
        try:
            outcome = await self.machine.handle(event)
            if outcome is None:
                return
            case_id = outcome.case_id
            if self.pipeline is not None:
                await self.pipeline.advance(outcome.case_id)
            # 本轮各阶段提出的写操作（打标签、汇总评论）在这里统一发出
            if self.executor is not None:
                await self.executor.flush(outcome.case_id)
        except Exception:
            log.exception("failed to handle event %s (%s)", event.delivery_id, event.name)
        finally:
            await self._mark(event, finished_at=now(), case_id=case_id)
            self.queue.task_done()

    async def _mark(self, event: DomainEvent, **fields: object) -> None:
        if self.db is None:
            return
        try:
            await mark_delivery(self.db, event.delivery_id, **fields)
        except Exception:
            # 计时失败不能影响事件处理
            log.exception("failed to record timing for %s", event.delivery_id)
