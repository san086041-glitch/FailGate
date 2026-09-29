"""进程内的队列后端（QUEUE_BACKEND=local，默认；测试也用它）。

两个 asyncio.Queue：events（并发 EVENTS_CONCURRENCY）和 sandbox（并发 SANDBOX_CONCURRENCY），
调度逻辑在 dispatch.Dispatcher。进程重启会丢掉还没处理的事件（deliveries 表里仍有记录）
和排队中的沙箱任务；要持久化和多进程部署用 Redis 后端（redis_queue.RedisQueues）。
"""

from __future__ import annotations

import asyncio
import logging

from failgate.platforms.base import DomainEvent

from .dispatch import Dispatcher, LocalCaseLocks

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, *, events_concurrency: int = 4, sandbox_concurrency: int = 1) -> None:
        self.events: asyncio.Queue[DomainEvent] = asyncio.Queue()
        self.sandbox: asyncio.Queue[tuple[int, int]] = asyncio.Queue()
        self.events_concurrency = max(1, events_concurrency)
        self.sandbox_concurrency = max(1, sandbox_concurrency)
        self.locks = LocalCaseLocks()
        self.dispatcher: Dispatcher | None = None

    def bind(self, dispatcher: Dispatcher) -> None:
        self.dispatcher = dispatcher

    async def enqueue_event(self, event: DomainEvent) -> None:
        await self.events.put(event)

    async def enqueue_sandbox(self, case_id: int, version: int) -> None:
        await self.sandbox.put((case_id, version))

    async def run_forever(self) -> None:
        loops = [self._events_loop() for _ in range(self.events_concurrency)]
        loops += [self._sandbox_loop() for _ in range(self.sandbox_concurrency)]
        await asyncio.gather(*loops)

    async def _events_loop(self) -> None:
        assert self.dispatcher is not None
        while True:
            event = await self.events.get()
            try:
                await self.dispatcher.handle_event(event)
            finally:
                self.events.task_done()

    async def _sandbox_loop(self) -> None:
        assert self.dispatcher is not None
        while True:
            case_id, version = await self.sandbox.get()
            try:
                await self.dispatcher.run_sandbox(case_id, version)
            finally:
                self.sandbox.task_done()

    async def drain(self) -> int:
        """按顺序处理完两个队列里当前（以及处理中新产生）的全部任务，返回处理的事件数。

        测试和 CLI 调试用：不并发，结果确定。
        """
        assert self.dispatcher is not None
        n = 0
        while not (self.events.empty() and self.sandbox.empty()):
            while not self.events.empty():
                await self.dispatcher.handle_event(self.events.get_nowait())
                self.events.task_done()
                n += 1
            while not self.sandbox.empty():
                await self.dispatcher.run_sandbox(*self.sandbox.get_nowait())
                self.sandbox.task_done()
        return n

    async def close(self) -> None:
        return None
