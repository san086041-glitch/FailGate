"""PR 核验接进流水线（ADR 0018）：PR 事件 → 状态机 → 核验 / 重新封存 → PR 上的报告。

真正的核验用假的 runner：按顺序返回事先准备好的结论，记下被调用的参数。
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import REPO, Harness, _harness, comment_event, make_settings, user
from sqlalchemy import select
from test_repro_fixtures import TEST_CODE, l2_report, run_source_issue
from test_repro_pipeline import FakeRunner

from failgate.db import Case, Evidence, SealedEvidenceError, TransitionLog, VerificationRecord
from failgate.orchestrator.states import CaseState
from failgate.orchestrator.transitions import GuardContext, resolve
from failgate.platforms.base import User
from failgate.skills.verify import ResealOutcome
from failgate.verify.engine import (
    ClaimResult,
    ClaimVerdict,
    ExamRun,
    Layer1,
    Layer2,
    Layer3,
    Verification,
)
from failgate.verify.receipt import (
    EvidenceReceipt,
    SealedTest,
    check_receipt,
    code_sha256,
    receipt_digest,
    reseal_receipt,
)
from failgate.verify.tamper import Signal

PR = 12
NEW_TEST = TEST_CODE.replace("parse({})", "parse({'name': 'x'})")


def pull_event(action: str, *, title: str = "Fix parse", body: str = "Fixes #1",
               author: str = "carol") -> dict[str, Any]:
    return {
        "action": action, "number": PR,
        "pull_request": {"title": title, "body": body, "user": user(author),
                         "author_association": "CONTRIBUTOR"},
        "repository": {"full_name": REPO}, "installation": {"id": 42}, "sender": user(author),
    }


def verification(verdict: ClaimVerdict | None, *, head: str = "h" * 40,
                 reasons: list[str] | None = None) -> Verification:
    claims = []
    if verdict is not None:
        runs = [ExamRun(exit_code=1, outcome="failed_same")] * 2
        ok = [ExamRun(exit_code=0, outcome="passed")] * 2
        claims = [ClaimResult(
            issue=1, verdict=verdict, reasons=reasons or [], evidence_id="e" * 32,
            exam_receipt_sha256="r" * 64, test_path="tests/test_failgate_issue_1.py",
            test_sha256=code_sha256(TEST_CODE),
            layer1=Layer1(status="pass", reason="pass", base=runs, head=ok),
            layer2=Layer2(signals=[Signal(level="high", kind="exam_modified",
                                          path="tests/test_failgate_issue_1.py")]
                          if verdict == ClaimVerdict.REFUTED else []),
            layer3=Layer3(status="none", reason="none"),
        )]
    return Verification(repo=REPO, pr=PR, base_sha="b" * 40, head_sha=head, head_repo=REPO,
                        claims=claims, verdict=verdict, created_at="2026-09-28T12:00:00Z")


class FakeVerifyRunner:
    def __init__(self, *results: Verification, db: Any = None) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, str, int]] = []
        self.db = db

    async def verify(self, repo: str, number: int) -> Verification:
        self.calls.append(("verify", repo, number))
        return self.results.pop(0)

    async def reseal(self, repo: str, number: int, actor: str) -> ResealOutcome:
        """和线上一样：在当前考卷的收据上生成新收据（这里 PR 上的新版本是 NEW_TEST）。"""
        self.calls.append(("reseal", repo, number))
        async with self.db.session() as s:
            old = (await s.scalars(select(Evidence))).one()
        receipt = reseal_receipt(old.receipt, NEW_TEST, sealed_by=actor, source_sha="h" * 40)
        return ResealOutcome(sealed=[SealedTest(receipt=receipt, code=NEW_TEST)])


async def pr_case(h: Harness) -> Case:
    async with h.failgate.db.session() as s:
        return (await s.scalars(select(Case).where(Case.kind == "pull"))).one()


def bodies(case: dict[str, Any]) -> list[str]:
    return [e["payload"]["body"] for e in case["effects"] if e["action"] == "upsert_summary"]


async def detail(h: Harness, case_id: int) -> dict[str, Any]:
    return (await h.client.get(f"/api/cases/{case_id}")).json()


async def open_pr(h: Harness, **kw: Any) -> Case:
    await h.send("pull_request", pull_event("opened", **kw), "p-1")
    await h.failgate.worker.drain()
    return await pr_case(h)


# ---------------------------------------------------------------- 状态机


def test_reseal_and_verify_commands_need_write_permission():
    reader = GuardContext(actor=User(login="x", association="NONE"))
    writer = GuardContext(actor=User(login="m", association="MEMBER"))
    for cmd in ("cmd.reseal", "cmd.verify"):
        assert resolve(CaseState.REFUTED, cmd, reader) is None
        assert resolve(CaseState.REFUTED, cmd, writer) is not None
    t = resolve(CaseState.REFUTED, "cmd.reseal", writer)
    assert t is not None and t.to == CaseState.RESEALING
    # 核验中出错卡住时，维护者可以再触发一次
    assert resolve(CaseState.VERIFYING, "cmd.verify", writer) is not None
    assert resolve(CaseState.VERIFYING, "cmd.reseal", writer) is None


# ---------------------------------------------------------------- 流水线


async def test_pr_is_verified_and_report_is_posted_in_the_pr_language(tmp_path):
    runner = FakeVerifyRunner(verification(ClaimVerdict.VERIFIED))
    async for h in _harness(make_settings(tmp_path), verify_runner=runner):
        case = await open_pr(h)
        assert case.state == CaseState.VERIFIED and runner.calls == [("verify", REPO, PR)]
        body = bodies(await detail(h, case.id))[-1]
        assert "🛡️ **FailGate verification: PR #12**" in body and "✅ Accepted" in body
        async with h.failgate.db.session() as s:
            rec = (await s.scalars(select(VerificationRecord))).one()
        assert rec.verdict == "VERIFIED" and rec.head_sha == "h" * 40 and rec.case_id == case.id
        assert check_receipt(rec.receipt) == [] and rec.receipt["schema"] == "failgate.verify/v1"
        # 核验不调用 LLM
        assert h.llm.requests == []


async def test_chinese_pr_gets_a_chinese_report(tmp_path):
    runner = FakeVerifyRunner(verification(ClaimVerdict.VERIFIED))
    async for h in _harness(make_settings(tmp_path), verify_runner=runner):
        case = await open_pr(h, title="修复配置解析", body="fixes #1")
        assert "FailGate 核验：PR #12" in bodies(await detail(h, case.id))[-1]


async def test_pr_without_claim_ends_in_no_claim(tmp_path):
    runner = FakeVerifyRunner(verification(None))
    async for h in _harness(make_settings(tmp_path), verify_runner=runner):
        case = await open_pr(h, body="Refactor only")
        assert case.state == CaseState.NO_CLAIM
        assert "does not claim to fix any issue" in bodies(await detail(h, case.id))[-1]


async def test_new_commits_and_maintainer_command_trigger_reverification(tmp_path):
    runner = FakeVerifyRunner(
        verification(ClaimVerdict.REFUTED, reasons=["tamper:exam_modified"]),
        verification(ClaimVerdict.VERIFIED, head="2" * 40),
        verification(ClaimVerdict.VERIFIED, head="2" * 40),
    )
    async for h in _harness(make_settings(tmp_path), verify_runner=runner):
        case = await open_pr(h)
        assert case.state == CaseState.REFUTED
        assert "`/failgate reseal`" in bodies(await detail(h, case.id))[-1]
        await h.send("pull_request", pull_event("synchronize"), "p-2")
        await h.failgate.worker.drain()
        assert (await pr_case(h)).state == CaseState.VERIFIED
        # 没有写权限的人发命令：忽略
        await h.send("issue_comment", comment_event("/failgate verify", PR, on_pull=True), "c-1")
        await h.failgate.worker.drain()
        assert len(runner.calls) == 2
        await h.send("issue_comment", comment_event("/failgate verify", PR, login="maint",
                                                    association="MEMBER", on_pull=True), "c-2")
        await h.failgate.worker.drain()
        assert len(runner.calls) == 3
        async with h.failgate.db.session() as s:
            recs = (await s.scalars(select(VerificationRecord))).all()
        assert [r.verdict for r in recs] == ["REFUTED", "VERIFIED", "VERIFIED"]


async def test_maintainer_reseal_supersedes_the_exam_and_reverifies(tmp_path):
    from failgate.skills.verify import ResealSkill, VerifySkill

    # 先让 issue #1 复现出 L2 考卷，再对它开 PR
    async for h in run_source_issue(FakeRunner(l2_report()), tmp_path):  # type: ignore[arg-type]
        runner = FakeVerifyRunner(
            verification(ClaimVerdict.REFUTED, reasons=["tamper:exam_modified"]),
            verification(ClaimVerdict.VERIFIED), db=h.failgate.db,
        )
        assert h.failgate.pipeline is not None
        h.failgate.pipeline.skills[CaseState.VERIFYING] = (VerifySkill(runner), "-")
        h.failgate.pipeline.skills[CaseState.RESEALING] = (ResealSkill(runner), "-")
        async with h.failgate.db.session() as s:
            old = (await s.scalars(select(Evidence))).one()
        case = await open_pr(h)
        assert case.state == CaseState.REFUTED

        await h.send("issue_comment", comment_event("/failgate reseal", PR, login="maint",
                                                    association="OWNER", on_pull=True), "c-1")
        await h.failgate.worker.drain()
        assert (await pr_case(h)).state == CaseState.VERIFIED
        assert [c[0] for c in runner.calls] == ["verify", "reseal", "verify"]
        async with h.failgate.db.session() as s:
            rows = {e.id: e for e in (await s.scalars(select(Evidence))).all()}
            issue_case = (await s.scalars(select(Case).where(Case.kind == "issue"))).one()
            log = (await s.scalars(select(TransitionLog).where(
                TransitionLog.to_state == CaseState.RESEALING))).one()
        new = next(e for e in rows.values() if e.id != old.id)
        # 旧考卷被取代、内容没变；新考卷挂在 issue 的 Case 下，记下是谁重新封存的
        assert rows[old.id].superseded_by == new.id and rows[old.id].test_code == TEST_CODE
        assert new.case_id == issue_case.id and new.test_code == NEW_TEST
        assert new.receipt["sealed_by"] == "maint" and new.receipt["supersedes"] == old.id
        assert new.verdict == "RESEALED" and check_receipt(new.receipt, NEW_TEST) == []
        assert log.actor == "maint"


async def test_verification_records_are_append_only(tmp_path):
    runner = FakeVerifyRunner(verification(ClaimVerdict.VERIFIED))
    async for h in _harness(make_settings(tmp_path), verify_runner=runner):
        await open_pr(h)
        with pytest.raises(SealedEvidenceError, match="不能修改"):
            async with h.failgate.db.session() as s, s.begin():
                rec = (await s.scalars(select(VerificationRecord))).one()
                rec.verdict = "REFUTED"


# ---------------------------------------------------------------- 收据兼容


def test_old_receipts_keep_their_hash_after_adding_optional_fields():
    """重新封存给收据加了两个可选字段；它们为空时不写出，以前的收据重算哈希结果不变。"""
    from test_evidence import receipt as make_receipt

    old = make_receipt().signed_dict()
    assert "supersedes" not in old and "sealed_by" not in old
    assert EvidenceReceipt.from_dict(old).digest() == old["receipt_sha256"]
    new = reseal_receipt(old, NEW_TEST, sealed_by="maint", source_sha="h" * 40).signed_dict()
    assert new["supersedes"] == old["evidence_id"] and new["runs"] == []
    assert new["signature"] is None and new["verdict"] == "RESEALED"
    assert receipt_digest(new) == new["receipt_sha256"]
