"""证据收据与考卷封存（ADR 0016）。

- 收据的哈希规则：换行归一、键顺序无关、改任何字段或考卷代码都能查出来；
- 复现报告 → 收据（L2 能当考卷，L1 不能；没复现不生成）；
- 流水线把收据写进评论、把收据 + 完整代码封存进 evidence 表；
- 封存之后改不了、删不了（除了 superseded_by）；
- `failgate evidence list / show`。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest
from conftest import REPO
from sqlalchemy import select, update
from test_repro_fixtures import TEST_CODE, l2_report, run_source_issue
from test_repro_pipeline import (
    FakeRunner,
    case_detail,
    not_reproduced,
    reproduced,
    run_issue,
    summary,
)
from typer.testing import CliRunner

from failgate.cli import app
from failgate.db import Database, Evidence, SealedEvidenceError
from failgate.repro.judge import RunRecord, Verdict, VerdictKind
from failgate.repro.l2 import pytest_argv
from failgate.skills.repro import seal
from failgate.verify.receipt import (
    EvidenceReceipt,
    build_receipt,
    canonical_json,
    check_receipt,
    code_sha256,
    receipt_digest,
)
from failgate.verify.store import audit, find_evidence

VERDICT = Verdict(
    kind=VerdictKind.REPRODUCED, reason="4 次", match=0.92, match_method="signature", runs=4,
    fail_rate=1.0, records=[RunRecord(exit_code=1, same_failure=True)] * 4,
)


def receipt(**kw) -> EvidenceReceipt:
    args = dict(
        repo="acme/widgets", issue=7, level="L2", mode="source", test_path="tests/t.py",
        code=TEST_CODE, package="mylib", command=pytest_argv("tests/t.py"), verdict=VERDICT,
        python="3.12", pytest="pytest==8.0.0", source_repo="acme/mylib", source_sha="a" * 40,
        now=datetime(2026, 9, 28, 10, 0, 0, 123456, tzinfo=UTC), evidence_id="e" * 32,
    )
    return build_receipt(**{**args, **kw})


# ---------------------------------------------------------------- 哈希规则


def test_code_hash_ignores_line_endings_but_not_content():
    assert code_sha256("a\r\nb\r\n") == code_sha256("a\nb\n") == code_sha256("a\rb\r")
    assert code_sha256("a\nb\n") != code_sha256("a\nb")


def test_receipt_digest_is_canonical_and_self_excluding():
    data = receipt().signed_dict()
    assert data["schema"] == "failgate.receipt/v1"
    assert data["created_at"] == "2026-09-28T10:00:00Z"
    assert data["acceptance"] is True and data["test_sha256"] == code_sha256(TEST_CODE)
    assert [r["exit_code"] for r in data["runs"]] == [1, 1, 1, 1]
    # 键顺序不影响；receipt_sha256 自己不参与哈希
    shuffled = dict(reversed(list(data.items())))
    assert receipt_digest(shuffled) == data["receipt_sha256"]
    assert "receipt_sha256" not in canonical_json(data)
    # 非 ASCII 原样保留（中文消息），任何语言都能独立重算
    assert '"reason"' not in canonical_json(data)  # 判定理由不进收据，只进评论
    assert check_receipt(data, TEST_CODE) == []
    assert EvidenceReceipt.from_dict(data).digest() == data["receipt_sha256"]


@pytest.mark.parametrize(
    ("field", "value"),
    [("verdict", "FLAKY"), ("test_sha256", "0" * 64), ("source_sha", "b" * 40), ("issue", 8)],
)
def test_changing_any_field_breaks_the_receipt(field, value):
    data = receipt().signed_dict()
    data[field] = value
    assert any("收据被改过" in p for p in check_receipt(data))


def test_changing_the_test_code_is_detected():
    data = receipt().signed_dict()
    assert check_receipt(data, TEST_CODE.replace("\n", "\r\n")) == []
    problems = check_receipt(data, TEST_CODE + "    assert True\n")
    assert problems == ["测试代码和 test_sha256 对不上：考卷被改过"]


# ---------------------------------------------------------------- 复现报告 → 收据


def test_seal_l2_report_is_an_acceptance_test():
    sealed = seal(l2_report())
    assert sealed is not None and sealed.code == TEST_CODE
    r = sealed.receipt
    assert (r.level, r.mode, r.acceptance) == ("L2", "source", True)
    assert r.test_path == "tests/test_failgate_issue_1.py"
    assert r.command == pytest_argv("tests/test_failgate_issue_1.py")
    assert (r.source_repo, r.python, r.pytest) == ("acme/mylib", "3.12", "pytest==8.0.0")
    assert r.repo == REPO and r.issue == 1 and r.verdict == "REPRODUCED"


def test_seal_l1_report_is_not_an_acceptance_test():
    sealed = seal(reproduced())
    assert sealed is not None
    r = sealed.receipt
    assert (r.level, r.mode, r.acceptance) == ("L1", "package", False)
    assert r.test_path == "repro.py" and r.command == ["python", "repro.py"]
    assert r.version == "2.4.1" and r.source_sha is None and r.pytest is None


def test_nothing_is_sealed_without_a_reproduction():
    assert seal(not_reproduced()) is None
    assert seal(l2_report(level="NONE", kind=VerdictKind.NOT_REPRODUCED,
                          status="not_reproduced", fail_rate=None)) is None


# ---------------------------------------------------------------- 流水线：评论 + 封存


async def _only_evidence(db: Database) -> Evidence:
    async with db.session() as s:
        return (await s.scalars(select(Evidence))).one()


async def test_pipeline_seals_l2_and_shows_command_and_receipt(tmp_path):
    async for h in run_source_issue(FakeRunner(l2_report()), tmp_path):  # type: ignore[arg-type]
        case = await case_detail(h)
        out = next(r for r in case["runs"] if r["skill"] == "repro")["output"]
        ev = await _only_evidence(h.failgate.db)
        assert out["evidence_id"] == ev.id and out["receipt"] == ev.receipt
        assert ev.acceptance and ev.test_code == TEST_CODE and ev.level == "L2"
        assert ev.receipt_sha256 == ev.receipt["receipt_sha256"]
        body = summary(case)
        assert "验收命令（在仓库根目录运行" in body
        assert "`python -m pytest tests/test_failgate_issue_1.py`" in body
        assert f"<details><summary>证据收据 `{ev.receipt_sha256[:12]}`</summary>" in body
        assert "这份测试已封存" in body
        # 评论里的收据 JSON 和库里的一致，拿出来能独立核对
        block = body.split("```json\n", 1)[1].split("\n```", 1)[0]
        assert json.loads(block) == ev.receipt and check_receipt(json.loads(block), TEST_CODE) == []
        async with h.failgate.db.session() as s:
            ref = await find_evidence(s, ev.id[:8])
        assert ref is not None and audit(ref) == []


async def test_pipeline_l1_receipt_says_it_is_not_an_acceptance_test(tmp_path):
    async for h in run_issue(FakeRunner(reproduced()), tmp_path):
        ev = await _only_evidence(h.failgate.db)
        assert ev.level == "L1" and not ev.acceptance
        body = summary(await case_detail(h))
        assert "L1 是独立脚本，只证明 bug 存在，不作为验收测试。" in body
        assert "验收命令" not in body


async def test_no_evidence_row_when_not_reproduced(tmp_path):
    async for h in run_issue(FakeRunner(not_reproduced()), tmp_path):
        async with h.failgate.db.session() as s:
            assert (await s.scalars(select(Evidence))).all() == []
        assert "证据收据" not in summary(await case_detail(h))


# ---------------------------------------------------------------- 封存：只加不改


async def test_sealed_evidence_cannot_be_modified_or_deleted(tmp_path):
    async for h in run_source_issue(FakeRunner(l2_report()), tmp_path):  # type: ignore[arg-type]
        db = h.failgate.db
        ev = await _only_evidence(db)
        with pytest.raises(SealedEvidenceError, match="不能修改"):
            async with db.session() as s, s.begin():
                row = await s.get(Evidence, ev.id)
                assert row is not None
                row.test_code = "def test_ok():\n    pass\n"
        with pytest.raises(SealedEvidenceError, match="不能删除"):
            async with db.session() as s, s.begin():
                await s.delete(await s.get(Evidence, ev.id))
        # 唯一允许改的：重新封存时指向新的证据
        async with db.session() as s, s.begin():
            row = await s.get(Evidence, ev.id)
            assert row is not None
            row.superseded_by = "f" * 32
        assert (await _only_evidence(db)).test_code == TEST_CODE


async def test_audit_catches_a_row_edited_behind_the_orm(tmp_path):
    async for h in run_source_issue(FakeRunner(l2_report()), tmp_path):  # type: ignore[arg-type]
        db = h.failgate.db
        ev = await _only_evidence(db)
        async with db.engine.begin() as conn:  # 绕过 ORM 直接改库：守卫挡不住，核对能查出来
            table = Evidence.__table__
            await conn.execute(
                update(table).where(table.c.id == ev.id).values(test_code="def test_x(): pass\n")
            )
        async with db.session() as s:
            ref = await find_evidence(s, ev.id)
        assert ref is not None and audit(ref) == ["测试代码和 test_sha256 对不上：考卷被改过"]


# ---------------------------------------------------------------- CLI


async def _seeded_db(tmp_path) -> tuple[Evidence, str]:
    async for h in run_source_issue(FakeRunner(l2_report()), tmp_path):  # type: ignore[arg-type]
        ev = await _only_evidence(h.failgate.db)
        url = h.failgate.db.engine.url.render_as_string(hide_password=False)
    return ev, url


def test_evidence_cli_list_and_show(tmp_path):
    # 命令自己调用 asyncio.run，所以测试本身是同步的
    ev, url = asyncio.run(_seeded_db(tmp_path))
    cli = CliRunner()
    res = cli.invoke(app, ["evidence", "list", REPO, "--db", url])
    assert res.exit_code == 0 and ev.id[:12] in res.output and "L2 考卷" in res.output
    out = tmp_path / "out"
    res = cli.invoke(app, ["evidence", "show", ev.id[:8], "--db", url, "--out", str(out)])
    assert res.exit_code == 0, res.output
    assert "✓ 哈希一致" in res.output  # 终端符号和首页一致（ADR 0038）
    assert json.loads((out / "receipt.json").read_text(encoding="utf-8")) == ev.receipt
    assert (out / "test_failgate_issue_1.py").read_text(encoding="utf-8") == TEST_CODE
    res = cli.invoke(app, ["evidence", "show", "abc", "--db", url])
    assert res.exit_code != 0 and "至少要给 6 位" in res.output
