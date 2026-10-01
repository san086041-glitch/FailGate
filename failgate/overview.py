"""数据概览：工作台的 /api/stats 和 CLI 首页共用（ADR 0035、0036）。

单独成模块、不 import FastAPI：CLI 首页要在 1 秒内出结果。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from failgate.db import Case, Evidence, Repo, VerificationRecord


async def collect_stats(s: AsyncSession) -> dict[str, Any]:
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


async def latest_verification(s: AsyncSession) -> dict[str, Any] | None:
    """最近一次核验：仓库、PR 号、结论、时间。"""
    row = (await s.execute(
        select(VerificationRecord.pr_number, VerificationRecord.verdict,
               VerificationRecord.created_at, Repo.full_name)
        .join(Case, VerificationRecord.case_id == Case.id)
        .join(Repo, Case.repo_id == Repo.id)
        .order_by(VerificationRecord.id.desc()).limit(1))).first()
    if row is None:
        return None
    pr, verdict, at, repo = row
    return {"repo": repo, "pr": pr, "verdict": verdict, "at": at}
