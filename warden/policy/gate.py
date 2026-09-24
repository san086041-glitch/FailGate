"""PolicyGate：写操作先落成 Effect 记录，再由执行器按模式执行。

M0 只负责记录：影子模式 → shadowed；正常模式 → pending。
M1 增加 EffectExecutor，取出 pending 记录调用 PlatformWriter 执行，并接入预算与审批。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from warden.db import Case, Effect, Repo

_STATUS_BY_MODE = {"shadow": "shadowed", "live": "pending"}


def effect_key(case_id: int, action: str, payload: dict[str, Any]) -> str:
    raw = json.dumps(
        {"case": case_id, "action": action, "payload": payload},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


class PolicyGate:
    async def propose(
        self,
        session: AsyncSession,
        *,
        repo: Repo,
        case: Case,
        action: str,
        payload: dict[str, Any],
    ) -> Effect:
        key = effect_key(case.id, action, payload)
        existing = await session.get(Effect, key)
        if existing is not None:
            return existing
        effect = Effect(
            effect_key=key,
            case_id=case.id,
            action=action,
            payload=payload,
            mode=repo.mode,
            status=_STATUS_BY_MODE.get(repo.mode, "skipped"),
        )
        session.add(effect)
        return effect
