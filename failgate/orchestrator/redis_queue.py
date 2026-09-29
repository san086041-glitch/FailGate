"""Redis 队列后端（QUEUE_BACKEND=redis，ADR 0023）：arq 做队列和 worker，redis-py 的锁做 Case 锁。

- 两个 arq 队列：`failgate:events`、`failgate:sandbox`，各自一个 arq Worker，并发分别是
  EVENTS_CONCURRENCY / SANDBOX_CONCURRENCY（arq 的 max_jobs）。
- 任务 id 带幂等键：事件用 delivery id，沙箱任务用 case id + state_version；同一个 id
  排队中或结果还没过期时重复入队会被 arq 忽略。
- worker 崩溃时 arq 会把进行中的任务重投（至少一次），重复执行由 Dispatcher 挡掉
  （deliveries.finished_at、state_version）。
- serve 进程可以同时跑两条车道（WORKER_LANES，默认都跑），也可以只收 webhook，
  另起 `failgate worker --lanes sandbox` 这样的独立进程。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Collection
from typing import Any

from arq import Worker as ArqWorker
from arq import func
from arq.connections import ArqRedis, RedisSettings, create_pool

from failgate.platforms.base import DomainEvent

from .dispatch import Dispatcher

log = logging.getLogger(__name__)

EVENTS_QUEUE = "failgate:events"
SANDBOX_QUEUE = "failgate:sandbox"
LANES = ("events", "sandbox")


async def _handle_event(ctx: dict[str, Any], payload: dict[str, Any]) -> None:
    dispatcher: Dispatcher = ctx["dispatcher"]
    await dispatcher.handle_event(DomainEvent.model_validate(payload))


async def _run_sandbox(ctx: dict[str, Any], case_id: int, version: int) -> None:
    dispatcher: Dispatcher = ctx["dispatcher"]
    await dispatcher.run_sandbox(case_id, version)


class RedisCaseLocks:
    """Case 锁：`SET key token NX PX` + 比对 token 再删（redis-py 的 Lock）。

    timeout 是锁的最长持有时间：持锁的进程崩溃后到期自动释放。events 车道的任务
    只跑快阶段（几十秒），取任务超时时间，不需要续期。
    """

    def __init__(self, redis: ArqRedis, *, timeout_s: float, wait_s: float) -> None:
        self.redis = redis
        self.timeout_s = timeout_s
        self.wait_s = wait_s

    @contextlib.asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        lock = self.redis.lock(f"failgate:lock:{key}", timeout=self.timeout_s,
                               sleep=0.2, blocking_timeout=self.wait_s)
        if not await lock.acquire():
            raise TimeoutError(f"case lock {key} not acquired in {self.wait_s}s")
        try:
            yield
        finally:
            # 超时后锁可能已经被别人拿走：只释放自己的，不是自己的就算了
            with contextlib.suppress(Exception):
                await lock.release()


class RedisQueues:
    def __init__(
        self,
        url: str,
        *,
        events_concurrency: int = 4,
        sandbox_concurrency: int = 1,
        events_timeout_s: int = 600,
        sandbox_timeout_s: int = 1200,
        max_tries: int = 3,
    ) -> None:
        self.settings = RedisSettings.from_dsn(url)
        self.events_concurrency = max(1, events_concurrency)
        self.sandbox_concurrency = max(1, sandbox_concurrency)
        self.events_timeout_s = events_timeout_s
        self.sandbox_timeout_s = sandbox_timeout_s
        self.max_tries = max_tries
        self.pool: ArqRedis | None = None
        self.locks: RedisCaseLocks | None = None
        self.dispatcher: Dispatcher | None = None

    async def connect(self) -> None:
        if self.pool is None:
            self.pool = await create_pool(self.settings)
            self.locks = RedisCaseLocks(self.pool, timeout_s=self.events_timeout_s,
                                        wait_s=self.events_timeout_s)

    def bind(self, dispatcher: Dispatcher) -> None:
        self.dispatcher = dispatcher

    async def enqueue_event(self, event: DomainEvent) -> None:
        assert self.pool is not None, "call connect() first"
        await self.pool.enqueue_job("handle_event", event.model_dump(mode="json"),
                                    _job_id=f"event:{event.delivery_id}",
                                    _queue_name=EVENTS_QUEUE)

    async def enqueue_sandbox(self, case_id: int, version: int) -> None:
        assert self.pool is not None, "call connect() first"
        await self.pool.enqueue_job("run_sandbox", case_id, version,
                                    _job_id=f"sandbox:{case_id}:{version}",
                                    _queue_name=SANDBOX_QUEUE)

    def _worker(self, lane: str, *, burst: bool = False) -> ArqWorker:
        assert self.pool is not None and self.dispatcher is not None
        if lane == "events":
            fn = func(_handle_event, name="handle_event", timeout=self.events_timeout_s,
                      max_tries=self.max_tries)
            queue, jobs = EVENTS_QUEUE, self.events_concurrency
        else:
            fn = func(_run_sandbox, name="run_sandbox", timeout=self.sandbox_timeout_s,
                      max_tries=self.max_tries)
            queue, jobs = SANDBOX_QUEUE, self.sandbox_concurrency
        return ArqWorker(
            functions=[fn], queue_name=queue, redis_pool=self.pool, max_jobs=jobs,
            ctx={"dispatcher": self.dispatcher}, handle_signals=False, burst=burst,
            poll_delay=0.2, keep_result=60,
        )

    async def run_forever(self, lanes: Collection[str] = LANES) -> None:
        await self.connect()
        workers = [self._worker(lane) for lane in LANES if lane in lanes]
        # 不调 arq Worker.close()：它会关掉共享的连接池（self.pool 由 close() 统一关）。
        # 被取消时进行中的任务跟着取消，arq 之后按"进行中"标记过期重投
        await asyncio.gather(*(w.main() for w in workers))

    async def drain(self) -> int:
        """burst 模式把两个队列跑空（测试用）。返回值没有意义，固定 0。"""
        await self.connect()
        assert self.pool is not None
        while True:
            for lane in LANES:
                w = self._worker(lane, burst=True)
                await w.main()
            pending = sum([await self.pool.zcard(q) for q in (EVENTS_QUEUE, SANDBOX_QUEUE)])
            if not pending:
                return 0

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.aclose()
            self.pool = None
