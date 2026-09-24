from __future__ import annotations

import asyncio
import logging

import typer
import uvicorn
from sqlalchemy import select

from warden.db import Case, Database, Repo
from warden.settings import Settings

app = typer.Typer(help="RepoWarden：证据驱动的开源仓库值班 Agent", no_args_is_help=True)


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8080, reload: bool = False) -> None:
    """启动 API 服务（M0 中 worker 在同一进程内运行）。"""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    uvicorn.run("warden.app:create_app", factory=True, host=host, port=port, reload=reload)


@app.command("db-init")
def db_init() -> None:
    """创建数据库表。"""

    async def run() -> None:
        db = Database(Settings().warden_db_url)
        await db.create_all()
        await db.dispose()

    asyncio.run(run())
    typer.echo("数据库已初始化")


@app.command()
def cases(limit: int = 20) -> None:
    """列出最近的 Case。"""

    async def run() -> list[tuple[Case, Repo]]:
        db = Database(Settings().warden_db_url)
        async with db.session() as s:
            q = select(Case, Repo).join(Repo, Case.repo_id == Repo.id)
            rows = (await s.execute(q.order_by(Case.id.desc()).limit(limit))).all()
        await db.dispose()
        return [(c, r) for c, r in rows]

    for c, r in asyncio.run(run()):
        typer.echo(f"#{c.id:<5} {r.full_name}#{c.number:<6} {c.kind:<6} {c.state}")


if __name__ == "__main__":
    app()
