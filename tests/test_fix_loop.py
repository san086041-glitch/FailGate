"""出题 → 答题 → 阅卷闭环（ADR 0029）：/failgate fix → Fixer 开 PR → 核验 → 驳回 → 重修。

修复和核验都用假的 runner（按顺序返回事先准备好的结果、记下参数）；Fixer App 用假的推送器
或 httpx.MockTransport。真实的修复 Agent 和沙箱在 test_fix.py / test_fix_eval.py 里测。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from conftest import REPO, Harness, _harness, comment_event, make_settings, user
from sqlalchemy import select
from test_repro_fixtures import l2_report
from test_repro_pipeline import FakeRunner
from test_verify_pipeline import FakeVerifyRunner, verification

from failgate.db import Case, Repo
from failgate.fix.feedback import feedback_from
from failgate.orchestrator.states import CaseState
from failgate.orchestrator.transitions import GuardContext, resolve
from failgate.platforms.base import User
from failgate.platforms.github_app import GitHubApp
from failgate.platforms.github_fixer import FixerClient, fix_branch
from failgate.skills.fix import FixOutput, PushRequest, fix_comment
from failgate.verify.engine import ClaimVerdict, Layer1, Layer3

BOT = "failgate-fixer[bot]"
PR = 12
S = CaseState


def push(round_: int = 0) -> PushRequest:
    return PushRequest(repo=REPO, base_sha="b" * 40, base_branch="main", branch=fix_branch(1),
                       files={"src/mylib/core.py": f"x = {round_}\n"}, message="m",
                       title="修复 #1", body="Fixes #1")


class FakeFixRunner:
    def __init__(self, *, fixes: list[FixOutput] | None = None,
                 refixes: list[FixOutput] | None = None, fail: bool = False) -> None:
        self.fixes = list(fixes or [])
        self.refixes = list(refixes or [])
        self.fail = fail
        self.calls: list[tuple[Any, ...]] = []

    async def fix(self, repo: str, issue: int, title: str, body: str,
                  budget_usd: float) -> tuple[FixOutput, None]:
        self.calls.append(("fix", repo, issue, budget_usd))
        if self.fail:
            raise RuntimeError("boom")
        return self.fixes.pop(0), None

    async def refix(self, repo: str, pr: int, verification: dict[str, Any], round_: int,
                    budget_usd: float) -> tuple[FixOutput, None]:
        self.calls.append(("refix", repo, pr, round_, verification["verdict"]))
        return self.refixes.pop(0), None


def refuted_regression(head: str = "h" * 40):
    v = verification(ClaimVerdict.REFUTED, head=head, reasons=["layer3:new_failures"])
    c = v.claims[0]
    c.layer2.signals = []  # type: ignore[union-attr]
    c.layer3 = Layer3(status="fail", reason="new_failures", files=["tests/test_core.py"],
                      new_failures=["tests/test_core.py::test_other"])
    return v


def bot_pull(action: str, author: str = BOT) -> dict[str, Any]:
    bot = author.endswith("[bot]")
    return {
        "action": action, "number": PR,
        "pull_request": {"title": "修复 #1：parse 崩溃", "body": "Fixes #1",
                         "user": user(author, bot=bot), "author_association": "NONE"},
        "repository": {"full_name": REPO}, "installation": {"id": 42},
        "sender": user(author, bot=bot),
    }


async def setup_reproduced(h: Harness) -> None:
    async with h.failgate.db.session() as s, s.begin():
        s.add(Repo(platform="github", full_name=REPO, mode="shadow",
                   repro_package="mylib", repro_source="acme/mylib"))
    from conftest import issue_event

    await h.send("issues", issue_event("opened"), "d-1")
    await h.failgate.worker.drain()


async def case_of(h: Harness, kind: str) -> Case:
    async with h.failgate.db.session() as s:
        return (await s.scalars(select(Case).where(Case.kind == kind))).one()


async def effects(h: Harness, case_id: int) -> list[dict[str, Any]]:
    data = (await h.client.get(f"/api/cases/{case_id}")).json()
    return list(data["effects"])


def harness(tmp_path, **kw: Any):
    return _harness(make_settings(tmp_path, fixer_bot_login=BOT), **kw)


# ---------------------------------------------------------------- 状态机


def test_fix_transitions():
    writer = GuardContext(actor=User(login="m", association="MEMBER"))
    reader = GuardContext(actor=User(login="x", association="NONE"))
    for src in (S.REPRODUCED, S.PR_OPENED):
        assert resolve(src, "cmd.fix", reader) is None
        t = resolve(src, "cmd.fix", writer)
        assert t is not None and t.to == S.FIXING
    ok = GuardContext(facts={"fix_ok": True})
    no = GuardContext(facts={"fix_ok": False})
    assert resolve(S.FIXING, "skill.done", ok).to == S.PR_OPENED  # type: ignore[union-attr]
    # 没修好回到 REPRODUCED（可以再试），不是不可恢复的 FAILED
    assert resolve(S.FIXING, "skill.done", no).to == S.REPRODUCED  # type: ignore[union-attr]


def test_refix_only_for_fixer_prs_with_rounds_left():
    base = {"verdict": "REFUTED", "fixer_pr": True, "refix_left": True, "budget_ok": True}
    assert resolve(S.VERIFYING, "skill.done",
                   GuardContext(facts=base)).to == S.REFIXING  # type: ignore[union-attr]
    for k, v in (("fixer_pr", False), ("refix_left", False), ("budget_ok", False)):
        t = resolve(S.VERIFYING, "skill.done", GuardContext(facts={**base, k: v}))
        assert t is not None and t.to == S.REFUTED
    verified = resolve(S.VERIFYING, "skill.done", GuardContext(facts={**base,
                                                                      "verdict": "VERIFIED"}))
    assert verified is not None and verified.to == S.VERIFIED
    assert resolve(S.REFIXING, "skill.done",
                   GuardContext(facts={"fix_ok": True})).to == S.REFIXED  # type: ignore[union-attr]
    assert resolve(S.REFIXING, "skill.done",
                   GuardContext(facts={"fix_ok": False})).to == S.REFUTED  # type: ignore[union-attr]
    assert resolve(S.REFIXED, "pull.synchronize",
                   GuardContext()).to == S.VERIFYING  # type: ignore[union-attr]


# ---------------------------------------------------------------- 反馈


def test_feedback_lists_new_failures_as_must_pass():
    fb = feedback_from(refuted_regression(), 1)
    assert fb is not None and fb.actionable
    assert fb.must_pass == ["tests/test_core.py::test_other"]
    assert "新出现失败" in fb.text and "tests/test_core.py::test_other" in fb.text


def test_feedback_for_tampering_is_not_actionable_and_none_when_not_refuted():
    tampered = verification(ClaimVerdict.REFUTED, reasons=["tamper:exam_modified"])
    fb = feedback_from(tampered, 1)
    assert fb is not None and not fb.actionable and fb.must_pass == []
    assert feedback_from(verification(ClaimVerdict.VERIFIED), 1) is None
    exam_failed = verification(ClaimVerdict.REFUTED, reasons=["layer1:head_failed"])
    exam_failed.claims[0].layer1 = Layer1(status="fail", reason="head_failed")
    fb2 = feedback_from(exam_failed, 1)
    assert fb2 is not None and fb2.actionable and "没有通过" in fb2.text


def test_fix_comment_texts():
    ok = FixOutput(issue=1, push=push(), files=["src/mylib/core.py"])
    assert "failgate/fix-1" in fix_comment(ok, "zh") and "核验" in fix_comment(ok, "zh")
    assert "Fixer App" in fix_comment(ok, "en")
    bad = FixOutput(issue=1, round=2, attempted=True, reason="no_change")
    assert "第 2 轮重修" in fix_comment(bad, "zh") and "停止重修" in fix_comment(bad, "zh")


# ---------------------------------------------------------------- 流水线：出题 → 答题


async def test_fix_command_runs_the_agent_and_proposes_a_push(tmp_path):
    runner = FakeFixRunner(fixes=[FixOutput(issue=1, attempted=True, passed=True, push=push(),
                                            files=["src/mylib/core.py"])])
    async for h in harness(tmp_path, repro_runner=FakeRunner(l2_report()),  # type: ignore[arg-type]
                           fix_runner=runner):
        await setup_reproduced(h)
        assert (await case_of(h, "issue")).state == S.REPRODUCED
        # 没有写权限的人发命令：忽略
        await h.send("issue_comment", comment_event("/failgate fix"), "c-1")
        await h.failgate.worker.drain()
        assert runner.calls == []
        await h.send("issue_comment", comment_event("/failgate fix", login="maint",
                                                    association="MEMBER"), "c-2")
        await h.failgate.worker.drain()
        case = await case_of(h, "issue")
        assert case.state == S.PR_OPENED
        assert runner.calls[0][:3] == ("fix", REPO, 1) and runner.calls[0][3] <= 0.15
        effs = await effects(h, case.id)
        pushes = [e for e in effs if e["action"] == "push_fix"]
        assert len(pushes) == 1 and pushes[0]["status"] == "shadowed"  # 影子模式只记录
        assert pushes[0]["payload"]["branch"] == "failgate/fix-1"
        assert any(e["action"] == "create_comment" and "failgate/fix-1" in e["payload"]["body"]
                   for e in effs)


async def test_failed_fix_goes_back_to_reproduced_and_says_why(tmp_path):
    runner = FakeFixRunner(fixes=[FixOutput(issue=1, attempted=True, reason="not_passed")])
    async for h in harness(tmp_path, repro_runner=FakeRunner(l2_report()),  # type: ignore[arg-type]
                           fix_runner=runner):
        await setup_reproduced(h)
        await h.send("issue_comment", comment_event("/failgate fix", login="maint",
                                                    association="MEMBER"), "c-1")
        await h.failgate.worker.drain()
        case = await case_of(h, "issue")
        assert case.state == S.REPRODUCED
        effs = await effects(h, case.id)
        assert not any(e["action"] == "push_fix" for e in effs)
        # issue 是英文写的，说明也用英文
        assert any("did not pass acceptance" in e["payload"].get("body", "") for e in effs)


async def test_runner_error_does_not_leave_the_case_stuck(tmp_path):
    runner = FakeFixRunner(fail=True)
    async for h in harness(tmp_path, repro_runner=FakeRunner(l2_report()),  # type: ignore[arg-type]
                           fix_runner=runner):
        await setup_reproduced(h)
        await h.send("issue_comment", comment_event("/failgate fix", login="maint",
                                                    association="MEMBER"), "c-1")
        await h.failgate.worker.drain()
        assert (await case_of(h, "issue")).state == S.REPRODUCED


# ---------------------------------------------------------------- 流水线：阅卷 → 答题（回传）


async def test_refuted_fixer_pr_is_revised_up_to_two_rounds(tmp_path):
    verify = FakeVerifyRunner(refuted_regression(), refuted_regression("2" * 40),
                              refuted_regression("3" * 40))
    fix = FakeFixRunner(refixes=[
        FixOutput(issue=1, round=1, attempted=True, passed=True, push=push(1)),
        FixOutput(issue=1, round=2, attempted=True, passed=True, push=push(2)),
    ])
    async for h in harness(tmp_path, verify_runner=verify, fix_runner=fix):
        await h.send("pull_request", bot_pull("opened"), "p-1")
        await h.failgate.worker.drain()
        pr = await case_of(h, "pull")
        # Fixer 机器人开的 PR 被放行、核验、被驳回 → 按理由重修 → 推新提交、等 synchronize
        assert pr.author_login == BOT and pr.state == S.REFIXED
        assert fix.calls == [("refix", REPO, PR, 1, "REFUTED")]
        for n, (delivery, expect) in enumerate((("p-2", S.REFIXED), ("p-3", S.REFUTED)), 2):
            await h.send("pull_request", bot_pull("synchronize"), delivery)
            await h.failgate.worker.drain()
            assert (await case_of(h, "pull")).state == expect, n
        # 第 3 次被驳回时已经重修过 2 轮：停在 REFUTED，不再重修
        assert [c[3] for c in fix.calls] == [1, 2] and len(verify.calls) == 3
        effs = await effects(h, pr.id)
        assert [e["payload"]["files"]["src/mylib/core.py"] for e in effs
                if e["action"] == "push_fix"] == ["x = 1\n", "x = 2\n"]
        # 每一轮的核验报告都发了（驳回 → 重修 → 再核验，PR 上看得到）
        assert len([e for e in effs if e["action"] == "upsert_summary"]) >= 1
        assert sum("第 1 轮重修" in e["payload"].get("body", "") for e in effs) == 1


async def test_other_peoples_refuted_prs_are_not_revised(tmp_path):
    verify = FakeVerifyRunner(refuted_regression())
    fix = FakeFixRunner()
    async for h in harness(tmp_path, verify_runner=verify, fix_runner=fix):
        await h.send("pull_request", bot_pull("opened", author="carol"), "p-1")
        await h.failgate.worker.drain()
        assert (await case_of(h, "pull")).state == S.REFUTED and fix.calls == []


async def test_untrusted_bots_and_fixer_comments_are_ignored(tmp_path):
    verify = FakeVerifyRunner(refuted_regression())
    async for h in harness(tmp_path, verify_runner=verify, fix_runner=FakeFixRunner()):
        await h.send("pull_request", bot_pull("opened", author="other[bot]"), "p-1")
        await h.failgate.worker.drain()
        async with h.failgate.db.session() as s:
            assert (await s.scalars(select(Case))).all() == []
        # Fixer 机器人的评论 / 命令不放行（只放行 PR 事件）
        event = comment_event("/failgate verify", PR, login=BOT, association="MEMBER",
                              on_pull=True)
        event["comment"]["user"] = event["sender"] = user(BOT, bot=True)
        await h.send("issue_comment", event, "c-1")
        await h.failgate.worker.drain()
        assert verify.calls == []


async def test_refix_that_cannot_change_anything_ends_refuted(tmp_path):
    verify = FakeVerifyRunner(refuted_regression())
    fix = FakeFixRunner(refixes=[FixOutput(issue=1, round=1, attempted=True, reason="no_change")])
    async for h in harness(tmp_path, verify_runner=verify, fix_runner=fix):
        await h.send("pull_request", bot_pull("opened"), "p-1")
        await h.failgate.worker.drain()
        pr = await case_of(h, "pull")
        assert pr.state == S.REFUTED
        assert any("停止重修" in e["payload"].get("body", "") for e in await effects(h, pr.id))


# ---------------------------------------------------------------- Fixer App 推送


class FakeGitHub:
    """Git Data API 的最小模拟：记下请求，按分支是否存在、PR 是否已开返回结果。"""

    def __init__(self, *, branch_head: str | None = None, open_pr: bool = False,
                 head_tree: str = "t-old") -> None:
        self.branch_head = branch_head
        self.open_pr = open_pr
        self.head_tree = head_tree
        self.requests: list[tuple[str, str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, m = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        self.requests.append((m, path, body))
        if path == "/repos/acme/widgets/installation":
            return httpx.Response(200, json={"id": 77})
        if path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "t", "expires_at": "2099-01-01T00:00:00Z"})
        if m == "GET" and "/git/ref/heads/" in path:
            if self.branch_head is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"object": {"sha": self.branch_head}})
        if m == "GET" and "/git/commits/" in path:
            sha = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"tree": {"sha": "t-base" if sha == "b" * 40
                                                      else self.head_tree}})
        if m == "POST" and path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": "t-new"})
        if m == "POST" and path.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "c-new"})
        if path.endswith("/git/refs") or "/git/refs/heads/" in path:
            return httpx.Response(200, json={})
        if m == "GET" and path.endswith("/pulls"):
            prs = [{"number": 12, "html_url": "u"}] if self.open_pr else []
            return httpx.Response(200, json=prs)
        if m == "POST" and path.endswith("/pulls"):
            return httpx.Response(201, json={"number": 12, "html_url": "u"})
        return httpx.Response(500, json={"message": f"unexpected {m} {path}"})


def fixer_for(gh: FakeGitHub) -> FixerClient:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    return FixerClient(GitHubApp("1", key, transport=httpx.MockTransport(gh)))


async def push_once(gh: FakeGitHub):
    fixer = fixer_for(gh)
    try:
        return await fixer.push_fix("acme/widgets", base_sha="b" * 40, base_branch="main",
                                    branch="failgate/fix-1", files={"a.py": "x\n"}, message="m",
                                    title="t", body="Fixes #1")
    finally:
        await fixer.aclose()


async def test_first_push_creates_branch_and_pr_on_top_of_base():
    gh = FakeGitHub()
    res = await push_once(gh)
    assert res.created_pr and res.commit_sha == "c-new" and res.pr_number == 12
    tree = next(b for m, p, b in gh.requests if p.endswith("/git/trees"))
    assert tree["base_tree"] == "t-base" and tree["tree"][0]["path"] == "a.py"
    commit = next(b for m, p, b in gh.requests if m == "POST" and p.endswith("/git/commits"))
    assert commit["parents"] == ["b" * 40]  # 第一次：父提交是 base
    assert any(p.endswith("/git/refs") and b["ref"] == "refs/heads/failgate/fix-1"
               for m, p, b in gh.requests if m == "POST")


async def test_later_round_commits_on_the_branch_head_and_reuses_the_pr():
    gh = FakeGitHub(branch_head="h1", open_pr=True)
    res = await push_once(gh)
    assert not res.created_pr and res.commit_sha == "c-new"
    commit = next(b for m, p, b in gh.requests if m == "POST" and p.endswith("/git/commits"))
    assert commit["parents"] == ["h1"]  # 后面的轮次：接在分支头上，PR 上能看到每一轮
    assert any(m == "PATCH" and b == {"sha": "c-new", "force": False} for m, p, b in gh.requests)
    assert not any(m == "POST" and p.endswith("/pulls") for m, p, b in gh.requests)


async def test_same_tree_as_branch_head_makes_no_new_commit():
    gh = FakeGitHub(branch_head="h1", open_pr=True, head_tree="t-new")
    res = await push_once(gh)
    assert res.commit_sha is None
    assert not any(m == "POST" and p.endswith("/git/commits") for m, p, b in gh.requests)


# ---------------------------------------------------------------- 执行器：按 action 选身份


class RecordingWriter:
    def __init__(self) -> None:
        self.comments: list[str] = []

    async def create_comment(self, ref: Any, body: str) -> str:
        self.comments.append(body)
        return "1"


class RecordingFixer:
    def __init__(self) -> None:
        self.pushes: list[dict[str, Any]] = []

    async def push_fix(self, repo: str, **kw: Any) -> None:
        self.pushes.append({"repo": repo, **kw})


async def test_executor_pushes_with_the_fixer_identity_and_comments_with_the_core_app():
    from types import SimpleNamespace

    from failgate.platforms.base import CaseKind, CaseRef, RepoRef
    from failgate.policy.executor import EffectExecutor

    ref = CaseRef(repo=RepoRef(platform="github", full_name=REPO), kind=CaseKind.ISSUE, number=1)
    writer, fixer = RecordingWriter(), RecordingFixer()
    push_effect = SimpleNamespace(action="push_fix", effect_key="k" * 12,
                                  payload=push().model_dump(mode="json"))
    comment = SimpleNamespace(action="create_comment", effect_key="c" * 12,
                              payload={"body": "hello", "at": "t"})
    ex = EffectExecutor(None, lambda r: writer, fixer)  # type: ignore[arg-type]
    assert (await ex._execute_inner(writer, ref, push_effect, None, 1)).status == "executed"  # type: ignore[arg-type]
    assert fixer.pushes[0]["branch"] == "failgate/fix-1" and writer.comments == []
    assert (await ex._execute_inner(writer, ref, comment, None, 1)).status == "executed"  # type: ignore[arg-type]
    assert writer.comments == ["hello"]
    # 没配 Fixer App：不重试，直接失败并说明原因
    out = await EffectExecutor(None, lambda r: writer)._execute_inner(  # type: ignore[arg-type]
        writer, ref, push_effect, None, 1)  # type: ignore[arg-type]
    assert out.status == "failed" and "Fixer App" in (out.error or "")


async def test_non_actionable_refix_does_not_use_up_a_round(tmp_path):
    # 维护者在 Fixer 的 PR 里加严了考卷（还没 reseal）→ 篡改驳回 → 重修不可操作，不占轮数；
    # 之后的两次真正重修照常进行
    tampered = verification(ClaimVerdict.REFUTED, reasons=["tamper:exam_modified"])
    verify = FakeVerifyRunner(tampered, refuted_regression("2" * 40),
                              refuted_regression("3" * 40), refuted_regression("4" * 40))
    fix = FakeFixRunner(refixes=[
        FixOutput(issue=1, round=1, reason="not_actionable"),
        FixOutput(issue=1, round=1, attempted=True, passed=True, push=push(1)),
        FixOutput(issue=1, round=2, attempted=True, passed=True, push=push(2)),
    ])
    async for h in harness(tmp_path, verify_runner=verify, fix_runner=fix):
        await h.send("pull_request", bot_pull("opened"), "p-1")
        await h.failgate.worker.drain()
        pr = await case_of(h, "pull")
        assert pr.state == S.REFUTED
        effs = await effects(h, pr.id)
        assert any("not something the fix agent can change" in e["payload"].get("body", "")
                   or "改不了" in e["payload"].get("body", "") for e in effs)
        await h.send("issue_comment", comment_event("/failgate verify", PR, login="maint",
                                                    association="MEMBER", on_pull=True), "c-1")
        await h.failgate.worker.drain()
        assert (await case_of(h, "pull")).state == S.REFIXED
        await h.send("pull_request", bot_pull("synchronize"), "p-2")
        await h.failgate.worker.drain()
        assert (await case_of(h, "pull")).state == S.REFIXED
        await h.send("pull_request", bot_pull("synchronize"), "p-3")
        await h.failgate.worker.drain()
        assert (await case_of(h, "pull")).state == S.REFUTED  # 两轮真正的重修都用完了
        # 轮次编号按真正的重修算：1、2（不可操作的那次传进去的是 1，之后仍从 1 开始）
        assert [c[3] for c in fix.calls] == [1, 1, 2]
