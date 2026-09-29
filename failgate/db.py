"""持久化模型。开发和测试用 SQLite，部署用 PostgreSQL（ADR 0024，Alembic 迁移）。"""

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Connection,
    DateTime,
    Dialect,
    Float,
    ForeignKey,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    event,
)
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, InstanceState, Mapped, Session, mapped_column

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """库里一律存"不带时区的 UTC"，读出来也是不带时区的 UTC。

    代码里写入的时间都带时区（datetime.now(UTC)）。SQLite 会悄悄丢掉时区；
    asyncpg 往 TIMESTAMP 列写带时区的值会直接报错。在这里统一换算，两种库行为一致，
    查询条件里传带时区或不带时区的值都行（不带时区的按 UTC 理解）。
    """

    impl = DateTime(timezone=False)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            return value.astimezone(UTC).replace(tzinfo=None)
        return value


class Base(DeclarativeBase):
    # float 显式映射成 Float：SQLAlchemy 2.1 起默认是 Double，和 2.0 下生成的迁移对不上
    type_annotation_map = {datetime: UTCDateTime, float: Float}


class Repo(Base):
    __tablename__ = "repos"
    __table_args__ = (UniqueConstraint("platform", "full_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(32))
    full_name: Mapped[str] = mapped_column(String(255))
    mode: Mapped[str] = mapped_column(String(16), default="shadow")
    # GitHub App 的安装 ID：用它换安装令牌。从 webhook 里带过来，随事件更新
    installation_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    # 自动打标签的白名单（通配符模式，如 ["T: *", "C: *"]）；None = 不限制，只拦结论 / 进度类标签
    auto_labels: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    # 复现（package 模式）：PyPI 包名和 import 名。包名为空 = 这个仓库不做复现
    repro_package: Mapped[str | None] = mapped_column(String(255), default=None)
    repro_import_name: Mapped[str | None] = mapped_column(String(255), default=None)
    # 源码仓库（owner/name）：报告的是未发布版本时走 source 模式（L2）。为空 = 只用 package 模式
    repro_source: Mapped[str | None] = mapped_column(String(255), default=None)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class Delivery(Base):
    """已收到的 webhook 投递，主键即平台的 delivery id，用于幂等。"""

    __tablename__ = "deliveries"

    delivery_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    platform: Mapped[str] = mapped_column(String(32))
    event: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(default=_now, index=True)
    # 排队延迟测量（W6）：worker 开始 / 处理完（含写回平台）的时间，和这次事件落到的 Case
    started_at: Mapped[datetime | None] = mapped_column(default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    case_id: Mapped[int | None] = mapped_column(default=None)


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
    # 平台上那条汇总评论的 ID：每个 Case 只维护一条，之后都是编辑它
    summary_comment_id: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)


class TransitionLog(Base):
    __tablename__ = "transitions"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), index=True)
    from_state: Mapped[str] = mapped_column(String(32))
    to_state: Mapped[str] = mapped_column(String(32))
    event: Mapped[str] = mapped_column(String(64))
    # 触发这次转换的人（外部事件的发起人）；能力模块完成等内部事件为空。
    # 重新封存考卷时要记下是哪位维护者做的决定
    actor: Mapped[str | None] = mapped_column(String(255), default=None)
    at: Mapped[datetime] = mapped_column(default=_now)


class IssueDoc(Base):
    """查重用的历史 issue 语料：来自 webhook（新 issue）和 `failgate index build`（回填）。"""

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


class DocChunk(Base):
    """文档切块（README、docs/、CHANGELOG…），供 Answer 检索引用。由 `failgate index docs` 构建。"""

    __tablename__ = "doc_chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id"), index=True)
    path: Mapped[str] = mapped_column(String(512))
    # 标题路径，例如 "Usage › Configuration"
    heading: Mapped[str] = mapped_column(Text, default="")
    text: Mapped[str] = mapped_column(Text)
    # 带 commit SHA 的永久链接（+ 标题锚点）
    url: Mapped[str] = mapped_column(String(1024))
    commit_sha: Mapped[str] = mapped_column(String(64))
    # 向量（可选）：第一次语义检索时按需计算
    embedding: Mapped[list[float] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class Run(Base):
    """能力模块的一次执行。输出、用量、花费都落库，供控制台展示和回放评测对比。"""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), index=True)
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
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), index=True)
    action: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    mode: Mapped[str] = mapped_column(String(16))
    # shadowed：影子模式只记录 · pending：等待执行器执行 · executed：已执行
    # failed：多次失败或不可重试的错误 · blocked：发出前被拦下（例如疑似含密钥）· skipped
    status: Mapped[str] = mapped_column(String(16), index=True)
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    executed_at: Mapped[datetime | None] = mapped_column(default=None)


