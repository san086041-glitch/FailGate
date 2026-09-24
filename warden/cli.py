from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from sqlalchemy import select

from warden.db import Case, Database, IssueDoc, Repo
from warden.settings import Settings

_CJK = re.compile(r"[\u4e00-\u9fff]")

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


index_app = typer.Typer(help="查重索引：回填历史 issue、调试召回", no_args_is_help=True)
app.add_typer(index_app, name="index")


async def _repo_row(db: Database, full_name: str, mode: str) -> Repo:
    async with db.session() as s, s.begin():
        repo = await s.scalar(
            select(Repo).where(Repo.platform == "github", Repo.full_name == full_name)
        )
        if repo is None:
            repo = Repo(platform="github", full_name=full_name, mode=mode)
            s.add(repo)
            await s.flush()
        return repo


@index_app.command("build")
def index_build(
    repo: Annotated[str, typer.Argument(help="owner/name")],
    limit: Annotated[int, typer.Option(help="最多回填多少个 issue")] = 1000,
    db_url: Annotated[str | None, typer.Option("--db", help="数据库连接串，默认用配置")] = None,
) -> None:
    """从 GitHub 回填历史 issue 到查重索引（不含 PR）。"""
    from warden.index.store import IssueIndex
    from warden.platforms.github_rest import GitHubRest

    settings = Settings()

    async def run() -> int:
        db = Database(db_url or settings.warden_db_url)
        await db.create_all()
        repo_row = await _repo_row(db, repo, settings.default_repo_mode)
        index, gh = IssueIndex(db), GitHubRest(settings.github_token)
        n = 0
        try:
            async with db.session() as s, s.begin():
                async for item in gh.iter_issues(repo, limit=limit):
                    await index.upsert(
                        s,
                        repo_row.id,
                        number=item["number"],
                        title=item.get("title") or "",
                        body=item.get("body") or "",
                        state=item.get("state", "open"),
                        state_reason=item.get("state_reason"),
                        labels=[lb["name"] for lb in item.get("labels", [])],
                        url=item.get("html_url"),
                        created_at=datetime.fromisoformat(
                            item["created_at"].replace("Z", "+00:00")
                        ),
                    )
                    n += 1
                    if n % 100 == 0:
                        typer.echo(f"  已回填 {n} 个…")
        finally:
            await gh.aclose()
            await db.dispose()
        return n

    typer.echo(f"回填完成：{repo} 共 {asyncio.run(run())} 个 issue")


@index_app.command("docs")
def index_docs(
    repo: Annotated[str, typer.Argument(help="owner/name")],
    ref: Annotated[str, typer.Option(help="分支、标签或提交；默认是默认分支最新提交")] = "HEAD",
    db_url: Annotated[str | None, typer.Option("--db", help="数据库连接串，默认用配置")] = None,
) -> None:
    """下载仓库源码包，把 README、docs/、CHANGELOG 等切块写进文档索引（整体替换）。"""
    from warden.index.docs import DocIndex, chunk_document, extract_docs
    from warden.platforms.github_rest import GitHubRest

    settings = Settings()

    async def run() -> tuple[str, int, int]:
        db = Database(db_url or settings.warden_db_url)
        await db.create_all()
        repo_row = await _repo_row(db, repo, settings.default_repo_mode)
        gh = GitHubRest(settings.github_token)
        try:
            sha, tarball = await gh.fetch_tarball(repo, ref)
            files = extract_docs(tarball)
            chunks = [c for path, text in files for c in chunk_document(path, text)]
            async with db.session() as s, s.begin():
                await DocIndex(db).replace(s, repo_row.id, repo, sha, chunks)
        finally:
            await gh.aclose()
            await db.dispose()
        return sha, len(files), len(chunks)

    sha, n_files, n_chunks = asyncio.run(run())
    typer.echo(f"文档索引完成：{repo}@{sha[:10]}，{n_files} 个文件，{n_chunks} 个块")


@index_app.command("search")
def index_search(
    repo: Annotated[str, typer.Argument(help="owner/name")],
    title: Annotated[str, typer.Option(help="查询标题")],
    body: Annotated[str, typer.Option(help="查询正文")] = "",
    k: int = 8,
    db_url: Annotated[str | None, typer.Option("--db", help="数据库连接串，默认用配置")] = None,
) -> None:
    """只跑召回（不调用 LLM），打印每个候选在各通道的名次，用于调试查重。"""
    from warden.index.store import IssueIndex
    from warden.index.trace import signature
    from warden.skills.intake import extract_traceback

    settings = Settings()

    async def run() -> None:
        db = Database(db_url or settings.warden_db_url)
        repo_row = await _repo_row(db, repo, settings.default_repo_mode)
        results = await IssueIndex(db).search(
            repo_row.id,
            title=title,
            body=body,
            trace=signature(extract_traceback(body)),
            exclude_number=-1,
            before=None,
            k=k,
        )
        await db.dispose()
        if not results:
            typer.echo("没有召回任何候选（索引为空？先运行 warden index build）")
        for r in results:
            ranks = " ".join(f"{c}#{n}" for c, n in r.ranks.items())
            typer.echo(f"#{r.number:<6} rrf={r.rrf:.4f} [{ranks}] {r.state:<6} {r.title[:70]}")

    asyncio.run(run())


