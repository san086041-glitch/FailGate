"""排队延迟测量（W6 平台工程，先量后改）。

单个 worker 按到达顺序处理事件：一个慢的复现 / 核验（几十秒到几分钟）会让排在后面的
分诊、答疑一起等。这里把每个 webhook 投递拆成三段：

    received_at ──排队──▶ started_at ──处理（含写回平台）──▶ finished_at

两个数据来源：
- 服务端：deliveries 表（worker 记的开始 / 结束时间）+ transitions 表（判断这次处理
  有没有进沙箱：REPRODUCING / VERIFYING / RESEALING）；
- GitHub：issue 创建（或命令评论）到 bot 评论出现 / 被更新的时间，两边都是 GitHub 的时钟，
  不受本机时钟偏差影响，也包括 smee 转发的耗时。
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from failgate.db import Case, Database, Delivery, Repo, Run, TransitionLog

SANDBOX_STATES = frozenset({"REPRODUCING", "VERIFYING", "RESEALING"})
SANDBOX_SKILLS = ("repro", "verify", "reseal")


def _utc(dt: datetime | None) -> datetime | None:
    # SQLite 读回来的是不带时区的 UTC
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass
class EventTiming:
    delivery_id: str
    event: str
    received_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    kind: str | None = None
    number: int | None = None
    states: list[str] = field(default_factory=list)
    # GitHub 侧：触发时间（issue 创建 / 命令评论）和 bot 评论出现的时间
    gh_trigger: datetime | None = None
    gh_reply: datetime | None = None
    # 沙箱阶段出结果的时间（那次复现 / 核验 Run 的结束时间）。拆车道以后（ADR 0023），
    # 事件在快车道上几秒就处理完、只投一个沙箱任务，结果要看沙箱车道什么时候跑完
    result_at: datetime | None = None

    @property
    def sandbox(self) -> bool:
        return any(s in SANDBOX_STATES for s in self.states)

    @property
    def queue_wait(self) -> float | None:
        return _secs(self.received_at, self.started_at)

    @property
    def service(self) -> float | None:
        return _secs(self.started_at, self.finished_at)

    @property
    def total(self) -> float | None:
        """收到 → 这个事件的结果全部写回（沙箱事件算到沙箱阶段结束）。"""
        ends = [t for t in (self.finished_at, self.result_at) if t is not None]
        return _secs(self.received_at, max(ends)) if ends else None

    @property
    def gh_latency(self) -> float | None:
        return _secs(self.gh_trigger, self.gh_reply)


def _secs(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return (b - a).total_seconds()


async def load_timings(db: Database, repo: str, since: datetime,
                       until: datetime | None = None) -> list[EventTiming]:
    """repo 在 [since, until) 内收到、并且落到了某个 Case 的投递，按到达顺序。"""
    async with db.session() as s:
        q = (
            select(Delivery, Case)
            .join(Case, Case.id == Delivery.case_id)
            .join(Repo, Repo.id == Case.repo_id)
            .where(Repo.full_name == repo, Delivery.received_at >= since)
            .order_by(Delivery.received_at)
        )
        if until is not None:
            q = q.where(Delivery.received_at < until)
        pairs = (await s.execute(q)).all()
        out: list[EventTiming] = []
        for d, case in pairs:
            row = EventTiming(
                delivery_id=d.delivery_id, event=d.event,
                received_at=_utc(d.received_at) or since,
                started_at=_utc(d.started_at), finished_at=_utc(d.finished_at),
                kind=case.kind, number=case.number,
            )
            if row.started_at and row.finished_at:
                tq = (
                    select(TransitionLog.to_state)
                    .where(TransitionLog.case_id == case.id,
                           TransitionLog.at >= row.started_at.replace(tzinfo=None),
                           TransitionLog.at <= row.finished_at.replace(tzinfo=None))
                    .order_by(TransitionLog.id)
                )
                row.states = list((await s.execute(tq)).scalars())
            if row.sandbox and row.started_at:
                rq = (
                    select(Run.ended_at)
                    .where(Run.case_id == case.id, Run.skill.in_(SANDBOX_SKILLS),
                           Run.started_at >= row.started_at.replace(tzinfo=None))
                    .order_by(Run.started_at).limit(1)
                )
                row.result_at = _utc(await s.scalar(rq))
            out.append(row)
    return out


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def _is_bot(c: dict[str, Any], bot_login: str | None) -> bool:
    login = str((c.get("user") or {}).get("login", ""))
    return login == bot_login if bot_login else login.endswith("[bot]")


def bot_reply_after(comments: Iterable[dict[str, Any]], trigger: datetime,
                    bot_login: str | None = None) -> datetime | None:
    """trigger 之后 bot 最早一次发出或更新评论的时间。

    汇总评论是单条 upsert：第一次是新建（看 created_at），之后是编辑（只能看到最后一次
    updated_at）。所以同一个 Case 在一轮测量里只应该有一个触发。
    """
    best: datetime | None = None
    for c in comments:
        if not _is_bot(c, bot_login):
            continue
        created, updated = _parse(c.get("created_at")), _parse(c.get("updated_at"))
        t = created if created and created >= trigger else updated
        if t is not None and t >= trigger and (best is None or t < best):
            best = t
    return best


def command_trigger(comments: Iterable[dict[str, Any]], before: datetime,
                    prefix: tuple[str, ...] = ("/failgate", "/warden")) -> datetime | None:
    """before（服务端收到的时间）之前最近一条命令评论的创建时间。"""
    best: datetime | None = None
    for c in comments:
        if not str(c.get("body", "")).lstrip().startswith(prefix):
            continue
        t = _parse(c.get("created_at"))
        # 本机时钟可能比 GitHub 快几分钟：留 10 分钟余量，取最近的一条
        if t is not None and (t - before).total_seconds() < 600 and (best is None or t > best):
            best = t
    return best


async def attach_github(rows: Sequence[EventTiming], repo: str, rest: Any,
                        bot_login: str | None = None) -> None:
    """补上 GitHub 侧的触发时间和 bot 回复时间（issue.opened 和命令评论）。"""
    comments: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        if row.number is None:
            continue
        if row.number not in comments:
            comments[row.number] = await rest.list_comments(repo, row.number)
        cs = comments[row.number]
        if row.event == "issue.opened":
            row.gh_trigger = _parse((await rest.issue(repo, row.number)).get("created_at"))
        elif row.event == "comment.created":
            row.gh_trigger = command_trigger(cs, row.received_at)
        if row.gh_trigger is not None:
            row.gh_reply = bot_reply_after(cs, row.gh_trigger, bot_login)


def percentile(values: Sequence[float], q: float) -> float:
    """最近秩法；样本少时 p95 就是最大值，报告里会注明 n。"""
    xs = sorted(values)
    return xs[max(0, math.ceil(q * len(xs)) - 1)]


def _stats(values: Sequence[float | None]) -> dict[str, float | int] | None:
    xs = [v for v in values if v is not None]
    if not xs:
        return None
    return {"n": len(xs), "p50": statistics.median(xs), "p95": percentile(xs, 0.95),
            "max": max(xs)}


def summarize(rows: Sequence[EventTiming]) -> dict[str, Any]:
    """按"有没有进沙箱"分两组：快事件（分诊、查重、答疑）和慢事件（复现、核验）。"""
    out: dict[str, Any] = {}
    for name, group in (("fast", [r for r in rows if not r.sandbox]),
                        ("sandbox", [r for r in rows if r.sandbox])):
        out[name] = {
            "n": len(group),
            "queue_wait": _stats([r.queue_wait for r in group]),
            "service": _stats([r.service for r in group]),
            "total": _stats([r.total for r in group]),
            "github": _stats([r.gh_latency for r in group]),
        }
    return out


def _fmt(v: float | None) -> str:
    return "—" if v is None else f"{v:.1f}"


def render(rows: Sequence[EventTiming], summary: dict[str, Any], *, repo: str,
           title: str, notes: Sequence[str] = ()) -> str:
    lines = [f"# {title}", "", f"仓库：`{repo}`。时间单位：秒。", ""]
    lines += list(notes) + ([""] if notes else [])
    lines += ["## 汇总", "",
              "| 组 | n | 指标 | p50 | p95 | max |", "|---|---|---|---|---|---|"]
    labels = {"queue_wait": "排队", "service": "处理", "total": "服务端合计（到结果）",
              "github": "GitHub：触发 → bot 评论"}
    for group, g in summary.items():
        name = "快事件" if group == "fast" else "沙箱事件"
        for key, label in labels.items():
            st = g[key]
            if st is None:
                continue
            lines.append(f"| {name} | {st['n']} | {label} | {_fmt(st['p50'])} | "
                         f"{_fmt(st['p95'])} | {_fmt(st['max'])} |")
    lines += ["", "p95 用最近秩法；n < 20 时它就是最大值，只能当参考。", "",
              "## 明细（按到达顺序）", "",
              "| # | 事件 | Case | 进沙箱 | 排队 | 处理 | 服务端合计 | GitHub |",
              "|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        case = f"{r.kind} #{r.number}" if r.number is not None else "—"
        lines.append(f"| {i} | {r.event} | {case} | {'是' if r.sandbox else ''} | "
                     f"{_fmt(r.queue_wait)} | {_fmt(r.service)} | {_fmt(r.total)} | "
                     f"{_fmt(r.gh_latency)} |")
    return "\n".join(lines) + "\n"
