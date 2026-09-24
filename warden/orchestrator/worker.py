"""事件队列与 worker。

M0 用进程内 asyncio.Queue：进程重启会丢失未处理的事件（deliveries 表里仍有记录）。
M1 换成 Redis 队列（arq），并加上 Case 级分布式锁。
"""

from __future__ import annotations

import asyncio
import logging

from warden.platforms.base import DomainEvent

from .machine import CaseMachine

log = logging.getLogger(__name__)

EventQueue = asyncio.Queue[DomainEvent]


class Worker:
    def __init__(self, queue: EventQueue, machine: CaseMachine) -> None:
        self.queue = queue
        self.machine = machine

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
        try:
            await self.machine.handle(event)
        except Exception:
            log.exception("failed to handle event %s (%s)", event.delivery_id, event.name)
        finally:
            self.queue.task_done()
