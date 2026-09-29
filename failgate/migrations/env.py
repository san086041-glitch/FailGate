"""Alembic 运行环境。

两种用法：
- 程序里传进来一个已打开的连接（config.attributes["connection"]），在它上面跑；
- 命令行 `alembic …`：用 `-x db=<url>`、alembic.ini 里的 sqlalchemy.url，或配置里的
  FAILGATE_DB_URL 建一个 async 引擎。
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from failgate.db import Base, UTCDateTime

config = context.config
target_metadata = Base.metadata


def render_item(type_: str, obj: Any, autogen_context: AutogenContext) -> str | Literal[False]:
    # UTCDateTime 只是在 Python 这一侧换算时区，库里就是普通的 DateTime：
    # 迁移脚本里写成 sa.DateTime()，不依赖 failgate 的代码
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime()"
    return False


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_item=render_item,
        # SQLite 不支持大多数 ALTER TABLE：用"建新表、拷数据、换名"的批处理模式
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
    )


def _run(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


def _url() -> str:
    x = context.get_x_argument(as_dictionary=True)
    url = x.get("db") or config.get_main_option("sqlalchemy.url")
    if not url:
        from failgate.settings import Settings

        url = Settings().failgate_db_url
    return url


async def _run_async() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as conn:
        await conn.run_sync(_run)
        await conn.commit()
    await engine.dispose()


connection = config.attributes.get("connection")
if connection is not None:
    _run(connection)
elif context.is_offline_mode():
    context.configure(url=_url(), target_metadata=target_metadata, render_item=render_item,
                      literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_run_async())
