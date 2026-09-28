"""PolicyGate：写操作先落成 Effect 记录，再由执行器按模式执行。

M0 只负责记录：影子模式 → shadowed；正常模式 → pending。
EffectExecutor 取出 pending 记录调用 PlatformWriter 执行。
打标签还要过一道标签策略（labels.py）：结论 / 进度类标签和不在仓库白名单里的标签不会自动打上，
被拦下的写进 payload 留档；全部被拦时这条 Effect 标为 blocked，永远不会执行。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from failgate.db import Case, Effect, Repo

from .labels import filter_auto_labels

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
        status = _STATUS_BY_MODE.get(repo.mode, "skipped")
        if action == "set_labels":
            payload, all_blocked = _label_policy(repo, payload)
            if all_blocked:
                status = "blocked"
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
            status=status,
        )
        session.add(effect)
        return effect


def _label_policy(repo: Repo, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """按标签策略过滤要自动打的标签；被拦下的写进 payload 留档。返回 (新 payload, 是否全部被拦)。"""
    wanted = list(payload.get("add", []))
    kept, blocked = filter_auto_labels(wanted, repo.auto_labels)
    out = {**payload, "add": kept}
    if blocked:
        out["blocked"] = blocked
    return out, bool(wanted) and not kept
