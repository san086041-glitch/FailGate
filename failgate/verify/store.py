"""读取和核对封存的证据（`failgate evidence`，之后 ClaimVerify 也从这里取考卷）。"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from failgate.db import Case, Evidence, HiddenExamRecord, Repo
from failgate.index.trace import TraceSignature

from .engine import Exam
from .hidden import HiddenExam
from .receipt import check_receipt

MIN_PREFIX = 6


@dataclass
class EvidenceRef:
    evidence: Evidence
    repo: str
    issue: int


async def find_evidence(s: AsyncSession, id_or_prefix: str) -> EvidenceRef | None:
    """按完整 ID 或前缀（至少 6 位，和评论里显示的短哈希一样方便复制）查找；前缀不唯一时报错。"""
    if len(id_or_prefix) < MIN_PREFIX:
        raise ValueError(f"证据 ID 至少要给 {MIN_PREFIX} 位")
    rows = (await s.execute(
        select(Evidence, Repo.full_name, Case.number)
        .join(Case, Evidence.case_id == Case.id).join(Repo, Case.repo_id == Repo.id)
        .where(Evidence.id.startswith(id_or_prefix, autoescape=True))
        .limit(2)
    )).all()
    if len(rows) > 1:
        raise ValueError(f"前缀 {id_or_prefix} 对应多条证据，请多给几位")
    if not rows:
        return None
    ev, repo, issue = rows[0]
    return EvidenceRef(ev, repo, issue)


async def list_evidence(s: AsyncSession, repo: str | None = None) -> list[EvidenceRef]:
    q = (
        select(Evidence, Repo.full_name, Case.number)
        .join(Case, Evidence.case_id == Case.id).join(Repo, Case.repo_id == Repo.id)
        .order_by(Evidence.created_at)
    )
    if repo:
        q = q.where(Repo.full_name == repo)
    return [EvidenceRef(ev, r, n) for ev, r, n in (await s.execute(q)).all()]


async def latest_exam(s: AsyncSession, repo: str, issue: int) -> Exam | None:
    """#issue 当前有效的考卷：能当考卷（L2）、没被取代的最新一份。"""
    row = (await s.execute(
        select(Evidence, Repo.repro_import_name)
        .join(Case, Evidence.case_id == Case.id).join(Repo, Case.repo_id == Repo.id)
        .where(Repo.full_name == repo, Case.number == issue, Evidence.acceptance.is_(True),
               Evidence.superseded_by.is_(None))
        .order_by(Evidence.created_at.desc()).limit(1)
    )).first()
    if row is None:
        return None
    ev, import_name = row
    r = ev.receipt
    sig = r.get("signature")
    hidden = await latest_hidden(s, ev.id)
    return Exam(hidden=hidden,
        evidence_id=ev.id, issue=issue, test_path=ev.test_path, code=ev.test_code,
        test_sha256=ev.test_sha256, receipt_sha256=ev.receipt_sha256, package=r["package"],
        module=import_name or r["package"].replace("-", "_").lower(), python=ev.python,
        pytest=ev.pytest, version=r.get("version"),
        signature=TraceSignature.model_validate(sig) if sig else None, receipt=r,
    )


async def latest_hidden(s: AsyncSession, evidence_id: str) -> HiddenExam | None:
    """这份公开考卷最新的隐藏考卷（ADR 0021）；没有就是 None。"""
    row = (await s.execute(
        select(HiddenExamRecord).where(HiddenExamRecord.evidence_id == evidence_id)
        .order_by(HiddenExamRecord.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if row is None:
        return None
    return HiddenExam(hidden_id=row.id, evidence_id=row.evidence_id, test_path=row.test_path,
                      code=row.test_code, test_sha256=row.test_sha256, tests=list(row.tests),
                      receipt=row.receipt)


def hidden_row(h: HiddenExam) -> HiddenExamRecord:
    return HiddenExamRecord(id=h.hidden_id, evidence_id=h.evidence_id, test_path=h.test_path,
                            test_code=h.code, test_sha256=h.test_sha256, tests=h.tests,
                            receipt=h.receipt, receipt_sha256=h.receipt["receipt_sha256"])


def audit(ref: EvidenceRef) -> list[str]:
    """核对一条证据：收据自身的哈希、考卷代码的哈希，以及表里的列和收据是否一致。"""
    ev = ref.evidence
    problems = check_receipt(ev.receipt, ev.test_code)
    r = ev.receipt
    expected = {
        "evidence_id": ev.id, "test_sha256": ev.test_sha256, "test_path": ev.test_path,
        "level": ev.level, "repo": ref.repo, "issue": ref.issue,
    }
    for key, value in expected.items():
        if r.get(key) != value:
            problems.append(f"表里的 {key}={value!r} 和收据里的 {r.get(key)!r} 不一致")
    if r.get("receipt_sha256") != ev.receipt_sha256:
        problems.append("表里的 receipt_sha256 和收据里的不一致")
    return problems
