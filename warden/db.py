"""持久化模型。M0 用 SQLite，表结构与技术方案第 14 节一致，之后可平移到 PostgreSQL。"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Repo(Base):
    __tablename__ = "repos"
    __table_args__ = (UniqueConstraint("platform", "full_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(32))
    full_name: Mapped[str] = mapped_column(String(255))
    mode: Mapped[str] = mapped_column(String(16), default="shadow")
    created_at: Mapped[datetime] = mapped_column(default=_now)


class Delivery(Base):
    """已收到的 webhook 投递，主键即平台的 delivery id，用于幂等。"""

    __tablename__ = "deliveries"

    delivery_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    platform: Mapped[str] = mapped_column(String(32))
    event: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(default=_now)


class Case(Base):
    __tablename__ = "cases"
    __table_args__ = (UniqueConstraint("repo_id", "kind", "number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id"))
    kind: Mapped[str] = mapped_column(String(16))
    number: Mapped[int]
    state: Mapped[str] = mapped_column(String(32))
    state_version: Mapped[int] = mapped_column(default=0)
    author_login: Mapped[str | None] = mapped_column(String(255), default=None)
    title: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    spent_usd: Mapped[float] = mapped_column(default=0.0)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)


class TransitionLog(Base):
    __tablename__ = "transitions"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"))
    from_state: Mapped[str] = mapped_column(String(32))
    to_state: Mapped[str] = mapped_column(String(32))
    event: Mapped[str] = mapped_column(String(64))
    at: Mapped[datetime] = mapped_column(default=_now)


class IssueDoc(Base):
    """查重用的历史 issue 语料：来自 webhook（新 issue）和 `warden index build`（回填）。"""

    __tablename__ = "issue_docs"
    __table_args__ = (UniqueConstraint("repo_id", "number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id"), index=True)
    number: Mapped[int]
    title: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(String(16), default="open")
    # GitHub 的关闭原因：completed / not_planned / duplicate / reopened
    state_reason: Mapped[str | None] = mapped_column(String(32), default=None)
    labels: Mapped[list[str]] = mapped_column(JSON, default=list)
    url: Mapped[str | None] = mapped_column(String(512), default=None)
    trace_sig: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    embedding: Mapped[list[float] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)


class Run(Base):
    """能力模块的一次执行。输出、用量、花费都落库，供控制台展示和回放评测对比。"""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"))
    skill: Mapped[str] = mapped_column(String(32))
    skill_version: Mapped[str] = mapped_column(String(16))
    model: Mapped[str | None] = mapped_column(String(64), default=None)
    # ok | error
    status: Mapped[str] = mapped_column(String(16))
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    confidence: Mapped[float | None] = mapped_column(default=None)
    tokens_in: Mapped[int] = mapped_column(default=0)
    tokens_out: Mapped[int] = mapped_column(default=0)
    tokens_cached: Mapped[int] = mapped_column(default=0)
    usd: Mapped[float] = mapped_column(default=0.0)
    started_at: Mapped[datetime] = mapped_column(default=_now)
    ended_at: Mapped[datetime] = mapped_column(default=_now)


class Effect(Base):
    """对外写操作的审计记录；effect_key 保证同一动作只执行一次。"""

    __tablename__ = "effects"

    effect_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"))
    action: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    mode: Mapped[str] = mapped_column(String(16))
    # shadowed：影子模式只记录 · pending：等待执行器执行 · executed · skipped
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(default=_now)
    executed_at: Mapped[datetime | None] = mapped_column(default=None)


class Database:
    def __init__(self, url: str) -> None:
        self.engine = create_async_engine(url)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    def session(self) -> AsyncSession:
        return self.sessionmaker()

    async def dispose(self) -> None:
        await self.engine.dispose()
