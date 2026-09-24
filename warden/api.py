"""控制台只读 API（M0）。M1 起加上认证和影子动作的审批接口。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from warden.db import Case, Effect, Repo, TransitionLog

if TYPE_CHECKING:
    from warden.app import Warden

router = APIRouter(prefix="/api")


def _warden(request: Request) -> Warden:
    return request.app.state.warden


@router.get("/cases")
async def list_cases(request: Request, state: str | None = None) -> list[dict[str, Any]]:
    async with _warden(request).db.session() as s:
        q = select(Case, Repo).join(Repo, Case.repo_id == Repo.id).order_by(Case.id.desc())
        if state:
            q = q.where(Case.state == state)
        rows = (await s.execute(q.limit(200))).all()
    return [
        {
            "id": c.id,
            "repo": r.full_name,
            "platform": r.platform,
            "kind": c.kind,
            "number": c.number,
            "state": c.state,
            "updated_at": c.updated_at.isoformat(),
        }
        for c, r in rows
    ]


@router.get("/cases/{case_id}")
async def get_case(request: Request, case_id: int) -> dict[str, Any]:
    async with _warden(request).db.session() as s:
        case = await s.get(Case, case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="case not found")
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
    return {
        "id": case.id,
        "kind": case.kind,
        "number": case.number,
        "state": case.state,
        "state_version": case.state_version,
        "transitions": [
            {"from": t.from_state, "to": t.to_state, "event": t.event, "at": t.at.isoformat()}
            for t in transitions
        ],
        "effects": [
            {"action": e.action, "payload": e.payload, "mode": e.mode, "status": e.status}
            for e in effects
        ],
    }
