"""工作台最小版（ADR 0035）：只读 API 的新字段、收据核对、隐藏考卷不外泄、访问控制、页面。"""

from __future__ import annotations

from typing import Any

import httpx
from conftest import REPO, Harness, make_settings
from sqlalchemy import text

from failgate.app import create_app
from failgate.db import Case, Evidence, HiddenExamRecord, Repo, Run, VerificationRecord
from failgate.repro.judge import RunRecord, Verdict, VerdictKind
from failgate.verify.receipt import build_receipt, code_sha256, receipt_digest

EXAM = "def test_x():\n    assert parse('') == {}\n"
HIDDEN_CODE = "def test_hidden_variant_secret():\n    assert 1\n"
VERDICT = Verdict(kind=VerdictKind.REPRODUCED, reason="4 次", match=1.0,
                  match_method="signature", runs=4, fail_rate=1.0,
                  records=[RunRecord(exit_code=1, same_failure=True)] * 4)


async def seed(h: Harness) -> dict[str, Any]:
    receipt = build_receipt(repo=REPO, issue=1, level="L2", mode="source",
                            test_path="tests/test_failgate_issue_1.py", code=EXAM,
                            package="demo", command=["python", "-m", "pytest"],
                            verdict=VERDICT).signed_dict()
    claim = {"issue": 1, "verdict": "VERIFIED", "reasons": [],
             "evidence_id": receipt["evidence_id"],
             "layer1": {"status": "pass"}, "layer2": {"signals": []},
             "layer3": {"status": "pass", "new_failures": []},
             "strength": {"grade": "weak", "kill_rate": 0.4, "killed": 6, "survived": 9},
             "hidden": {"total": 5, "failed": 2}}
    vreceipt = {"repo": REPO, "pr": 7, "claims": [claim], "verdict": "VERIFIED"}
    vreceipt["receipt_sha256"] = receipt_digest(vreceipt)
    async with h.failgate.db.session() as s:
        repo = Repo(platform="github", full_name=REPO, mode="live")
        s.add(repo)
        await s.flush()
        issue = Case(repo_id=repo.id, kind="issue", number=1, state="REPRODUCED",
                     title="<script>alert(1)</script> crash", spent_usd=0.0123)
        pr = Case(repo_id=repo.id, kind="pull", number=7, state="VERIFIED", title="Fix #1")
        s.add_all([issue, pr])
        await s.flush()
        s.add(Run(case_id=issue.id, skill="repro", skill_version="1", model="deepseek-flash",
                  status="ok", tokens_in=100, tokens_out=10, usd=0.01))
        s.add(Evidence(id=receipt["evidence_id"], case_id=issue.id, level="L2", mode="source",
                       acceptance=True, test_path=receipt["test_path"], test_code=EXAM,
                       test_sha256=receipt["test_sha256"], verdict="REPRODUCED",
                       receipt=receipt, receipt_sha256=receipt["receipt_sha256"]))
        await s.flush()
        s.add(HiddenExamRecord(id="h" * 32, evidence_id=receipt["evidence_id"],
                               test_path="tests/.failgate_hidden.py", test_code=HIDDEN_CODE,
                               test_sha256=code_sha256(HIDDEN_CODE),
                               tests=["test_hidden_variant_secret"], receipt={},
                               receipt_sha256="x"))
        s.add(VerificationRecord(case_id=pr.id, pr_number=7, base_sha="a" * 40,
                                 head_sha="b" * 40, verdict="VERIFIED", receipt=vreceipt,
                                 receipt_sha256=vreceipt["receipt_sha256"]))
        await s.commit()
        return {"issue": issue.id, "pr": pr.id, "evidence": receipt["evidence_id"]}


