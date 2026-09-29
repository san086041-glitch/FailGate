"""Alembic 迁移（ADR 0024）：迁移脚本和 db.py 的模型必须一致，升级、降级都能跑。

改了模型却忘了写迁移，`test_migrations_match_models` 就会列出差异。
SQLite 每次都跑；PostgreSQL 连得上才跑（CI 里有服务容器）。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from failgate.db import Base, Database
from failgate.migrations import current, downgrade, head, upgrade_to_head

PG_URL = os.environ.get(
    "TEST_POSTGRES_URL", "postgresql+asyncpg://failgate:failgate@127.0.0.1:5432/failgate")


async def _pg_or_skip() -> str:
    engine = create_async_engine(PG_URL)
    try:
        async with asyncio.timeout(5), engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
    except Exception:
        pytest.skip(f"PostgreSQL not reachable at {PG_URL}")
    finally:
        await engine.dispose()
    return PG_URL


def _diff(conn: Connection) -> list[Any]:
    ctx = MigrationContext.configure(conn, opts={"compare_type": True})
    return compare_metadata(ctx, Base.metadata)


def _tables(conn: Connection) -> set[str]:
    return set(inspect(conn).get_table_names())


async def _check_roundtrip(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(upgrade_to_head)
        async with engine.connect() as conn:
            assert await conn.run_sync(current) == head()
            assert await conn.run_sync(_diff) == []
        async with engine.begin() as conn:
            await conn.run_sync(downgrade, "base")
        async with engine.connect() as conn:
            assert await conn.run_sync(_tables) <= {"alembic_version"}
    finally:
        await engine.dispose()


async def test_migrations_match_models_on_sqlite(tmp_path):
    await _check_roundtrip(f"sqlite+aiosqlite:///{(tmp_path / 'm.db').as_posix()}")


@pytest.mark.postgres
async def test_migrations_match_models_on_postgres():
    await _check_roundtrip(await _pg_or_skip())


@pytest.mark.postgres
async def test_concurrent_startups_upgrade_once():
    # serve 和几个 worker 同时启动：都会调 create_all，advisory lock 让它们排队，不会撞车
    url = await _pg_or_skip()
    dbs = [Database(url) for _ in range(3)]
    try:
        await asyncio.gather(*(db.create_all() for db in dbs))
        async with dbs[0].engine.connect() as conn:
            assert await conn.run_sync(current) == head()
    finally:
        for db in dbs:
            await db.dispose()


def test_sqlite_databases_keep_using_create_all(tmp_path):
    # 开发、测试、回放库照旧：不经过 Alembic，也就没有 alembic_version 表
    async def go() -> set[str]:
        db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'c.db').as_posix()}")
        await db.create_all()
        async with db.engine.connect() as conn:
            names = await conn.run_sync(_tables)
        await db.dispose()
        return names

    names = asyncio.run(go())
    assert "cases" in names and "alembic_version" not in names


async def _seeded_sqlite(tmp_path) -> str:
    from conftest import _harness, issue_event, make_settings

    settings = make_settings(tmp_path)
    if not settings.failgate_db_url.startswith("sqlite"):
        pytest.skip("seed needs the SQLite harness")
    async for h in _harness(settings):
        await h.send("issues", issue_event("opened", 1), "d-1")
        await h.failgate.worker.drain()
    return settings.failgate_db_url


async def _counts(url: str) -> dict[str, int]:
    from sqlalchemy import func, select

    engine = create_async_engine(url)
    out: dict[str, int] = {}
    async with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            out[table.name] = int(await conn.scalar(select(func.count()).select_from(table)) or 0)
    await engine.dispose()
    return out


async def _copy_and_check(src: str, dst: str) -> None:
    from failgate.db_copy import copy_database

    counts = await copy_database(src, dst, echo=lambda _: None)
    assert counts["cases"] == 1 and counts["transitions"] >= 4
    assert await _counts(dst) == await _counts(src)
    # 目标库不是空的：拒绝，不做合并
    with pytest.raises(RuntimeError, match="不是空的"):
        await copy_database(src, dst, echo=lambda _: None)


async def test_copy_sqlite_to_sqlite(tmp_path):
    src = await _seeded_sqlite(tmp_path)
    await _copy_and_check(src, f"sqlite+aiosqlite:///{(tmp_path / 'copy.db').as_posix()}")


@pytest.mark.postgres
async def test_copy_sqlite_to_postgres_and_insert_after(tmp_path):
    from sqlalchemy import select

    from failgate.db import Case, Repo

    dst = await _pg_or_skip()
    src = await _seeded_sqlite(tmp_path)
    await _copy_and_check(src, dst)
    # 序列已对齐到 max(id)：再插入不会撞主键
    db = Database(dst)
    try:
        async with db.session() as s, s.begin():
            repo = (await s.scalars(select(Repo))).one()
            case = Case(repo_id=repo.id, kind="issue", number=2, state="NEW", state_version=0,
                        title="", body="", spent_usd=0.0)
            s.add(case)
            await s.flush()
            assert case.id == 2
    finally:
        await db.dispose()
