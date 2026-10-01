"""控制台只读 API + 工作台页面（M0 起；W12 最小版工作台，ADR 0035）。

全部只读。访问控制（ADR 0035）：
- 配了 CONSOLE_TOKEN：要带 `Authorization: Bearer <token>`，或 cookie `failgate_console`
  （浏览器打开 `/console?token=…` 时写入）；
- 没配：只允许本机（loopback）访问。`failgate serve` 默认也只监听 127.0.0.1，这是第二道防线。

工作台不显示隐藏考卷的题目（ADR 0021：题目不公开），只显示题数和哈希。
"""

from __future__ import annotations

import hmac
from importlib import resources
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select

from failgate.db import (
    Case,
    Effect,
    Evidence,
    HiddenExamRecord,
    Repo,
    Run,
    TransitionLog,
    VerificationRecord,
)
from failgate.verify.receipt import check_receipt

if TYPE_CHECKING:
    from failgate.app import FailGate

COOKIE = "failgate_console"
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def _failgate(request: Request) -> FailGate:
    return request.app.state.failgate


def _token(request: Request) -> str:
    return str(getattr(_failgate(request).settings, "console_token", "") or "")


def require_console(request: Request) -> None:
    """只读接口和工作台的访问控制。"""
    token = _token(request)
    if token:
        auth = request.headers.get("authorization", "")
        given = auth[7:] if auth.lower().startswith("bearer ") else request.cookies.get(COOKIE, "")
        if not given or not hmac.compare_digest(given, token):
            raise HTTPException(status_code=401, detail="需要 CONSOLE_TOKEN")
        return
    host = request.client.host if request.client else ""
    if host not in LOOPBACK:
        raise HTTPException(status_code=403, detail="没有配置 CONSOLE_TOKEN 时只允许本机访问")


router = APIRouter(prefix="/api", dependencies=[Depends(require_console)])
console_router = APIRouter()


# ---------------------------------------------------------------- 列表与统计