async def test_console_api_fields_and_receipt_check(harness: Harness):
    ids = await seed(harness)
    c = harness.client
    stats = (await c.get("/api/stats")).json()
    assert stats["cases"] == 2 and stats["by_kind"] == {"issue": 1, "pull": 1}
    assert stats["verifications"] == {"VERIFIED": 1} and stats["evidence"] == {"L2": 1}
    assert stats["repos"] == [{"repo": REPO, "mode": "live"}]

    prs = (await c.get("/api/cases", params={"kind": "pull"})).json()
    assert [x["number"] for x in prs] == [7] and prs[0]["title"] == "Fix #1"
    assert (await c.get("/api/cases", params={"repo": "other/x"})).json() == []

    issue = (await c.get(f"/api/cases/{ids['issue']}")).json()
    assert issue["platform"] == "github" and issue["repo"] == REPO
    ev = issue["evidence"][0]
    assert ev["level"] == "L2" and ev["hidden_exams"] == 1 and ev["superseded_by"] is None
    pr = (await c.get(f"/api/cases/{ids['pr']}")).json()
    claim = pr["verifications"][0]["claims"][0]
    assert claim["verdict"] == "VERIFIED" and claim["layer1"] == "pass"
    assert claim["strength"]["grade"] == "weak" and claim["hidden"] == {"total": 5, "failed": 2}

    detail = await c.get(f"/api/evidence/{ids['evidence']}")
    body = detail.json()
    assert body["problems"] == [] and body["test_code"] == EXAM
    assert body["hidden_exams"][0]["tests"] == 1
    assert "test_hidden_variant_secret" not in detail.text  # 隐藏考卷的题目不外泄

    v = (await c.get(f"/api/verifications/{pr['verifications'][0]['id']}")).json()
    assert v["problems"] == [] and v["receipt"]["claims"][0]["issue"] == 1
    assert (await c.get("/api/evidence/nope")).status_code == 404


async def test_tampered_evidence_is_flagged(harness: Harness):
    ids = await seed(harness)
    # 绕过 ORM 的只加不改守卫，直接改库（ADR 0016 说过这挡不住，要靠核对哈希查出来）
    async with harness.failgate.db.session() as s:
        await s.execute(text("UPDATE evidence SET test_code = :c WHERE id = :i"),
                        {"c": "def test_x():\n    pass\n", "i": ids["evidence"]})
        await s.commit()
    body = (await harness.client.get(f"/api/evidence/{ids['evidence']}")).json()
    assert any("考卷被改过" in p for p in body["problems"])


async def test_console_page_is_served_and_escapes_by_construction(harness: Harness):
    r = await harness.client.get("/console")
    assert r.status_code == 200 and "FailGate 工作台" in r.text
    assert r.headers["cache-control"] == "no-store"
    # 页面只用 textContent 插入数据，不出现 innerHTML
    assert "innerHTML" not in r.text


async def _client(app: Any, host: str) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=(host, 1234))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_remote_access_needs_a_token(tmp_path):
    app = create_app(make_settings(tmp_path), run_worker=False)
    await app.state.failgate.start(run_worker=False)
    try:
        async with await _client(app, "203.0.113.5") as remote:
            assert (await remote.get("/api/cases")).status_code == 403
            assert (await remote.get("/console")).status_code == 403
            assert (await remote.get("/healthz")).status_code == 200  # 健康检查不受限
    finally:
        await app.state.failgate.stop()

    app = create_app(make_settings(tmp_path, console_token="s3cret"), run_worker=False)
    await app.state.failgate.start(run_worker=False)
    try:
        async with await _client(app, "203.0.113.5") as remote:
            assert (await remote.get("/api/cases")).status_code == 401
            ok = await remote.get("/api/cases", headers={"Authorization": "Bearer s3cret"})
            assert ok.status_code == 200
            assert (await remote.get("/console", params={"token": "wrong"})).status_code == 401
            login = await remote.get("/console", params={"token": "s3cret"})
            assert login.status_code == 303 and login.headers["location"] == "/console"
            assert "httponly" in login.headers["set-cookie"].lower()
            page = await remote.get("/console")  # cookie 已经写进客户端
            assert page.status_code == 200
            assert (await remote.get("/api/stats")).status_code == 200
        async with await _client(app, "127.0.0.1") as local:
            # 配了 token 之后本机也要带 token
            assert (await local.get("/api/cases")).status_code == 401
    finally:
        await app.state.failgate.stop()