class Evidence(Base):
    """复现证据与考卷封存（技术方案 9.4、16 节）。

    判定为复现（L1 / L2）时写一行，之后**不再修改**：核验 PR 时跑的永远是这里的 test_code。
    唯一允许改的是 superseded_by——维护者 /failgate reseal 重新封存时，旧行指向新行（W2–4）。
    """

    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # = 收据的 evidence_id
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), index=True)
    level: Mapped[str] = mapped_column(String(8))
    mode: Mapped[str] = mapped_column(String(16))
    acceptance: Mapped[bool]  # 能不能当考卷（只有 L2 能）
    test_path: Mapped[str] = mapped_column(String(512))
    test_code: Mapped[str] = mapped_column(Text)
    test_sha256: Mapped[str] = mapped_column(String(64))
    source_repo: Mapped[str | None] = mapped_column(String(255), default=None)
    source_sha: Mapped[str | None] = mapped_column(String(64), default=None)
    python: Mapped[str | None] = mapped_column(String(16), default=None)
    pytest: Mapped[str | None] = mapped_column(String(64), default=None)
    verdict: Mapped[str] = mapped_column(String(32))
    fail_rate: Mapped[float | None] = mapped_column(default=None)
    receipt: Mapped[dict[str, Any]] = mapped_column(JSON)
    receipt_sha256: Mapped[str] = mapped_column(String(64))
    superseded_by: Mapped[str | None] = mapped_column(String(32), default=None)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class VerificationRecord(Base):
    """一次 PR 核验（ADR 0018）：核验收据原样存下，只加不改。"""

    __tablename__ = "verifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), index=True)
    pr_number: Mapped[int]
    base_sha: Mapped[str] = mapped_column(String(64))
    head_sha: Mapped[str] = mapped_column(String(64))
    verdict: Mapped[str | None] = mapped_column(String(16), default=None)  # None：没有声明
    receipt: Mapped[dict[str, Any]] = mapped_column(JSON)
    receipt_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=_now)


class HiddenExamRecord(Base):
    """隐藏考卷（ADR 0021）：只根据 issue 出的变体题，封存但不公开。只加不改。

    一份公开考卷（evidence）可以有多份隐藏考卷（重新出题），核验时用最新的一份。"""

    __tablename__ = "hidden_exams"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # = 收据的 hidden_id
    evidence_id: Mapped[str] = mapped_column(ForeignKey("evidence.id"), index=True)
    test_path: Mapped[str] = mapped_column(String(512))
    test_code: Mapped[str] = mapped_column(Text)
    test_sha256: Mapped[str] = mapped_column(String(64))
    tests: Mapped[list[str]] = mapped_column(JSON)
    receipt: Mapped[dict[str, Any]] = mapped_column(JSON)
    receipt_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=_now)


SEALED_MUTABLE = frozenset({"superseded_by"})
SEALED_TYPES = (Evidence, VerificationRecord, HiddenExamRecord)


class SealedEvidenceError(RuntimeError):
    pass


@event.listens_for(Session, "before_flush")
def _guard_sealed_evidence(session: Session, _ctx: Any, _instances: Any) -> None:
    """封存的证据只能加、不能改（除了 superseded_by）、不能删。

    在 ORM 这一层拦，挡住的是"代码里不小心改了"；直接写 SQL 挡不住，那要靠数据库权限。
    """
    for obj in session.dirty:
        if not isinstance(obj, SEALED_TYPES):
            continue
        state: InstanceState[Any] = sa_inspect(obj)
        changed = {a.key for a in state.attrs if a.history.has_changes()}
        allowed = SEALED_MUTABLE if isinstance(obj, Evidence) else frozenset()
        if changed - allowed:
            raise SealedEvidenceError(
                f"{obj.__tablename__} {obj.id} 已封存，不能修改：{sorted(changed)}"
            )
    for obj in session.deleted:
        if isinstance(obj, SEALED_TYPES):
            raise SealedEvidenceError(f"{obj.__tablename__} {obj.id} 已封存，不能删除")


def add_missing_columns(conn: Connection) -> list[str]:
    """最小的"迁移"：只给已有的表补上新增的列。

    create_all 只建不存在的表，不会改已有的表；项目还没引入 Alembic，
    新增一个可空（或带数据库默认值）的列时靠这里补齐，不用删库重建。
    改列类型、删列、加非空无默认值的列仍然需要真正的迁移工具。
    """
    insp = sa_inspect(conn)
    added: list[str] = []
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing:
                continue
            if not col.nullable and col.server_default is None:
                raise RuntimeError(
                    f"{table.name}.{col.name} 是非空且没有数据库默认值的新列，无法自动补齐；"
                    "请删库重建或手工迁移"
                )
            ddl = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(conn.dialect)}"
            if col.server_default is not None:
                ddl += f" DEFAULT {col.server_default.arg}"  # type: ignore[attr-defined]
            if not col.nullable:
                ddl += " NOT NULL"
            conn.exec_driver_sql(ddl)
            added.append(f"{table.name}.{col.name}")
            log.info("added column %s.%s", table.name, col.name)
    return added


class Database:
    def __init__(self, url: str) -> None:
        self.engine = create_async_engine(url)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    @property
    def is_sqlite(self) -> bool:
        return self.engine.dialect.name == "sqlite"

    async def create_all(self) -> None:
        """准备好表结构。所有入口（serve、worker、CLI）启动时都调用它。

        - SQLite（开发、测试、回放库）：create_all + 补可空列，和以前一样；
        - 其他（PostgreSQL）：Alembic 升级到最新版本（ADR 0024），多个进程同时启动时
          用 advisory lock 串行。
        """
        async with self.engine.begin() as conn:
            if self.is_sqlite:
                await conn.run_sync(Base.metadata.create_all)
                await conn.run_sync(add_missing_columns)
            else:
                from failgate.migrations import upgrade_to_head

                await conn.run_sync(upgrade_to_head)

    def session(self) -> AsyncSession:
        return self.sessionmaker()

    async def dispose(self) -> None:
        await self.engine.dispose()
