"""Redis 后端（arq + redis-py 锁）的集成测试：连不上 Redis 时跳过。

本地：`docker run -d -p 127.0.0.1:6379:6379 redis:7-alpine`；CI 里有 redis 服务容器。
用第 15 号库，开始前清空。
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from conftest import SPANS, _harness, issue_event, make_settings
from test_verify_pipeline import PR, FakeVerifyRunner, pr_case, pull_event, verification

from failgate.orchestrator.redis_queue import RedisCaseLocks, RedisQueues
from failgate.orchestrator.states import CaseState
from failgate.verify.engine import ClaimVerdict

REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:6379/15")

pytestmark = pytest.mark.redis


async def _redis_or_skip() -> None:
    from redis.asyncio import Redis

    client = Redis.from_url(REDIS_URL)
    try:
        await asyncio.wait_for(client.ping(), 5)
        await client.flushdb()
    except Exception:
        pytest.skip(f"Redis not reachable at {REDIS_URL}")
    finally:
        await client.aclose()


async def test_events_and_sandbox_jobs_go_through_redis(tmp_path):
    await _redis_or_skip()
    runner = FakeVerifyRunner(verification(ClaimVerdict.VERIFIED))
    settings = make_settings(tmp_path, queue_backend="redis", redis_url=REDIS_URL)
    async for h in _harness(settings, verify_runner=runner):
        assert isinstance(h.failgate.worker, RedisQueues)
        SPANS.clear()
        r = await h.send("pull_request", pull_event("opened"), "p-1")
        assert r.status_code == 202
        await h.send("issues", issue_event("opened", 1), "d-1")
        # 同一个投递再入队一次：arq 按任务 id 去重
        again = h.failgate.platforms["github"].parse_event(
            {"X-GitHub-Event": "issues", "X-GitHub-Delivery": "d-1"},
            json.dumps(issue_event("opened", 1)).encode())
        assert again is not None
        await h.failgate.worker.enqueue_event(again)
        await h.failgate.worker.drain()
        assert (await pr_case(h)).state == CaseState.VERIFIED
        # trace 上下文跟着任务参数进了 Redis：沙箱任务仍在触发它的那条 trace 里（ADR 0025）
        spans = SPANS.get_finished_spans()
        root = next(s for s in spans if s.name == "webhook github/pull_request")
        job = next(s for s in spans if s.name == "sandbox job")
        assert job.context.trace_id == root.context.trace_id
        # Baggage（Langfuse 会话）也跟着任务进了 Redis
        assert job.attributes["langfuse.session.id"] == "github:acme/widgets:pull:12"
        assert runner.calls == [("verify", "acme/widgets", PR)]
        cases = (await h.client.get("/api/cases")).json()
        issue = next(c for c in cases if c["kind"] == "issue")
        assert issue["state"] == "TRIAGE_ONLY"


async def test_redis_case_lock_is_exclusive():
    await _redis_or_skip()
    q = RedisQueues(REDIS_URL)
    await q.connect()
    try:
        assert q.pool is not None
        locks = RedisCaseLocks(q.pool, timeout_s=5, wait_s=0.3)
        async with locks.hold("k"):
            with pytest.raises(TimeoutError):
                async with locks.hold("k"):
                    pass
            async with locks.hold("other"):
                pass
        # 释放之后可以再拿
        async with locks.hold("k"):
            pass
    finally:
        await q.close()