replay_app = typer.Typer(help="回放评测：用仓库历史当标准答案", no_args_is_help=True)
app.add_typer(replay_app, name="replay")

# 回放评测默认使用独立的数据库，不碰线上数据；先用 `warden index build --db ...` 回填语料
REPLAY_DB = "sqlite+aiosqlite:///eval/cache/replay.db"


@replay_app.command("mine")
def replay_mine(repo: Annotated[str, typer.Argument(help="owner/name")]) -> None:
    """从维护者评论（Duplicate of #N）挖掘查重标准答案，写入 eval/datasets/。"""
    from warden.platforms.github_rest import GitHubRest
    from warden.replay.dataset import save_gold
    from warden.replay.mine import mine_gold

    async def run() -> None:
        gh = GitHubRest(Settings().github_token)
        try:
            gold = await mine_gold(gh, repo)
        finally:
            await gh.aclose()
        path = save_gold(gold)
        typer.echo(
            f"搜索命中 {gold.search_hits} 个 issue，"
            f"维护者确认的重复对 {len(gold.pairs)} 个 → {path}"
        )

    asyncio.run(run())


def _run_paths(run_id: str) -> tuple[Path, Path]:
    return Path("eval/runs") / f"{run_id}.json", Path("eval/reports") / f"{run_id}.md"


def _write_report(run_path: Path, min_precision: float) -> None:
    from warden.replay.dataset import load_labels
    from warden.replay.dedup import RunResult
    from warden.replay.metrics import evaluate, recommend, sweep
    from warden.replay.report import render_report

    run = RunResult.model_validate_json(run_path.read_text("utf-8"))
    labels = load_labels(run.config.repo)
    cfg = run.config
    if not any(r.judged for r in run.records):
        hits = " ".join(f"@{k}={h}" for k, h in run.recall.hits.items())
        typer.echo(f"只做了召回评测（{run.recall.usable_pairs} 对）：{hits}")
        return
    current = evaluate(run.records, high=cfg.high, low=cfg.low, gate=True, labels=labels)
    swept = sweep(run.records, labels=labels, low=cfg.low)
    best = recommend(swept, min_precision=min_precision)
    report_path = Path("eval/reports") / f"{run_path.stem}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        render_report(run, current, swept, best, labels, min_precision), "utf-8"
    )
    typer.echo(
        f"当前阈值：精确率 {current.precision:.1%}，召回率 {current.recall:.1%}"
        f"（判对 {current.tp}/{current.n_pos}，对照被标记 {current.neg_flagged}/{current.n_neg}）"
    )
    if best is None:
        typer.echo(f"没有组合达到精确率 ≥ {min_precision:.0%}，请先复核报告里的对照样本")
    else:
        typer.echo(
            f"推荐：high={best.high:.2f}，闸门{'开' if best.gate else '关'} → "
            f"精确率 {best.precision:.1%}，召回率 {best.recall:.1%}"
        )
    typer.echo(f"报告：{report_path}")


@replay_app.command("dedup")
def replay_dedup(
    repo: Annotated[str, typer.Argument(help="owner/name")],
    positives: Annotated[int, typer.Option(help="判断评测的正样本数")] = 100,
    negatives: Annotated[int, typer.Option(help="判断评测的对照样本数")] = 50,
    seed: int = 42,
    prompt_version: Annotated[str, typer.Option(help="查重提示词版本")] = "2",
    judge: Annotated[bool, typer.Option(help="关闭则只做召回评测（不花钱）")] = True,
    min_precision: Annotated[float, typer.Option(help="推荐阈值时的精确率目标")] = 0.9,
    db_url: Annotated[str, typer.Option("--db", help="语料数据库")] = REPLAY_DB,
) -> None:
    """查重回放评测：召回（全部配对）+ 判断（抽样，调用模型）+ 阈值扫描，输出报告。"""
    from warden.app import build_llm
    from warden.replay.dataset import load_gold, repo_slug
    from warden.replay.dedup import RunConfig, run_dedup_replay

    settings = Settings()
    llm = build_llm(settings) if judge else None
    if judge and llm is None:
        raise typer.BadParameter("判断评测需要 LLM_API_KEY；只做召回评测请加 --no-judge")
    cfg = RunConfig(
        repo=repo, positives=positives, negatives=negatives, seed=seed,
        prompt_version=prompt_version, model=settings.llm_model_small, judge=judge,
        high=settings.dedup_high, low=settings.dedup_low, recall_k=settings.dedup_recall_k,
    )

    async def run() -> Path:
        db = Database(db_url)
        try:
            result = await run_dedup_replay(
                db, llm, load_gold(repo), cfg, cache_root=Path("eval/cache/skills"),
                progress=typer.echo,
            )
        finally:
            if llm is not None:
                await llm.aclose()
            await db.dispose()
        run_id = (
            f"{repo_slug(repo)}__dedup__{result.started_at:%Y%m%d-%H%M}"
            f"__v{prompt_version}{'' if judge else '__recall'}"
        )
        run_path, _ = _run_paths(run_id)
        run_path.parent.mkdir(parents=True, exist_ok=True)
        run_path.write_text(result.model_dump_json(indent=1), "utf-8")
        typer.echo(
            f"完成：模型调用 {result.model_calls} 次（缓存 {result.cached_calls}），"
            f"花费 ${result.cost_usd:.4f} → {run_path}"
        )
        return run_path

    _write_report(asyncio.run(run()), min_precision)


