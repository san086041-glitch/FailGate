"""排队延迟测量（W6）：worker 计时、GitHub 时间戳匹配、汇总。"""

from datetime import UTC, datetime, timedelta

from conftest import REPO, Harness, issue_event
from sqlalchemy import select

from failgate.db import Delivery
from failgate.replay import latency as lat

T0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


async def test_worker_records_start_finish_and_case(harness: Harness):
    await harness.send("issues", issue_event("opened"), "d-1")
    await harness.failgate.worker.drain()
    async with harness.failgate.db.session() as s:
        d = (await s.execute(select(Delivery))).scalar_one()
    assert d.started_at is not None and d.finished_at is not None and d.case_id is not None
    assert d.received_at <= d.started_at <= d.finished_at

    rows = await lat.load_timings(harness.failgate.db, REPO, T0.replace(year=2000, tzinfo=None))
    assert len(rows) == 1
    row = rows[0]
    assert row.event == "issue.opened" and row.kind == "issue"
    # 分诊 → 查重 → TRIAGE_ONLY，没有进沙箱
    assert "TRIAGE_ONLY" in row.states and not row.sandbox
    assert row.queue_wait is not None and row.queue_wait >= 0
    assert lat.summarize(rows)["fast"]["n"] == 1


def _comment(login: str, created: datetime, updated: datetime | None = None,
             body: str = "") -> dict:
    return {"user": {"login": login}, "created_at": _iso(created),
            "updated_at": _iso(updated or created), "body": body}


def test_bot_reply_prefers_new_comment_then_edit():
    trigger = T0
    comments = [
        _comment("alice", T0 + timedelta(seconds=1)),
        # 新建的汇总评论
        _comment("failgate-dev-jian[bot]", T0 + timedelta(seconds=12)),
    ]
    assert lat.bot_reply_after(comments, trigger) == T0 + timedelta(seconds=12)
    # 之前就有的汇总评论，这次被编辑：看 updated_at
    edited = [_comment("failgate-dev-jian[bot]", T0 - timedelta(days=1),
                       T0 + timedelta(seconds=90))]
    assert lat.bot_reply_after(edited, trigger) == T0 + timedelta(seconds=90)
    # 触发之前的旧评论、没被编辑：不算
    stale = [_comment("failgate-dev-jian[bot]", T0 - timedelta(days=1))]
    assert lat.bot_reply_after(stale, trigger) is None
    # 指定 bot 登录名时忽略别的 bot
    other = [_comment("dependabot[bot]", T0 + timedelta(seconds=3))]
    assert lat.bot_reply_after(other, trigger, "failgate-dev-jian[bot]") is None


def test_command_trigger_picks_latest_command_before_receipt():
    comments = [
        _comment("maint", T0 - timedelta(hours=1), body="/failgate verify"),
        _comment("maint", T0 - timedelta(seconds=5), body="  /failgate verify"),
        _comment("maint", T0 - timedelta(seconds=2), body="thanks!"),
    ]
    assert lat.command_trigger(comments, T0) == T0 - timedelta(seconds=5)


def _row(received: float, start: float, finish: float, states: list[str]) -> lat.EventTiming:
    return lat.EventTiming(
        delivery_id=f"d{received}", event="issue.opened",
        received_at=T0 + timedelta(seconds=received),
        started_at=T0 + timedelta(seconds=start),
        finished_at=T0 + timedelta(seconds=finish), states=states,
    )


def test_summarize_splits_fast_and_sandbox():
    rows = [
        _row(0, 0, 90, ["VERIFYING", "VERIFIED"]),
        _row(10, 90, 95, ["TRIAGE_ONLY"]),
        _row(20, 95, 99, ["ANSWERED"]),
    ]
    s = lat.summarize(rows)
    assert s["sandbox"]["n"] == 1 and s["fast"]["n"] == 2
    assert s["fast"]["queue_wait"]["p50"] == 77.5 and s["fast"]["queue_wait"]["max"] == 80
    assert s["fast"]["github"] is None
    text = lat.render(rows, s, repo=REPO, title="t")
    assert "快事件" in text and "沙箱事件" in text and "| 3 |" in text


def test_percentile_nearest_rank():
    xs = [float(i) for i in range(1, 21)]
    assert lat.percentile(xs, 0.95) == 19.0
    assert lat.percentile([3.0], 0.95) == 3.0


def test_mixed_scenario_and_signed_send():
    import asyncio
    import hashlib
    import hmac

    import httpx

    from failgate.replay import loadgen

    plan = loadgen.mixed_scenario(REPO, pulls=[4, 6], fast=3, first_fast_at=0.0, interval=0.0,
                                  base=90001, maintainer="maint", installation=7)
    assert [p.event for p in plan[:2]] == ["issue_comment", "issue_comment"]
    assert [p.payload["issue"]["number"] for p in plan[2:]] == [90001, 90002, 90003]
    assert "pull_request" in plan[0].payload["issue"]
    assert plan[0].payload["comment"]["body"] == "/failgate verify"

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202)

    async def go() -> list[int]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await loadgen.send_plan(plan, "http://t/webhooks/github", "s3cret",
                                           client=client, echo=lambda _: None)

    assert asyncio.run(go()) == [202] * 5
    req = seen[0]
    want = "sha256=" + hmac.new(b"s3cret", req.content, hashlib.sha256).hexdigest()
    assert req.headers["X-Hub-Signature-256"] == want
    assert len({r.headers["X-GitHub-Delivery"] for r in seen}) == 5
