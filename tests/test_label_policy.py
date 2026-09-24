"""自动打标签的策略：结论 / 进度类标签始终拦截，仓库白名单可选；在 PolicyGate 层执行。"""

from collections.abc import AsyncIterator

import pytest
from conftest import REPO, Harness, _harness, issue_event, make_settings
from fake_github import FakeGitHub
from fake_llm import TRIAGE_OK
from harness_utils import only_case
from sqlalchemy import select

from warden.db import Repo
from warden.policy.labels import decision_phrase, filter_auto_labels


@pytest.mark.parametrize(
    "name",
    [
        # GitHub 默认标签里的"结论 / 邀请贡献"类
        "duplicate", "invalid", "wontfix", "good first issue", "help wanted",
        # psf/black 的自定义命名
        "R: duplicate", "R: not a bug", "R: rejected", "S: needs discussion", "S: needs repro",
        "S: awaiting response", "S: accepted", "spam / ai",
        # 其他常见写法
        "status: confirmed", "needs-repro", "stale", "Won't Fix", "wip",
    ],
)
def test_decision_labels_are_recognized(name):
    assert decision_phrase(name) is not None


@pytest.mark.parametrize(
    "name",
    [
        "bug", "documentation", "enhancement", "question",
        # 类别标签里出现了有歧义的词，但不在末尾：不能误拦
        "C: invalid code", "confirmed bug", "area: released builds",
        # 整词匹配："prefixed" 里有 "fixed"
        "prefixed", "T: style", "C: crash",
    ],
)
def test_content_labels_are_allowed(name):
    assert decision_phrase(name) is None


def test_filter_with_allowlist():
    kept, blocked = filter_auto_labels(
        ["T: bug", "C: crash", "S: needs repro", "good first issue", "T: bug"], ["T: *"]
    )
    assert kept == ["T: bug"]
    assert blocked["C: crash"] == "not in repo allowlist"
    assert blocked["S: needs repro"].startswith("decision/status")
    # 没有白名单时只拦结论 / 进度类
    assert filter_auto_labels(["C: crash", "duplicate"], None)[0] == ["C: crash"]


LABELS = ["bug", "area:io", "question", "duplicate", "S: needs discussion"]


@pytest.fixture
async def live(tmp_path) -> AsyncIterator[tuple[Harness, FakeGitHub]]:
    fake = FakeGitHub(labels=LABELS)
    async for h in _harness(make_settings(tmp_path, default_repo_mode="live"),
                            github_app=fake.app()):
        yield h, fake


async def test_gate_strips_decision_labels_and_records_why(live):
    h, fake = live
    h.llm.queue("triage", {**TRIAGE_OK, "labels": ["bug", "duplicate", "S: needs discussion"]})
    await h.send("issues", issue_event("opened"), "d-1")
    await h.warden.worker.drain()
    effect = next(e for e in (await only_case(h))["effects"] if e["action"] == "set_labels")
    assert effect["payload"]["add"] == ["bug"] and effect["status"] == "executed"
    assert set(effect["payload"]["blocked"]) == {"duplicate", "S: needs discussion"}
    assert fake.issue_labels[(REPO, 1)] == ["bug"]


async def test_all_blocked_is_never_sent(live):
    h, fake = live
    h.llm.queue("triage", {**TRIAGE_OK, "labels": ["duplicate"]})
    await h.send("issues", issue_event("opened"), "d-1")
    await h.warden.worker.drain()
    effect = next(e for e in (await only_case(h))["effects"] if e["action"] == "set_labels")
    assert effect["status"] == "blocked" and effect["payload"]["add"] == []
    assert (REPO, 1) not in fake.issue_labels
    assert not any(p.endswith("/labels") and m == "POST" for m, p in fake.requests)


async def test_repo_allowlist_limits_auto_labels(live):
    h, fake = live
    await h.send("issues", issue_event("opened", 1), "d-1")
    await h.warden.worker.drain()
    async with h.warden.db.session() as s, s.begin():
        repo = (await s.scalars(select(Repo))).one()
        repo.auto_labels = ["area:*"]
    h.llm.queue("triage", {**TRIAGE_OK, "labels": ["bug", "area:io"]})
    await h.send("issues", issue_event("opened", 2), "d-2")
    await h.warden.worker.drain()
    effect = next(e for e in (await only_case(h, 2))["effects"] if e["action"] == "set_labels")
    assert effect["payload"]["add"] == ["area:io"]
    assert effect["payload"]["blocked"] == {"bug": "not in repo allowlist"}
    assert fake.issue_labels[(REPO, 2)] == ["area:io"]
