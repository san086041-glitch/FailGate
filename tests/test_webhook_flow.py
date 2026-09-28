"""端到端：webhook → 去重 → 队列 → 状态机 → 策略层（影子模式）。"""

from conftest import REPO, Harness, comment_event, issue_event
from sqlalchemy import func, select, update

from failgate.db import Case, Effect, Repo
from failgate.policy.gate import PolicyGate


async def _case(h: Harness) -> dict:
    cases = (await h.client.get("/api/cases")).json()
    assert len(cases) == 1
    return (await h.client.get(f"/api/cases/{cases[0]['id']}")).json()


async def test_healthz(harness: Harness):
    r = await harness.client.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"


async def test_issue_opened_runs_intake_triage_dedup(harness: Harness):
    r = await harness.send("issues", issue_event("opened"), "d-1")
    assert r.status_code == 202 and r.json() == {"status": "queued"}
    assert await harness.failgate.worker.drain() == 1

    case = await _case(harness)
    # 复现在 M2 才启用，bug 走完查重后进入 TRIAGE_ONLY
    assert case["state"] == "TRIAGE_ONLY"
    assert [t["to"] for t in case["transitions"]] == [
        "INTAKE", "TRIAGING", "DEDUPING", "TRIAGE_ONLY",
    ]
    assert [r["skill"] for r in case["runs"]] == ["intake", "triage", "dedup"]
    # 仓库里只有这一个 issue：没有候选，查重不调用模型
    assert case["runs"][2]["output"]["verdict"] == "none" and case["runs"][2]["usd"] == 0
    assert case["spent_usd"] > 0
    actions = {e["action"]: e for e in case["effects"]}
    assert set(actions) == {"set_labels", "upsert_summary"}
    assert all(e["status"] == "shadowed" for e in case["effects"])
    # 仓库里不存在的标签被过滤掉
    assert actions["set_labels"]["payload"] == {"add": ["bug"]}
    summary = actions["upsert_summary"]["payload"]["body"]
    assert "FailGate" in summary and "运行环境" in summary


async def test_without_llm_case_waits_in_intake(harness_no_llm: Harness):
    await harness_no_llm.send("issues", issue_event("opened"), "d-1")
    await harness_no_llm.failgate.worker.drain()
    case = await _case(harness_no_llm)
    assert case["state"] == "INTAKE" and case["runs"] == [] and case["effects"] == []


async def test_duplicate_delivery_is_dropped(harness: Harness):
    await harness.send("issues", issue_event("opened"), "d-1")
    r = await harness.send("issues", issue_event("opened"), "d-1")
    assert r.status_code == 200 and r.json() == {"status": "duplicate"}
    assert await harness.failgate.worker.drain() == 1


async def test_bad_signature_rejected(harness: Harness):
    r = await harness.send("issues", issue_event("opened"), "d-1", secret="wrong")
    assert r.status_code == 401
    assert await harness.failgate.worker.drain() == 0


async def test_unknown_platform_404(harness: Harness):
    r = await harness.client.post("/webhooks/gitlab", content=b"{}")
    assert r.status_code == 404


async def test_close_and_reopen(harness: Harness):
    await harness.send("issues", issue_event("opened"), "d-1")
    await harness.send("issues", issue_event("closed", sender="maint"), "d-2")
    await harness.send("issues", issue_event("reopened", sender="maint"), "d-3")
    await harness.failgate.worker.drain()
    case = await _case(harness)
    assert [t["to"] for t in case["transitions"]] == [
        "INTAKE", "TRIAGING", "DEDUPING", "TRIAGE_ONLY", "CLOSED", "NEW",
    ]


async def test_bot_events_are_ignored(harness: Harness):
    await harness.send("issues", issue_event("opened", author="renovate[bot]", bot=True), "d-1")
    await harness.failgate.worker.drain()
    assert (await harness.client.get("/api/cases")).json() == []


async def test_ignore_command_needs_write_permission(harness: Harness):
    await harness.send("issues", issue_event("opened"), "d-1")
    await harness.send("issue_comment", comment_event("/failgate ignore", login="eve"), "d-2")
    await harness.failgate.worker.drain()
    assert (await _case(harness))["state"] == "TRIAGE_ONLY"

    await harness.send(
        "issue_comment",
        comment_event("/failgate ignore", login="maint", association="MEMBER"),
        "d-3",
    )
    await harness.failgate.worker.drain()
    assert (await _case(harness))["state"] == "IGNORED"


async def test_paused_repo_does_nothing(harness: Harness):
    await harness.send("issues", issue_event("opened", 1), "d-1")
    await harness.failgate.worker.drain()
    async with harness.failgate.db.session() as s, s.begin():
        await s.execute(update(Repo).where(Repo.full_name == REPO).values(mode="paused"))
    await harness.send("issues", issue_event("opened", 2), "d-2")
    await harness.failgate.worker.drain()
    assert len((await harness.client.get("/api/cases")).json()) == 1


async def test_effect_is_idempotent(harness: Harness):
    await harness.send("issues", issue_event("opened"), "d-1")
    await harness.failgate.worker.drain()
    gate = PolicyGate()
    async with harness.failgate.db.session() as s, s.begin():
        case = await s.scalar(select(Case))
        repo = await s.scalar(select(Repo))
        a = await gate.propose(s, repo=repo, case=case, action="x", payload={"add": ["bug"]})
        b = await gate.propose(s, repo=repo, case=case, action="x", payload={"add": ["bug"]})
        assert a is b
    async with harness.failgate.db.session() as s:
        # 汇总评论 + 分诊标签 + 本测试新增的一条
        assert await s.scalar(select(func.count()).select_from(Effect)) == 3
