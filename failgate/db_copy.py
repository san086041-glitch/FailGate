"""把一个库整个复制到另一个库（ADR 0024）：本机一直用的 SQLite 库搬到 PostgreSQL。

- 目标库先升级到最新迁移，所有表必须是空的（不做合并）；
- 按外键顺序逐表复制，只复制源表里有的列（老库缺的列用模型默认值）；
- 走 SQLAlchemy Core，不经过 ORM：封存证据的"只加不改"守卫不会被触发，数据原样搬过去；
- PostgreSQL 的自增序列复制完要对齐到 max(id)，否则之后插入会撞主键。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import Connection, Table, func, inspect, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from failgate.db import Base, Database

BATCH = 500


def _source_columns(conn: Connection, table: str) -> set[str] | None:
    insp = inspect(conn)
    if not insp.has_table(table):
        return None
    return {c["name"] for c in insp.get_columns(table)}


def _serial_pk(table: Table) -> str | None:
    pk = list(table.primary_key.columns)
    if len(pk) == 1 and pk[0].autoincrement in (True, "auto") and pk[0].type.python_type is int:
        return pk[0].name
    return None


async def copy_database(src_url: str, dst_url: str,
                        echo: Callable[[str], None] = print) -> dict[str, int]:
    src = create_async_engine(src_url)
    dst = Database(dst_url)
    counts: dict[str, int] = {}
    try:
        await dst.create_all()
        tables = Base.metadata.sorted_tables
        async with dst.engine.connect() as dc:
            for table in tables:
                n = await dc.scalar(select(func.count()).select_from(table))
                if n:
                    raise RuntimeError(f"目标库的 {table.name} 不是空的（{n} 行）：只能复制到空库")
        async with src.connect() as sc, dst.engine.begin() as dc:
            for table in tables:
                have = await sc.run_sync(_source_columns, table.name)
                if have is None:
                    echo(f"{table.name}: 源库没有这张表，跳过")
                    continue
                cols = [c for c in table.columns if c.name in have]
                rows: list[dict[str, Any]] = [
                    dict(r) for r in (await sc.execute(select(*cols))).mappings()
                ]
                for i in range(0, len(rows), BATCH):
                    await dc.execute(table.insert(), rows[i:i + BATCH])
                counts[table.name] = len(rows)
                echo(f"{table.name}: {len(rows)} 行")
            if dc.dialect.name == "postgresql":
                for table in tables:
                    pk = _serial_pk(table)
                    if pk is None:
                        continue
                    await dc.execute(text(
                        f"SELECT setval(pg_get_serial_sequence('{table.name}', '{pk}'), "
                        f"COALESCE(MAX({pk}), 1), MAX({pk}) IS NOT NULL) FROM {table.name}"
                    ))
    finally:
        await src.dispose()
        await dst.dispose()
    return counts