@replay_app.command("sweep")
def replay_sweep(
    run_file: Annotated[Path, typer.Argument(help="eval/runs/ 下的评测记录")],
    min_precision: float = 0.9,
) -> None:
    """复核标注更新后，离线重算指标与阈值扫描（不调用模型），重写报告。"""
    _write_report(run_file, min_precision)


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


@app.command("answer")
def answer_issue(
    repo: Annotated[str, typer.Argument(help="owner/name")],
    number: Annotated[int, typer.Argument(help="issue 编号（必须已在索引里）")],
    db_url: Annotated[str | None, typer.Option("--db", help="数据库连接串，默认用配置")] = None,
    show_sources: Annotated[bool, typer.Option(help="打印检索到的资料")] = False,
) -> None:
    """对索引里的某个 issue 试跑 Answer（只用它创建之前的 issue 和评论），打印答案、引用核对和花费。

    文档用的是索引时的版本，不做时间旅行：回放历史问题时文档可能比提问时更新。
    """
    from warden.app import build_llm
    from warden.index.docs import DocIndex
    from warden.index.store import IssueIndex
    from warden.platforms.base import Comment
    from warden.platforms.github_app import comment_from_api
    from warden.platforms.github_rest import GitHubRest
    from warden.report import answer_section
    from warden.skills.answer import AnswerSkill
    from warden.skills.base import IssueSnapshot, SkillContext

    settings = Settings()
    llm = build_llm(settings)
    if llm is None:
        raise typer.BadParameter("请先在 .env 中设置 LLM_API_KEY")

    async def run() -> None:
        db = Database(db_url or settings.warden_db_url)
        gh = GitHubRest(settings.github_token)
        try:
            async with db.session() as s:
                repo_row = await s.scalar(select(Repo).where(Repo.full_name == repo))
                if repo_row is None:
                    raise typer.BadParameter(f"{repo} 不在数据库里，先运行 warden index build")
                doc = await s.scalar(
                    select(IssueDoc).where(
                        IssueDoc.repo_id == repo_row.id, IssueDoc.number == number
                    )
                )
                if doc is None:
                    raise typer.BadParameter(f"#{number} 不在索引里")

            async def comments(full_name: str, n: int) -> list[Comment]:
                return [comment_from_api(c) for c in await gh.list_comments(full_name, n)]

            lang = "zh" if _CJK.search(doc.title + doc.body) else "en"
            ctx = SkillContext(
                issue=IssueSnapshot(
                    repo=repo, number=number, title=doc.title, body=doc.body,
                    repo_id=repo_row.id, created_at=doc.created_at,
                ),
                llm=llm,
                model=settings.llm_model_large,
                prior={"intake": {"language": lang}},
                retriever=IssueIndex(db),
                docs=DocIndex(db),
                comments=comments,
            )
            skill = AnswerSkill()
            if show_sources:
                for src in await skill.gather(ctx):
                    typer.echo(f"\n--- {src.id} [{src.kind}] {src.title}\n{src.url}")
                    typer.echo(src.text[:300])
            result = await skill.run(ctx)
            out = result.output.model_dump(mode="json")
            typer.echo(
                f"\n== answer ({result.model}) · ${result.cost_usd:.6f} · "
                f"status={out['status']} {out['reject_reason']} · confidence={out['confidence']}"
            )
            for c in out["citations"]:
                typer.echo(
                    f"  引用 {c['id']}: 来源存在={c['known_source']} "
                    f"原文找到={c['quote_found']} · {c['quote'][:80]!r}"
                )
            if out["removed_links"] or out["dropped_markers"]:
                typer.echo(
                    f"  删掉的链接 {out['removed_links']} 个；删掉的编号 {out['dropped_markers']}"
                )
            typer.echo("\n== 汇总评论里的回答部分\n")
            typer.echo(answer_section(out, ctx.prior["intake"]["language"]))
        finally:
            await llm.aclose()
            await gh.aclose()
            await db.dispose()

    asyncio.run(run())


