"""端到端：webhook → 能力模块 → PolicyGate → EffectExecutor → （假的）GitHub。"""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from conftest import REPO, Harness, _harness, comment_event, issue_event, make_settings
from fake_github import FakeGitHub
from fake_llm import TRIAGE_OK
from harness_utils import only_case
from sqlalchemy import select, text

from warden.db import Case, Database, Effect, Repo, add_missing_columns
from warden.policy.executor import SUMMARY_MARKER


@pytest.fixture
def fake_gh() -> FakeGitHub:
    return FakeGitHub()


async def _live(tmp_path, fake: FakeGitHub, **overrides: Any) -> AsyncIterator[Harness]:
    settings = make_settings(tmp_path, default_repo_mode="live", **overrides)
    async for h in _harness(settings, github_app=fake.app()):
        yield h


@pytest.fixture
async def live(tmp_path, fake_gh: FakeGitHub) -> AsyncIterator[Harness]:
    async for h in _live(tmp_path, fake_gh):
        yield h


def _comments(fake: FakeGitHub, number: int = 1) -> list[dict[str, Any]]:
    return fake.comments.get((REPO, number), [])


async def test_live_mode_posts_labels_and_one_summary(live: Harness, fake_gh: FakeGitHub):
    # area:io 不在 GitHub 默认标签里，只有读到仓库真实标签表才能保留
    live.llm.queue("triage", {**TRIAGE_OK, "labels": ["bug", "area:io", "made-up"]})
    await live.send("issues", issue_event("opened"), "d-1")
    await live.warden.worker.drain()

    case = await only_case(live)
    assert {e["status"] for e in case["effects"]} == {"executed"}
    assert fake_gh.issue_labels[(REPO, 1)] == ["bug", "area:io"]
    comments = _comments(fake_gh)
    assert len(comments) == 1 and SUMMARY_MARKER in comments[0]["body"]
    assert "RepoWarden" in comments[0]["body"]
    assert case["summary_comment_id"] == str(comments[0]["id"])
    # 标签表确实是从 API 读的
    assert ("GET", f"/repos/{REPO}/labels") in fake_gh.requests
    # 标签说明也带进了分诊提示词（triage v2）
    triage_req = next(r for r in live.llm.requests if "Triage" in r["messages"][0]["content"])
    assert "- `area:io`：Reading and writing files" in triage_req["messages"][0]["content"]


async def test_shadow_mode_never_writes(tmp_path, fake_gh: FakeGitHub):
    settings = make_settings(tmp_path, default_repo_mode="shadow")
    async for h in _harness(settings, github_app=fake_gh.app()):
        await h.send("issues", issue_event("opened"), "d-1")
        await h.warden.worker.drain()
        case = await only_case(h)
        assert {e["status"] for e in case["effects"]} == {"shadowed"}
        assert fake_gh.writes == []


async def test_existing_summary_is_edited_not_duplicated(live: Harness, fake_gh: FakeGitHub):
    await live.send("issues", issue_event("opened"), "d-1")
    await live.warden.worker.drain()
    first = _comments(fake_gh)[0]

    # 模拟后续阶段产生了新的汇总内容：再提一条 upsert_summary
    async with live.warden.db.session() as s, s.begin():
        case = (await s.scalars(select(Case))).one()
        e = await live.warden.gate.propose(
            s, repo=await s.get(Repo, case.repo_id), case=case,
            action="upsert_summary", payload={"body": "updated report"},
        )
        assert e.status == "pending"
    await live.warden.executor.flush(case.id)

    comments = _comments(fake_gh)
    assert len(comments) == 1 and comments[0]["id"] == first["id"]
    assert comments[0]["body"].startswith("updated report")


async def test_crash_recovery_finds_comment_by_marker(live: Harness, fake_gh: FakeGitHub):
    """评论已经发出，但 summary_comment_id 没落库（崩溃）：下次按隐藏标记找回，不重复发。"""
    await live.send("issues", issue_event("opened"), "d-1")
    await live.warden.worker.drain()
    async with live.warden.db.session() as s, s.begin():
        case = (await s.scalars(select(Case))).one()
        case.summary_comment_id = None
        repo = await s.get(Repo, case.repo_id)
        await live.warden.gate.propose(
            s, repo=repo, case=case, action="upsert_summary", payload={"body": "v2"}
        )
    await live.warden.executor.flush(case.id)
    comments = _comments(fake_gh)
    assert len(comments) == 1 and comments[0]["body"].startswith("v2")


async def test_deleted_summary_is_recreated(live: Harness, fake_gh: FakeGitHub):
    await live.send("issues", issue_event("opened"), "d-1")
    await live.warden.worker.drain()
    fake_gh.comments[(REPO, 1)].clear()  # 维护者删掉了机器人的评论
    async with live.warden.db.session() as s, s.begin():
        case = (await s.scalars(select(Case))).one()
        repo = await s.get(Repo, case.repo_id)
        await live.warden.gate.propose(
            s, repo=repo, case=case, action="upsert_summary", payload={"body": "v2"}
        )
    await live.warden.executor.flush(case.id)
    comments = _comments(fake_gh)
    assert len(comments) == 1
    case_json = await only_case(live)
    assert case_json["summary_comment_id"] == str(comments[0]["id"])


async def test_transient_failure_stays_pending_then_succeeds(live: Harness, fake_gh: FakeGitHub):
    fake_gh.fail("POST", r"/issues/1/comments$", httpx.Response(502, text="bad gateway"))
    await live.send("issues", issue_event("opened"), "d-1")
    await live.warden.worker.drain()
    case = await only_case(live)
    summary = next(e for e in case["effects"] if e["action"] == "upsert_summary")
    assert summary["status"] == "pending" and summary["attempts"] == 1
    assert "502" in summary["error"]

    assert await live.warden.executor.flush_all() == {"executed": 1}
    case = await only_case(live)
    assert {e["status"] for e in case["effects"]} == {"executed"}
    assert len(_comments(fake_gh)) == 1


