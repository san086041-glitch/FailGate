"""Alembic 迁移（ADR 0024）。

- 程序里调用：`upgrade_to_head(conn)`（Database.create_all 在 PostgreSQL 上用它）；
- 命令行：`failgate db upgrade` / `failgate db current`；
- 开发者加新迁移：改完 db.py 后在仓库根目录 `alembic revision --autogenerate -m "…"`，
  用 `-x db=<url>` 指定一个已升级到最新版本的库（默认读配置里的 FAILGATE_DB_URL）。
  `tests/test_migrations.py` 会检查迁移和模型是否一致，忘了写迁移测试就会失败。
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection

HERE = Path(__file__).parent
# 所有进程（serve、各个 worker）启动时都会尝试升级：用同一把 advisory lock 串行
ADVISORY_LOCK_ID = 0x6661696C67617465  # "failgate"


def config(url: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(HERE))
    if url:
        cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def head() -> str | None:
    return ScriptDirectory.from_config(config()).get_current_head()


def current(conn: Connection) -> str | None:
    return MigrationContext.configure(conn).get_current_revision()


def upgrade_to_head(conn: Connection) -> None:
    """在给定连接上升级到最新版本（同步函数，配合 AsyncConnection.run_sync 用）。"""
    if conn.dialect.name == "postgresql":
        # 事务级锁：这个事务提交或回滚时自动释放
        conn.exec_driver_sql(f"SELECT pg_advisory_xact_lock({ADVISORY_LOCK_ID})")
    cfg = config()
    cfg.attributes["connection"] = conn
    command.upgrade(cfg, "head")


def downgrade(conn: Connection, target: str) -> None:
    cfg = config()
    cfg.attributes["connection"] = conn
    command.downgrade(cfg, target)
