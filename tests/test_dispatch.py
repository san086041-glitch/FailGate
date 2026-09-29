"""两条车道（W6，ADR 0023）：沙箱任务不再挡住快事件；过期任务、重投事件被跳过。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

from conftest import Harness, _harness, comment_event, issue_event, make_settings
from sqlalchemy import func, select
from test_verify_pipeline import PR, FakeVerifyRunner, pr_case, pull_event, verification

from failgate.db import Case, TransitionLog, VerificationRecord
from failgate.orchestrator.dispatch import LocalCaseLocks
from failgate.orchestrator.states import CaseState
from failgate.orchestrator.worker import Worker
from failgate.verify.engine import ClaimVerdict, Verification


class GatedRunner(FakeVerifyRunner):
    """每次核验都停在一个闸门前，测试手动放行：模拟跑几分钟的沙箱任务。"""

    def __init__(self, *results: Verification) -> None:
        super().__init__(*results)
        self.gates: list[asyncio.Event] = []

    async def verify(self, repo: str, number: int) -> Verification:
        gate = asyncio.Event()
        self.gates.append(gate)
        await gate.wait()
        return await super().verify(repo, number)


async def wait_for(cond: Callable[[], Awaitable[bool]]) -> None:
    """轮询数据库直到条件成立（最多 5 秒）。"""
    async with asyncio.timeout(5):
        while not await cond():  # noqa: ASYNC110 — 等的是另一个任务写库，没有事件可等
            await asyncio.sleep(0.02)


async def until_state(h: Harness, kind: str, number: int, want: CaseState) -> None:
    async def ok() -> bool:
        return await case_state(h, kind, number) == want
    await wait_for(ok)


async def until_version(h: Harness, n: int) -> None:
    async def ok() -> bool:
        async with h.failgate.db.session() as s:
            v = await s.scalar(select(Case.state_version).where(Case.kind == "pull"))
        return (v or 0) >= n
    await wait_for(ok)


async def until_gates(runner: GatedRunner, n: int) -> None:
    async def ok() -> bool:
        return len(runner.gates) >= n
    await wait_for(ok)


async def case_state(h: Harness, kind: str, number: int) -> str | None:
    async with h.failgate.db.session() as s:
        return await s.scalar(select(Case.state).where(Case.kind == kind, Case.number == number))


async def stop_worker(h: Harness, task: asyncio.Task[None]) -> None:
    """先等两个队列里的任务都做完（包括写回），再取消消费协程并等它真正结束。

    直接 cancel 会打断正在进行的数据库操作，之后关连接池可能卡住。
    """
    worker = h.failgate.worker
    assert isinstance(worker, Worker)
    async with asyncio.timeout(5):
        await worker.events.join()
        await worker.sandbox.join()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_fast_event_is_not_blocked_by_a_running_sandbox_job(tmp_path):
    runner = GatedRunner(verification(ClaimVerdict.VERIFIED))
    async for h in _harness(make_settings(tmp_path), verify_runner=runner):
        task = asyncio.create_task(h.failgate.worker.run_forever())
        try:
            await h.send("pull_request", pull_event("opened"), "p-1")
            await until_gates(runner, 1)
            # 核验卡在沙箱里的时候来了一个新 issue：它照样走完分诊
            await h.send("issues", issue_event("opened", 1), "d-1")
            await until_state(h, "issue", 1, CaseState.TRIAGE_ONLY)
            assert await case_state(h, "pull", PR) == CaseState.VERIFYING
            runner.gates[0].set()
            await until_state(h, "pull", PR, CaseState.VERIFIED)
        finally:
            await stop_worker(h, task)


async def test_reverify_during_a_run_discards_the_stale_result(tmp_path):
    runner = GatedRunner(
        verification(ClaimVerdict.REFUTED, reasons=["tamper:exam_modified"]),
        verification(ClaimVerdict.VERIFIED, head="2" * 40),
    )
    async for h in _harness(make_settings(tmp_path), verify_runner=runner):
        task = asyncio.create_task(h.failgate.worker.run_forever())
        try:
            await h.send("pull_request", pull_event("opened"), "p-1")
            await until_gates(runner, 1)
            # 第一轮还在跑，维护者又要求重新核验（VERIFYING → VERIFYING，state_version + 1）
            await h.send("issue_comment", comment_event("/failgate verify", PR, login="maint",
                                                        association="MEMBER", on_pull=True), "c-1")
            await until_version(h, 2)
            runner.gates[0].set()
            # 第一轮的结果作废；第二轮（沙箱并发 1，排在后面）接着跑
            await until_gates(runner, 2)
            assert await case_state(h, "pull", PR) == CaseState.VERIFYING
            runner.gates[1].set()
            await until_state(h, "pull", PR, CaseState.VERIFIED)
            async with h.failgate.db.session() as s:
                recs = (await s.scalars(select(VerificationRecord))).all()
            assert [r.verdict for r in recs] == ["VERIFIED"]
        finally:
            await stop_worker(h, task)


async def test_stale_sandbox_job_is_skipped(tmp_path):
    runner = FakeVerifyRunner(verification(ClaimVerdict.VERIFIED))
    async for h in _harness(make_settings(tmp_path), verify_runner=runner):
        await h.send("pull_request", pull_event("opened"), "p-1")
        await h.failgate.worker.drain()
        case = await pr_case(h)
        assert case.state == CaseState.VERIFIED and len(runner.calls) == 1
        dispatcher = h.failgate.dispatcher
        assert dispatcher is not None
        # 老版本号的任务（比如重投）：不再跑
        await dispatcher.run_sandbox(case.id, case.state_version - 1)
        assert len(runner.calls) == 1


async def test_redelivered_event_is_handled_once(harness: Harness):
    payload = issue_event("opened", 1)
    await harness.send("issues", payload, "d-1")
    await harness.failgate.worker.drain()
    body = json.dumps(payload).encode()
    event = harness.failgate.platforms["github"].parse_event(
        {"X-GitHub-Event": "issues", "X-GitHub-Delivery": "d-1"}, body)
    assert event is not None and harness.failgate.dispatcher is not None
    async with harness.failgate.db.session() as s:
        before = await s.scalar(select(func.count()).select_from(TransitionLog))
    # 队列至少一次投递：同一个投递再来一次，什么也不做
    await harness.failgate.dispatcher.handle_event(event)
    async with harness.failgate.db.session() as s:
        after = await s.scalar(select(func.count()).select_from(TransitionLog))
    assert before == after


async def test_local_case_locks_serialize_same_key_only():
    locks = LocalCaseLocks()
    order: list[str] = []

    async def job(key: str, name: str, hold: float) -> None:
        async with locks.hold(key):
            order.append(f"{name}+")
            await asyncio.sleep(hold)
            order.append(f"{name}-")

    await asyncio.gather(job("a", "a1", 0.05), job("a", "a2", 0.0), job("b", "b1", 0.0))
    # 同一个 key 串行：a2 在 a1 结束之后才开始；b1 不用等 a1
    assert order.index("a2+") > order.index("a1-")
    assert order.index("b1-") < order.index("a1-")
    assert locks._locks == {}



async def test_first_events_of_a_new_repo_can_run_concurrently(harness: Harness):
    # 新仓库的头两个事件属于不同的 Case（不同的锁），在快车道里同时处理：
    # 两边都要建 Repo 行，后到的那个不能因为唯一约束失败
    parse = harness.failgate.platforms["github"].parse_event
    events = [
        parse({"X-GitHub-Event": "issues", "X-GitHub-Delivery": f"d-{n}"},
              json.dumps(issue_event("opened", n)).encode())
        for n in (1, 2, 3)
    ]
    outcomes = await asyncio.gather(*(harness.failgate.machine.handle(e) for e in events if e))
    assert all(o is not None for o in outcomes)
    async with harness.failgate.db.session() as s:
        assert await s.scalar(select(func.count()).select_from(Case)) == 3