async def test_gives_up_after_max_attempts(live: Harness, fake_gh: FakeGitHub):
    for _ in range(3):
        fake_gh.fail("POST", r"/issues/1/comments$", httpx.Response(503))
    await live.send("issues", issue_event("opened"), "d-1")
    await live.warden.worker.drain()
    await live.warden.executor.flush_all()
    await live.warden.executor.flush_all()
    case = await only_case(live)
    summary = next(e for e in case["effects"] if e["action"] == "upsert_summary")
    assert summary["status"] == "failed" and summary["attempts"] == 3
    # 失败了就不再重试
    assert await live.warden.executor.flush_all() == {}


async def test_permission_error_fails_immediately(live: Harness, fake_gh: FakeGitHub):
    fake_gh.fail(
        "POST", r"/labels$",
        httpx.Response(403, json={"message": "Resource not accessible by integration"}),
    )
    await live.send("issues", issue_event("opened"), "d-1")
    await live.warden.worker.drain()
    case = await only_case(live)
    labels = next(e for e in case["effects"] if e["action"] == "set_labels")
    assert labels["status"] == "failed" and labels["attempts"] == 1
    # 一条失败不影响另一条
    summary = next(e for e in case["effects"] if e["action"] == "upsert_summary")
    assert summary["status"] == "executed"


async def test_secret_in_comment_is_blocked(live: Harness, fake_gh: FakeGitHub):
    leaked = "ghp_" + "a1B2" * 9
    live.llm.queue("triage", {**TRIAGE_OK, "rationale": f"用户贴出了 token {leaked}"})
    await live.send("issues", issue_event("opened"), "d-1")
    await live.warden.worker.drain()
    case = await only_case(live)
    summary = next(e for e in case["effects"] if e["action"] == "upsert_summary")
    assert summary["status"] == "blocked" and "github_token" in summary["error"]
    assert leaked not in summary["error"]
    assert _comments(fake_gh) == []


async def test_no_installation_keeps_effects_pending(live: Harness, fake_gh: FakeGitHub):
    payload = issue_event("opened")
    del payload["installation"]
    await live.send("issues", payload, "d-1")
    await live.warden.worker.drain()
    case = await only_case(live)
    assert {e["status"] for e in case["effects"]} == {"pending"}
    assert fake_gh.writes == []


def _cmd(body: str, login: str, association: str) -> dict[str, Any]:
    payload = comment_event(body, login=login, association=association)
    payload["installation"] = {"id": 42}
    return payload


async def test_command_uses_live_permission_not_association(live: Harness, fake_gh: FakeGitHub):
    # dave 是 COLLABORATOR，但只有 triage 角色：旧逻辑会放行，新逻辑必须拒绝
    fake_gh.permissions = {"dave": "triage", "carol": "write"}
    await live.send("issues", issue_event("opened"), "d-1")
    await live.warden.worker.drain()

    await live.send("issue_comment", _cmd("/warden ignore", "dave", "COLLABORATOR"), "d-2")
    await live.warden.worker.drain()
    assert (await only_case(live))["state"] == "TRIAGE_ONLY"

    # carol 的 association 是 NONE（例如 webhook 里没带），但实际有 write 权限
    await live.send("issue_comment", _cmd("/warden ignore", "carol", "NONE"), "d-3")
    await live.warden.worker.drain()
    assert (await only_case(live))["state"] == "IGNORED"


async def test_permission_lookup_failure_denies(live: Harness, fake_gh: FakeGitHub):
    await live.send("issues", issue_event("opened"), "d-1")
    await live.warden.worker.drain()
    fake_gh.fail("GET", r"/permission$", httpx.Response(500))
    await live.send("issue_comment", _cmd("/warden ignore", "carol", "OWNER"), "d-2")
    await live.warden.worker.drain()
    assert (await only_case(live))["state"] == "TRIAGE_ONLY"


async def test_plain_comments_do_not_query_permissions(live: Harness, fake_gh: FakeGitHub):
    await live.send("issues", issue_event("opened"), "d-1")
    await live.warden.worker.drain()
    await live.send("issue_comment", _cmd("thanks!", "bob", "NONE"), "d-2")
    await live.warden.worker.drain()
    assert not any(p.endswith("/permission") for _, p in fake_gh.requests)


async def test_add_missing_columns_upgrades_old_database(tmp_path):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'old.db').as_posix()}"
    db = Database(url)
    async with db.engine.begin() as conn:
        # 模拟上一个版本的表：没有 attempts / error 两列
        await conn.execute(text(
            "CREATE TABLE effects (effect_key VARCHAR(64) PRIMARY KEY, case_id INTEGER, "
            "action VARCHAR(32), payload JSON, mode VARCHAR(16), status VARCHAR(16), "
            "created_at DATETIME, executed_at DATETIME)"
        ))
        await conn.execute(text(
            "INSERT INTO effects VALUES ('k', 1, 'set_labels', '{}', 'live', 'pending', "
            "'2026-09-24 00:00:00', NULL)"
        ))
    await db.create_all()
    async with db.session() as s:
        e = await s.get(Effect, "k")
        assert e is not None and e.attempts == 0 and e.error is None
    # 再跑一次什么都不加
    async with db.engine.begin() as conn:
        assert await conn.run_sync(add_missing_columns) == []
    await db.dispose()