repo_app = typer.Typer(help="仓库管理：查看、切换模式", no_args_is_help=True)
app.add_typer(repo_app, name="repo")


@repo_app.command("list")
def repo_list() -> None:
    """列出已登记的仓库、模式和 GitHub App 安装 ID。"""

    async def run() -> list[Repo]:
        db = Database(Settings().warden_db_url)
        await db.create_all()
        async with db.session() as s:
            rows = (await s.scalars(select(Repo).order_by(Repo.id))).all()
        await db.dispose()
        return list(rows)

    for r in asyncio.run(run()):
        typer.echo(f"{r.platform}:{r.full_name:<40} {r.mode:<7} installation={r.installation_id}")


@repo_app.command("mode")
def repo_mode(
    repo: Annotated[str, typer.Argument(help="owner/name")],
    mode: Annotated[str, typer.Argument(help="shadow | live | paused")],
) -> None:
    """切换仓库模式。live 之后新提出的写操作会真正发到 GitHub；已记录的影子动作不会补发。"""
    if mode not in {"shadow", "live", "paused"}:
        raise typer.BadParameter("mode 只能是 shadow / live / paused")

    async def run() -> str:
        db = Database(Settings().warden_db_url)
        await db.create_all()
        async with db.session() as s, s.begin():
            r = await s.scalar(
                select(Repo).where(Repo.platform == "github", Repo.full_name == repo)
            )
            if r is None:
                # 还没收到过这个仓库的事件：先登记，安装 ID 等第一个 webhook 带过来
                s.add(Repo(platform="github", full_name=repo, mode=mode))
                old = "（新登记）"
            else:
                old, r.mode = r.mode, mode
        await db.dispose()
        return old

    old = asyncio.run(run())
    typer.echo(f"{repo}: {old} → {mode}")


github_app_cli = typer.Typer(help="GitHub App：检查配置和安装情况", no_args_is_help=True)
app.add_typer(github_app_cli, name="github")


@github_app_cli.command("check")
def github_check() -> None:
    """用 .env 里的 App ID 和私钥签 JWT，确认 App 身份，并列出安装到了哪些账号。"""
    from warden.app import build_github_app

    settings = Settings()
    gh = build_github_app(settings)
    if gh is None:
        raise typer.BadParameter("请先在 .env 中设置 GITHUB_APP_ID 和 GITHUB_APP_PRIVATE_KEY_PATH")

    async def run() -> None:
        try:
            info = await gh.get_app()
            typer.echo(f"App: {info['name']} (slug={info['slug']}, id={info['id']})")
            perms = ", ".join(f"{k}:{v}" for k, v in sorted(info.get("permissions", {}).items()))
            typer.echo(f"权限: {perms}")
            typer.echo(f"订阅事件: {', '.join(info.get('events', []))}")
            installs = await gh.list_installations()
            if not installs:
                typer.echo("还没有安装到任何账号或仓库")
            for inst in installs:
                token = await gh.installation_token(inst["id"])
                typer.echo(
                    f"安装 {inst['id']}: {inst['account']['login']} "
                    f"(仓库范围={inst.get('repository_selection')}, 令牌获取成功={bool(token)})"
                )
        finally:
            await gh.aclose()

    asyncio.run(run())


effects_app = typer.Typer(help="对外写操作：查看、补发", no_args_is_help=True)
app.add_typer(effects_app, name="effects")


@effects_app.command("flush")
def effects_flush() -> None:
    """立即执行所有 pending 的写操作（服务运行时每分钟也会自动补偿一次）。"""
    from warden.app import build_github_app
    from warden.platforms.base import PlatformWriter
    from warden.policy.executor import EffectExecutor

    settings = Settings()
    gh = build_github_app(settings)

    def writer_for(repo: Repo) -> PlatformWriter | None:
        if gh is None or repo.platform != "github" or not repo.installation_id:
            return None
        return gh.installation(repo.installation_id)

    async def run() -> dict[str, int]:
        db = Database(settings.warden_db_url)
        await db.create_all()
        try:
            return await EffectExecutor(db, writer_for).flush_all()
        finally:
            await db.dispose()
            if gh is not None:
                await gh.aclose()

    typer.echo(json.dumps(asyncio.run(run()), ensure_ascii=False))


if __name__ == "__main__":
    app()
