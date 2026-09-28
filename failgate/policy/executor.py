"""EffectExecutor：把 PolicyGate 放行（status=pending）的写操作真正发到平台上。

为什么和 PolicyGate 分开：Gate 在编排的数据库事务里做"决定"（写一行 Effect），
执行器在事务之外做"执行"（调网络 API）。网络调用慢、会失败、要重试，不能夹在事务里。

可靠性：
- 至多一条汇总评论：Case 上记着 summary_comment_id，之后都是编辑它；
  评论里带一个隐藏标记，万一"评论发出去了、ID 没来得及落库"就崩溃，
  下次先按标记把它找回来，不会重复发。
- 失败分两类：可重试（5xx、限流、网络错误）留在 pending，下次再试，最多 MAX_ATTEMPTS 次；
  不可重试（权限不足、参数错误）直接 failed。
- 发出前做密钥扫描，命中就 blocked，永远不发。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from failgate.db import Case, Database, Effect, Repo
from failgate.platforms.base import CaseKind, CaseRef, PlatformError, PlatformWriter, RepoRef

from .secrets_scan import find_secrets

log = logging.getLogger(__name__)

SUMMARY_MARKER = "<!-- failgate:summary -->"
# 改名前（RepoWarden）发出的汇总评论带的是旧标记：找不到新标记时再找旧的，编辑它而不是再发一条
LEGACY_SUMMARY_MARKERS = ("<!-- repowarden:summary -->",)
MAX_ATTEMPTS = 3

WriterFor = Callable[[Repo], PlatformWriter | None]


@dataclass(frozen=True)
class _Outcome:
    status: str
    error: str | None = None
    comment_id: str | None = None


class EffectExecutor:
    def __init__(self, db: Database, writer_for: WriterFor) -> None:
        self.db = db
        self.writer_for = writer_for
        # 同一进程里 worker 和定时补偿可能同时 flush，加锁避免同一条评论被发两次。
        # 多进程部署时要换成 Case 级分布式锁（技术方案第 5 节）
        self._lock = asyncio.Lock()

    async def flush(self, case_id: int) -> dict[str, int]:
        """执行某个 Case 所有 pending 的 Effect，返回各状态的计数。"""
        async with self._lock:
            return await self._flush(case_id)

    async def flush_all(self) -> dict[str, int]:
        """补偿：扫一遍所有还有 pending Effect 的 Case（给之前失败待重试的）。"""
        async with self.db.session() as s:
            ids = (
                await s.scalars(select(Effect.case_id).where(Effect.status == "pending").distinct())
            ).all()
        total: dict[str, int] = {}
        for case_id in ids:
            for k, v in (await self.flush(case_id)).items():
                total[k] = total.get(k, 0) + v
        return total

    async def run_forever(self, interval: float = 60.0) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self.flush_all()
            except Exception:
                log.exception("effect flush_all failed")

    async def _flush(self, case_id: int) -> dict[str, int]:
        async with self.db.session() as s:
            case = await s.get(Case, case_id)
            repo = await s.get(Repo, case.repo_id) if case else None
            if case is None or repo is None:
                return {}
            effects = (
                await s.scalars(
                    select(Effect)
                    .where(Effect.case_id == case_id, Effect.status == "pending")
                    .order_by(Effect.created_at)
                )
            ).all()
        if not effects:
            return {}
        writer = self.writer_for(repo)
        if writer is None:
            log.warning(
                "case %s has %d pending effects but no writer for %s (GitHub App not configured "
                "or installation unknown)",
                case_id, len(effects), repo.full_name,
            )
            return {"no_writer": len(effects)}

        ref = CaseRef(
            repo=RepoRef(platform=repo.platform, full_name=repo.full_name),
            kind=CaseKind(case.kind),
            number=case.number,
        )
        summary_id = case.summary_comment_id
        counts: dict[str, int] = {}
        for effect in effects:
            outcome = await self._execute(writer, ref, effect, summary_id, effect.attempts + 1)
            if outcome.comment_id is not None:
                summary_id = outcome.comment_id
            await self._record(case_id, effect.effect_key, outcome)
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
        return counts

    async def _execute(
        self,
        writer: PlatformWriter,
        ref: CaseRef,
        effect: Effect,
        summary_id: str | None,
        attempt: int,
    ) -> _Outcome:
        hits = find_secrets(json.dumps(effect.payload, ensure_ascii=False))
        if hits:
            log.warning("effect %s blocked: secret-like content %s", effect.effect_key[:12], hits)
            return _Outcome("blocked", f"secret-like content: {', '.join(hits)}")
        try:
            if effect.action == "set_labels":
                await writer.set_labels(
                    ref, list(effect.payload.get("add", [])), list(effect.payload.get("remove", []))
                )
                return _Outcome("executed")
            if effect.action == "upsert_summary":
                cid = await self._upsert_summary(writer, ref, effect.payload["body"], summary_id)
                return _Outcome("executed", comment_id=cid)
            return _Outcome("failed", f"unknown action: {effect.action}")
        except PlatformError as e:
            retry = e.retryable and attempt < MAX_ATTEMPTS
            return _Outcome("pending" if retry else "failed", str(e)[:2000])
        except Exception as e:  # 网络超时、连接失败等，按可重试处理
            retry = attempt < MAX_ATTEMPTS
            return _Outcome("pending" if retry else "failed", f"{type(e).__name__}: {e}"[:2000])

    async def _upsert_summary(
        self, writer: PlatformWriter, ref: CaseRef, body: str, summary_id: str | None
    ) -> str:
        body = f"{body}\n\n{SUMMARY_MARKER}"
        if summary_id is None:
            # 库里没有 ID：可能是第一次发，也可能是上次发完没来得及记下就崩溃了
            for marker in (SUMMARY_MARKER, *LEGACY_SUMMARY_MARKERS):
                summary_id = await writer.find_comment(ref, marker)
                if summary_id is not None:
                    break
        if summary_id is not None:
            try:
                await writer.update_comment(ref, summary_id, body)
                return summary_id
            except PlatformError as e:
                if e.status != 404:
                    raise
                # 评论被人删掉了：重新发一条
        return await writer.create_comment(ref, body)

    async def _record(self, case_id: int, key: str, outcome: _Outcome) -> None:
        async with self.db.session() as s, s.begin():
            effect = await s.get(Effect, key)
            assert effect is not None
            effect.attempts += 1
            effect.status = outcome.status
            effect.error = outcome.error
            if outcome.status == "executed":
                effect.executed_at = datetime.now(UTC)
            if outcome.comment_id is not None:
                case = await s.get(Case, case_id)
                assert case is not None
                case.summary_comment_id = outcome.comment_id
