from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Annotated

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


@app.command("try")
def try_issue(
    title: Annotated[str, typer.Option(help="issue 标题")],
    body: Annotated[str, typer.Option(help="issue 正文")] = "",
    body_file: Annotated[Path | None, typer.Option(help="从文件读取正文（UTF-8）")] = None,
) -> None:
    """不经过 GitHub，直接对一段 issue 文本跑 Intake + Triage，打印结果和花费。"""
    from warden.app import build_llm
    from warden.report import render_summary
    from warden.skills.base import IssueSnapshot, SkillContext
    from warden.skills.intake import IntakeSkill
    from warden.skills.triage import TriageSkill

    settings = Settings()
    llm = build_llm(settings)
    if llm is None:
        raise typer.BadParameter("请先在 .env 中设置 LLM_API_KEY")
    text = body_file.read_text(encoding="utf-8") if body_file else body

    async def run() -> None:
        issue = IssueSnapshot(repo="local/try", number=0, title=title, body=text)
        ctx = SkillContext(issue=issue, llm=llm, model=settings.llm_model_small)
        total = 0.0
        try:
            for skill in (IntakeSkill(), TriageSkill()):
                result = await skill.run(ctx)
                total += result.cost_usd
                ctx.prior[skill.name] = result.output.model_dump(mode="json")
                u = result.usage
                typer.echo(
                    f"\n== {skill.name} ({result.model}) · tokens in={u.prompt_tokens} "
                    f"cached={u.cached_tokens} out={u.completion_tokens} · ${result.cost_usd:.6f}"
                )
                typer.echo(json.dumps(ctx.prior[skill.name], ensure_ascii=False, indent=2))
        finally:
            await llm.aclose()
        typer.echo("\n== 汇总评论预览\n")
        typer.echo(render_summary(ctx.prior["intake"], ctx.prior["triage"], total))

    asyncio.run(run())


if __name__ == "__main__":
    app()