@router.get("/cases")
async def list_cases(request: Request, state: str | None = None, repo: str | None = None,
                     kind: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    async with _failgate(request).db.session() as s:
        q = select(Case, Repo).join(Repo, Case.repo_id == Repo.id).order_by(Case.id.desc())
        if state:
            q = q.where(Case.state == state)
        if repo:
            q = q.where(Repo.full_name == repo)
        if kind:
            q = q.where(Case.kind == kind)
        rows = (await s.execute(q.limit(max(1, min(limit, 500))))).all()
    return [
        {
            "id": c.id,
            "repo": r.full_name,
            "platform": r.platform,
            "kind": c.kind,
            "number": c.number,
            "title": c.title,
            "state": c.state,
            "spent_usd": round(c.spent_usd, 6),
            "created_at": c.created_at.isoformat(),
            "updated_at": c.updated_at.isoformat(),
        }
        for c, r in rows
    ]


@router.get("/stats")
async def stats(request: Request) -> dict[str, Any]:
    async with _failgate(request).db.session() as s:
        states = (await s.execute(select(Case.state, func.count()).group_by(Case.state))).all()
        kinds = (await s.execute(select(Case.kind, func.count()).group_by(Case.kind))).all()
        spent = await s.scalar(select(func.coalesce(func.sum(Case.spent_usd), 0.0)))
        verdicts = (await s.execute(select(VerificationRecord.verdict, func.count())
                                    .group_by(VerificationRecord.verdict))).all()
        evidence = (await s.execute(select(Evidence.level, func.count())
                                    .where(Evidence.superseded_by.is_(None))
                                    .group_by(Evidence.level))).all()
        repos = (await s.execute(select(Repo.full_name, Repo.mode).order_by(Repo.full_name))).all()
    return {
        "cases": sum(n for _, n in states),
        "by_state": dict(states),
        "by_kind": dict(kinds),
        "spent_usd": round(float(spent or 0.0), 4),
        "verifications": {str(v): n for v, n in verdicts},
        "evidence": dict(evidence),
        "repos": [{"repo": r, "mode": m} for r, m in repos],
    }


# ---------------------------------------------------------------- 单个 Case


def _claim_summary(claim: dict[str, Any]) -> dict[str, Any]:
    """核验收据里一个声明的要点（工作台列表用；完整内容看 /api/verifications/{id}）。"""
    l1, l2, l3 = claim.get("layer1") or {}, claim.get("layer2") or {}, claim.get("layer3") or {}
    strength, hidden = claim.get("strength") or {}, claim.get("hidden") or {}
    return {
        "issue": claim.get("issue"),
        "verdict": claim.get("verdict"),
        "reasons": claim.get("reasons") or [],
        "evidence_id": claim.get("evidence_id"),
        "layer1": l1.get("status"),
        "layer2_signals": len(l2.get("signals") or []),
        "layer3": l3.get("status"),
        "new_failures": len(l3.get("new_failures") or []),
        "strength": {"grade": strength.get("grade"), "kill_rate": strength.get("kill_rate"),
                     "killed": strength.get("killed"), "survived": strength.get("survived")}
        if strength else None,
        "hidden": {"total": hidden.get("total"), "failed": hidden.get("failed")}
        if hidden else None,
    }


@router.get("/cases/{case_id}")
async def get_case(request: Request, case_id: int) -> dict[str, Any]:
    async with _failgate(request).db.session() as s:
        case = await s.get(Case, case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="case not found")
        repo = await s.get(Repo, case.repo_id)
        transitions = (
            await s.scalars(
                select(TransitionLog)
                .where(TransitionLog.case_id == case_id)
                .order_by(TransitionLog.id)
            )
        ).all()
        effects = (
            await s.scalars(
                select(Effect).where(Effect.case_id == case_id).order_by(Effect.created_at)
            )
        ).all()
        runs = (await s.scalars(select(Run).where(Run.case_id == case_id).order_by(Run.id))).all()
        evidence = (await s.scalars(select(Evidence).where(Evidence.case_id == case_id)
                                    .order_by(Evidence.created_at))).all()
        hidden_counts = dict((await s.execute(
            select(HiddenExamRecord.evidence_id, func.count())
            .where(HiddenExamRecord.evidence_id.in_([e.id for e in evidence]))
            .group_by(HiddenExamRecord.evidence_id))).all()) if evidence else {}
        verifications = (await s.scalars(
            select(VerificationRecord).where(VerificationRecord.case_id == case_id)
            .order_by(VerificationRecord.id))).all()
    return {
        "id": case.id,
        "repo": repo.full_name if repo else None,
        "platform": repo.platform if repo else None,
        "kind": case.kind,
        "number": case.number,
        "title": case.title,
        "state": case.state,
        "state_version": case.state_version,
        "spent_usd": round(case.spent_usd, 6),
        "summary_comment_id": case.summary_comment_id,
        "created_at": case.created_at.isoformat(),
        "updated_at": case.updated_at.isoformat(),
        "runs": [
            {
                "skill": r.skill,
                "version": r.skill_version,
                "model": r.model,
                "status": r.status,
                "output": r.output,
                "error": r.error,
                "tokens": {"in": r.tokens_in, "out": r.tokens_out, "cached": r.tokens_cached},
                "usd": r.usd,
            }
            for r in runs
        ],
        "transitions": [
            {"from": t.from_state, "to": t.to_state, "event": t.event, "at": t.at.isoformat()}
            for t in transitions
        ],
        "effects": [
            {
                "action": e.action,
                "payload": e.payload,
                "mode": e.mode,
                "status": e.status,
                "attempts": e.attempts,
                "error": e.error,
            }
            for e in effects
        ],
        "evidence": [
            {
                "id": e.id, "level": e.level, "mode": e.mode, "verdict": e.verdict,
                "test_path": e.test_path, "test_sha256": e.test_sha256,
                "receipt_sha256": e.receipt_sha256, "superseded_by": e.superseded_by,
                "python": e.python, "source_sha": e.source_sha,
                "hidden_exams": hidden_counts.get(e.id, 0),
                "created_at": e.created_at.isoformat(),
            }
            for e in evidence
        ],
        "verifications": [
            {
                "id": v.id, "pr": v.pr_number, "verdict": v.verdict, "base_sha": v.base_sha,
                "head_sha": v.head_sha, "receipt_sha256": v.receipt_sha256,
                "created_at": v.created_at.isoformat(),
                "claims": [_claim_summary(c) for c in (v.receipt or {}).get("claims") or []],
            }
            for v in verifications
        ],
    }


@router.get("/evidence/{evidence_id}")
async def get_evidence(request: Request, evidence_id: str) -> dict[str, Any]:
    async with _failgate(request).db.session() as s:
        e = await s.get(Evidence, evidence_id)
        if e is None:
            raise HTTPException(status_code=404, detail="evidence not found")
        hidden = (await s.scalars(select(HiddenExamRecord)
                                  .where(HiddenExamRecord.evidence_id == evidence_id)
                                  .order_by(HiddenExamRecord.created_at))).all()
    return {
        "id": e.id, "case_id": e.case_id, "level": e.level, "verdict": e.verdict,
        "test_path": e.test_path, "test_code": e.test_code, "receipt": e.receipt,
        "receipt_sha256": e.receipt_sha256, "superseded_by": e.superseded_by,
        "created_at": e.created_at.isoformat(),
        # 收据和代码重新算一遍哈希：数据库被直接改过时这里会报出来（ADR 0016）
        "problems": check_receipt(e.receipt, e.test_code),
        # 隐藏考卷只给题数和哈希，不给题目
        "hidden_exams": [{"id": h.id, "tests": len(h.tests), "test_sha256": h.test_sha256,
                          "created_at": h.created_at.isoformat()} for h in hidden],
    }


@router.get("/verifications/{verification_id}")
async def get_verification(request: Request, verification_id: int) -> dict[str, Any]:
    async with _failgate(request).db.session() as s:
        v = await s.get(VerificationRecord, verification_id)
        if v is None:
            raise HTTPException(status_code=404, detail="verification not found")
    return {"id": v.id, "case_id": v.case_id, "pr": v.pr_number, "verdict": v.verdict,
            "receipt": v.receipt, "receipt_sha256": v.receipt_sha256,
            "problems": check_receipt(v.receipt),
            "created_at": v.created_at.isoformat()}


# ---------------------------------------------------------------- 工作台页面


@console_router.get("/console", response_class=HTMLResponse, include_in_schema=False)
async def console(request: Request, token: str | None = None) -> Any:
    """单页工作台（failgate/console/index.html，纯静态 + 调上面的只读 API）。

    带 ?token= 打开时校验后写 cookie 并去掉地址栏里的 token，之后的 API 请求靠 cookie。"""
    expected = _token(request)
    if token is not None and expected:
        if not hmac.compare_digest(token, expected):
            raise HTTPException(status_code=401, detail="token 不对")
        resp = RedirectResponse("/console", status_code=303)
        resp.set_cookie(COOKIE, token, httponly=True, samesite="strict")
        return resp
    require_console(request)
    html = resources.files("failgate.console").joinpath("index.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


__all__ = ["COOKIE", "console_router", "require_console", "router"]
