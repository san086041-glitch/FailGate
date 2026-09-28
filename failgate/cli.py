from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
import uvicorn
from sqlalchemy import select

from failgate.db import Case, Database, IssueDoc, Repo
from failgate.repro.sandbox import DockerSandbox, SandboxLimits
from failgate.settings import Settings

if TYPE_CHECKING:
    from failgate.repro.config import PackageConfig
    from failgate.repro.envcache import EnvCache
    from failgate.repro.issue import IssueReproReport, L2IssueReport
    from failgate.repro.package import PackageRepro

_CJK = re.compile(r"[\u4e00-\u9fff]")

app = typer.Typer(help="FailGate：证据驱动的开源仓库值班 Agent", no_args_is_help=True)


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8080, reload: bool = False) -> None:
    """启动 API 服务（M0 中 worker 在同一进程内运行）。"""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    uvicorn.run("failgate.app:create_app", factory=True, host=host, port=port, reload=reload)


@app.command("db-init")
def db_init() -> None:
    """创建数据库表。"""

    async def run() -> None:
        db = Database(Settings().failgate_db_url)
        await db.create_all()
        await db.dispose()

    asyncio.run(run())
    typer.echo("数据库已初始化")


@app.command()
def cases(limit: int = 20) -> None:
    """列出最近的 Case。"""

    async def run() -> list[tuple[Case, Repo]]:
        db = Database(Settings().failgate_db_url)
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
    from failgate.index.store import IssueIndex
    from failgate.platforms.github_rest import GitHubRest

    settings = Settings()

    async def run() -> int:
        db = Database(db_url or settings.failgate_db_url)
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
    from failgate.index.docs import DocIndex, chunk_document, extract_docs
    from failgate.platforms.github_rest import GitHubRest

    settings = Settings()

    async def run() -> tuple[str, int, int]:
        db = Database(db_url or settings.failgate_db_url)
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
    from failgate.index.store import IssueIndex
    from failgate.index.trace import signature
    from failgate.skills.intake import extract_traceback

    settings = Settings()

    async def run() -> None:
        db = Database(db_url or settings.failgate_db_url)
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
            typer.echo("没有召回任何候选（索引为空？先运行 failgate index build）")
        for r in results:
            ranks = " ".join(f"{c}#{n}" for c, n in r.ranks.items())
            typer.echo(f"#{r.number:<6} rrf={r.rrf:.4f} [{ranks}] {r.state:<6} {r.title[:70]}")

    asyncio.run(run())


replay_app = typer.Typer(help="回放评测：用仓库历史当标准答案", no_args_is_help=True)
app.add_typer(replay_app, name="replay")

# 回放评测默认使用独立的数据库，不碰线上数据；先用 `failgate index build --db ...` 回填语料
REPLAY_DB = "sqlite+aiosqlite:///eval/cache/replay.db"


@replay_app.command("mine")
def replay_mine(repo: Annotated[str, typer.Argument(help="owner/name")]) -> None:
    """从维护者评论（Duplicate of #N）挖掘查重标准答案，写入 eval/datasets/。"""
    from failgate.platforms.github_rest import GitHubRest
    from failgate.replay.dataset import save_gold
    from failgate.replay.mine import mine_gold

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
    from failgate.replay.dataset import load_labels
    from failgate.replay.dedup import RunResult
    from failgate.replay.metrics import evaluate, recommend, sweep
    from failgate.replay.report import render_report

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
    semantic: Annotated[bool, typer.Option(help="启用向量召回通道（需要 EMBED_*）")] = False,
    recall_k: Annotated[int | None, typer.Option(help="交给模型判断的候选数，默认用配置")] = None,
    min_precision: Annotated[float, typer.Option(help="推荐阈值时的精确率目标")] = 0.9,
    db_url: Annotated[str, typer.Option("--db", help="语料数据库")] = REPLAY_DB,
) -> None:
    """查重回放评测：召回（全部配对）+ 判断（抽样，调用模型）+ 阈值扫描，输出报告。"""
    from failgate.app import build_embedder, build_llm
    from failgate.replay.dataset import load_gold, repo_slug
    from failgate.replay.dedup import RunConfig, run_dedup_replay

    settings = Settings()
    llm = build_llm(settings) if judge else None
    if judge and llm is None:
        raise typer.BadParameter("判断评测需要 LLM_API_KEY；只做召回评测请加 --no-judge")
    cfg = RunConfig(
        repo=repo, positives=positives, negatives=negatives, seed=seed,
        prompt_version=prompt_version, model=settings.llm_model_small, judge=judge,
        semantic=semantic,
        high=settings.dedup_high, low=settings.dedup_low,
        recall_k=recall_k or settings.dedup_recall_k,
    )

    embedder = build_embedder(settings) if semantic else None
    if semantic and embedder is None:
        raise typer.BadParameter("--semantic 需要在 .env 中配置 EMBED_BASE_URL / EMBED_MODEL")

    async def run() -> Path:
        db = Database(db_url)
        try:
            result = await run_dedup_replay(
                db, llm, load_gold(repo), cfg, cache_root=Path("eval/cache/skills"),
                progress=typer.echo, embedder=embedder,
            )
        finally:
            if llm is not None:
                await llm.aclose()
            if embedder is not None:
                await embedder.aclose()
            await db.dispose()
        run_id = (
            f"{repo_slug(repo)}__dedup__{result.started_at:%Y%m%d-%H%M}"
            f"__v{prompt_version}{'__sem' if semantic else ''}{'' if judge else '__recall'}"
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


@replay_app.command("triage")
def replay_triage(
    repo: Annotated[str, typer.Argument(help="owner/name")],
    per_class: Annotated[int, typer.Option(help="每个类型标签最多抽多少个")] = 40,
    seed: int = 42,
    prompt_version: Annotated[str, typer.Option(help="分诊提示词版本")] = "1",
    refresh_labels: Annotated[bool, typer.Option(help="重新从 GitHub 拉取标签表快照")] = False,
    db_url: Annotated[str, typer.Option("--db", help="语料数据库")] = REPLAY_DB,
) -> None:
    """分诊回放评测：以维护者的类型标签为标准答案，分层抽样，报告准确率与混淆矩阵。"""
    from failgate.app import build_llm
    from failgate.platforms.github_rest import GitHubRest
    from failgate.replay.dataset import EVAL_ROOT, repo_slug
    from failgate.replay.triage import (
        RepoLabel,
        TriageRunConfig,
        load_repo_labels,
        render_triage_report,
        run_triage_replay,
        save_repo_labels,
    )

    settings = Settings()
    llm = build_llm(settings)
    if llm is None:
        raise typer.BadParameter("需要 LLM_API_KEY")
    cfg = TriageRunConfig(
        repo=repo, per_class=per_class, seed=seed, prompt_version=prompt_version,
        model=settings.llm_model_small,
    )

    async def run() -> Path:
        labels = None if refresh_labels else load_repo_labels(repo, EVAL_ROOT)
        if labels is None:
            gh = GitHubRest(settings.github_token)
            try:
                raw = await gh.list_labels(repo)
            finally:
                await gh.aclose()
            labels = [RepoLabel(name=x["name"], description=x.get("description") or "")
                      for x in raw]
            saved = save_repo_labels(repo, labels, EVAL_ROOT)
            typer.echo(f"标签表快照：{len(labels)} 个 → {saved}")
        db = Database(db_url)
        try:
            result = await run_triage_replay(
                db, llm, cfg, labels, cache_root=Path("eval/cache/skills"), progress=typer.echo
            )
        finally:
            await llm.aclose()
            await db.dispose()
        run_id = f"{repo_slug(repo)}__triage__{result.started_at:%Y%m%d-%H%M}__v{prompt_version}"
        run_path, report_path = _run_paths(run_id)
        run_path.parent.mkdir(parents=True, exist_ok=True)
        run_path.write_text(result.model_dump_json(indent=1), "utf-8")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_triage_report(result), "utf-8")
        typer.echo(
            f"完成：模型调用 {result.model_calls} 次（缓存 {result.cached_calls}），"
            f"花费 ${result.cost_usd:.4f} → {run_path}"
        )
        return report_path

    typer.echo(f"报告：{asyncio.run(run())}")


@replay_app.command("triage-report")
def replay_triage_report(
    run_file: Annotated[Path, typer.Argument(help="eval/runs/ 下的分诊评测记录")],
) -> None:
    """不调用模型，按评测记录重新生成分诊报告。"""
    from failgate.replay.triage import TriageRunResult, render_triage_report

    run = TriageRunResult.model_validate_json(run_file.read_text("utf-8"))
    out = Path("eval/reports") / f"{run_file.stem}.md"
    out.write_text(render_triage_report(run), "utf-8")
    typer.echo(f"报告：{out}")


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
    from failgate.app import build_llm
    from failgate.report import render_summary
    from failgate.skills.base import IssueSnapshot, SkillContext
    from failgate.skills.intake import IntakeSkill
    from failgate.skills.triage import TriageSkill

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
    from failgate.app import build_embedder, build_llm
    from failgate.index.docs import DocIndex
    from failgate.index.store import IssueIndex
    from failgate.platforms.base import Comment
    from failgate.platforms.github_app import comment_from_api
    from failgate.platforms.github_rest import GitHubRest
    from failgate.report import answer_section
    from failgate.skills.answer import AnswerSkill
    from failgate.skills.base import IssueSnapshot, SkillContext

    settings = Settings()
    llm = build_llm(settings)
    if llm is None:
        raise typer.BadParameter("请先在 .env 中设置 LLM_API_KEY")

    embedder = build_embedder(settings)

    async def run() -> None:
        db = Database(db_url or settings.failgate_db_url)
        gh = GitHubRest(settings.github_token)
        try:
            async with db.session() as s:
                repo_row = await s.scalar(select(Repo).where(Repo.full_name == repo))
                if repo_row is None:
                    raise typer.BadParameter(f"{repo} 不在数据库里，先运行 failgate index build")
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
                retriever=IssueIndex(db, embedder),
                docs=DocIndex(db, embedder),
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
            if embedder is not None:
                await embedder.aclose()
            await db.dispose()

    asyncio.run(run())


repo_app = typer.Typer(help="仓库管理：查看、切换模式", no_args_is_help=True)
app.add_typer(repo_app, name="repo")


@repo_app.command("list")
def repo_list() -> None:
    """列出已登记的仓库、模式和 GitHub App 安装 ID。"""

    async def run() -> list[Repo]:
        db = Database(Settings().failgate_db_url)
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
        db = Database(Settings().failgate_db_url)
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


@repo_app.command("labels")
def repo_labels(
    repo: Annotated[str, typer.Argument(help="owner/name")],
    allow: Annotated[
        list[str] | None, typer.Option("--allow", help="白名单模式（可多次），如 'T: *'")
    ] = None,
    clear: Annotated[bool, typer.Option(help="清空白名单（只拦结论 / 进度类标签）")] = False,
) -> None:
    """查看或设置自动打标签的白名单，并列出仓库每个标签能不能被自动打上。"""
    from failgate.platforms.github_rest import GitHubRest
    from failgate.policy.labels import filter_auto_labels

    settings = Settings()

    async def run() -> tuple[list[str] | None, list[str]]:
        db = Database(settings.failgate_db_url)
        await db.create_all()
        async with db.session() as s, s.begin():
            r = await s.scalar(
                select(Repo).where(Repo.platform == "github", Repo.full_name == repo)
            )
            if r is None:
                r = Repo(platform="github", full_name=repo, mode=settings.default_repo_mode)
                s.add(r)
            if clear:
                r.auto_labels = None
            elif allow:
                r.auto_labels = list(allow)
            patterns = r.auto_labels
        await db.dispose()
        gh = GitHubRest(settings.github_token)
        try:
            names = [x["name"] for x in await gh.list_labels(repo)]
        finally:
            await gh.aclose()
        return patterns, names

    patterns, names = asyncio.run(run())
    typer.echo(f"白名单：{patterns if patterns else '未设置（只拦结论 / 进度类标签）'}")
    kept, blocked = filter_auto_labels(names, patterns)
    typer.echo(f"可以自动打（{len(kept)}）：{', '.join(kept)}")
    for name, why in blocked.items():
        typer.echo(f"  不会自动打：{name}  ← {why}")


@repo_app.command("repro")
def repo_repro(
    repo: Annotated[str, typer.Argument(help="owner/name")],
    package: Annotated[str | None, typer.Option(help="PyPI 包名，如 black")] = None,
    import_name: Annotated[str | None, typer.Option(help="import 名，默认由包名推出")] = None,
    source: Annotated[
        str | None,
        typer.Option(help="源码仓库 owner/name：报告未发布版本时走 source 模式（L2）；传 - 关闭"),
    ] = None,
    clear: Annotated[bool, typer.Option(help="关闭这个仓库的复现")] = False,
) -> None:
    """查看或设置仓库的复现配置（package 模式用哪个 PyPI 包、source 模式用哪个源码仓库）。
    还需要 REPRO_ENABLED=true。"""
    from failgate.repro.config import PackageConfig
    from failgate.repro.pypi import PackageNotFound, PyPIClient, PyPIError

    settings = Settings()
    unpublished = False
    if package:
        try:
            PackageConfig(name=package, import_name=import_name)
        except ValueError as e:
            raise typer.BadParameter(str(e)) from e
    if source not in (None, "-") and not re.fullmatch(r"[\w.-]+/[\w.-]+", source or ""):
        raise typer.BadParameter(f"源码仓库要写成 owner/name：{source!r}")

    async def run() -> tuple[str | None, str | None, str | None]:
        nonlocal unpublished
        if package:
            pypi = PyPIClient(settings.pypi_url)
            try:
                await pypi.releases(package)
            except PackageNotFound as e:
                # 没发布到 PyPI 的项目（应用、内部库）只能从源码复现，必须同时给源码仓库
                if source in (None, "-"):
                    raise typer.BadParameter(
                        f"{e}；没发布的项目要同时用 --source 指定源码仓库"
                    ) from e
                unpublished = True
            except PyPIError as e:
                raise typer.BadParameter(str(e)) from e
            finally:
                await pypi.aclose()
        db = Database(settings.failgate_db_url)
        await db.create_all()
        async with db.session() as s, s.begin():
            r = await s.scalar(
                select(Repo).where(Repo.platform == "github", Repo.full_name == repo)
            )
            if r is None:
                r = Repo(platform="github", full_name=repo, mode=settings.default_repo_mode)
                s.add(r)
            if clear:
                r.repro_package, r.repro_import_name, r.repro_source = None, None, None
            elif package:
                r.repro_package, r.repro_import_name = package, import_name
            if not clear and source is not None:
                r.repro_source = None if source == "-" else source
            current = r.repro_package, r.repro_import_name, r.repro_source
        await db.dispose()
        return current

    pkg, imp, src = asyncio.run(run())
    if pkg is None:
        typer.echo(f"{repo}：不做复现")
    elif unpublished:
        typer.echo(f"{repo}：包 {pkg}（import 名 {imp or '由包名推出'}）没发布到 PyPI，"
                   f"所有 bug 都走 source 模式（L2），源码仓库 {src}")
    else:
        typer.echo(f"{repo}：package 模式，包 {pkg}（import 名 {imp or '由包名推出'}）")
        if src:
            typer.echo(f"  报告未发布版本时走 source 模式（L2），源码仓库 {src}")
    if not settings.repro_enabled:
        typer.echo("注意：总开关 REPRO_ENABLED 没有打开，流水线不会进入复现阶段")


github_app_cli = typer.Typer(help="GitHub App：检查配置和安装情况", no_args_is_help=True)
app.add_typer(github_app_cli, name="github")


@github_app_cli.command("check")
def github_check() -> None:
    """用 .env 里的 App ID 和私钥签 JWT，确认 App 身份，并列出安装到了哪些账号。"""
    from failgate.app import build_github_app

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
    from failgate.app import build_github_app
    from failgate.platforms.base import PlatformWriter
    from failgate.policy.executor import EffectExecutor

    settings = Settings()
    gh = build_github_app(settings)

    def writer_for(repo: Repo) -> PlatformWriter | None:
        if gh is None or repo.platform != "github" or not repo.installation_id:
            return None
        return gh.installation(repo.installation_id)

    async def run() -> dict[str, int]:
        db = Database(settings.failgate_db_url)
        await db.create_all()
        try:
            return await EffectExecutor(db, writer_for).flush_all()
        finally:
            await db.dispose()
            if gh is not None:
                await gh.aclose()

    typer.echo(json.dumps(asyncio.run(run()), ensure_ascii=False))


evidence_app = typer.Typer(help="证据与考卷：查看封存的收据、核对哈希", no_args_is_help=True)
app.add_typer(evidence_app, name="evidence")


@evidence_app.command("list")
def evidence_list(
    repo: Annotated[str | None, typer.Argument(help="owner/name；不填列出全部")] = None,
    db_url: Annotated[str | None, typer.Option("--db", help="数据库 URL，默认读配置")] = None,
) -> None:
    """列出封存的证据：短 ID、issue、证据等级、能否当考卷、判定、测试文件。"""
    from failgate.verify.store import list_evidence

    async def run() -> None:
        db = Database(db_url or Settings().failgate_db_url)
        await db.create_all()
        try:
            async with db.session() as s:
                refs = await list_evidence(s, repo)
        finally:
            await db.dispose()
        if not refs:
            typer.echo("没有证据")
        for ref in refs:
            ev = ref.evidence
            exam = "考卷" if ev.acceptance else "非考卷"
            old = f"（已被 {ev.superseded_by[:8]} 取代）" if ev.superseded_by else ""
            typer.echo(
                f"{ev.id[:12]}  {ref.repo}#{ref.issue:<6} {ev.level} {exam:<3} {ev.verdict:<10} "
                f"{ev.test_path}  {ev.created_at:%Y-%m-%d %H:%M}{old}"
            )

    asyncio.run(run())


@evidence_app.command("show")
def evidence_show(
    evidence_id: Annotated[str, typer.Argument(help="证据 ID 或前缀（至少 6 位）")],
    db_url: Annotated[str | None, typer.Option("--db", help="数据库 URL，默认读配置")] = None,
    out: Annotated[
        Path | None, typer.Option(help="把收据 receipt.json 和测试文件写到这个目录，便于本地复验")
    ] = None,
) -> None:
    """打印收据，并重算哈希核对：收据、考卷代码、表里的列三者一致才算完好。"""
    from failgate.verify.store import audit, find_evidence

    async def run() -> int:
        db = Database(db_url or Settings().failgate_db_url)
        await db.create_all()
        try:
            async with db.session() as s:
                try:
                    ref = await find_evidence(s, evidence_id)
                except ValueError as e:
                    raise typer.BadParameter(str(e)) from e
        finally:
            await db.dispose()
        if ref is None:
            typer.echo(f"找不到证据 {evidence_id}", err=True)
            return 1
        ev = ref.evidence
        typer.echo(json.dumps(ev.receipt, ensure_ascii=False, indent=2, sort_keys=True))
        problems = audit(ref)
        if out is not None:
            out.mkdir(parents=True, exist_ok=True)
            (out / "receipt.json").write_text(
                json.dumps(ev.receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            test_file = out / Path(ev.test_path).name
            test_file.write_bytes(ev.test_code.encode("utf-8"))
            typer.echo(f"已写出 {out / 'receipt.json'} 和 {test_file}")
        if problems:
            for p in problems:
                typer.echo(f"❌ {p}")
            return 1
        typer.echo(f"✅ 哈希一致：receipt {ev.receipt_sha256[:12]} · test {ev.test_sha256[:12]}")
        return 0

    raise typer.Exit(asyncio.run(run()))


@app.command("verify")
def verify_pr(
    target: Annotated[str, typer.Argument(help="owner/name#PR 编号")],
    db_url: Annotated[str | None, typer.Option("--db", help="数据库 URL，默认读配置")] = None,
    out: Annotated[Path | None, typer.Option(help="把核验收据写到这个 JSON 文件")] = None,
    lang: Annotated[str, typer.Option(help="报告语言 zh / en")] = "zh",
) -> None:
    """用封存的考卷核验一个 PR（ClaimVerify 三层）。需要 Docker 和 GITHUB_TOKEN。

    退出码：0 通过验收，1 驳回，2 无法判定或没有声明。"""
    from failgate.platforms.github_rest import GitHubRest
    from failgate.repro.l2 import TestReproducer
    from failgate.repro.pypi import PyPIClient
    from failgate.verify.claims import parse_claims
    from failgate.verify.engine import ClaimVerdict, ClaimVerifier, Exam
    from failgate.verify.report import render_verification
    from failgate.verify.store import latest_exam
    from failgate.verify.workbench import SandboxWorkbench, fetch_pull

    m = re.fullmatch(r"([\w.-]+/[\w.-]+)#(\d+)", target)
    if m is None:
        raise typer.BadParameter("要写成 owner/name#PR编号")
    repo, number = m.group(1), int(m.group(2))
    settings = Settings()

    async def run() -> int:
        gh = GitHubRest(settings.github_token)
        sandbox = DockerSandbox(
            settings.docker_bin,
            limits=SandboxLimits(memory=settings.sandbox_memory, cpus=settings.sandbox_cpus),
            install_network=settings.sandbox_install_network,
            artifacts_dir=Path(settings.sandbox_artifacts_dir),
        )
        pypi = PyPIClient(settings.pypi_url)
        db = Database(db_url or settings.failgate_db_url)
        await db.create_all()
        try:
            pr = await fetch_pull(gh, repo, number)
            claims = parse_claims(pr.title, pr.body, repo)
            exams: dict[int, Exam | None] = {}
            async with db.session() as s:
                for n in claims:
                    exams[n] = await latest_exam(s, repo, n)
            typer.echo(f"{repo}#{number}：声称修复 {claims or '（无）'}；"
                       f"合并基点 {pr.base_sha[:7]} → head {pr.head_sha[:7]}", err=True)
            tester = TestReproducer(sandbox, _env_cache(settings, sandbox), pypi,
                                    run_timeout_s=settings.sandbox_run_timeout_seconds)
            verifier = ClaimVerifier(SandboxWorkbench.for_github(gh, tester))
            result = await verifier.verify(pr, claims, exams)
        finally:
            await db.dispose()
            await gh.aclose()
            await pypi.aclose()
        typer.echo(render_verification(result, lang))
        if out is not None:
            out.write_text(json.dumps(result.receipt(), ensure_ascii=False, indent=2,
                                      sort_keys=True) + "\n", encoding="utf-8")
        return {ClaimVerdict.VERIFIED: 0, ClaimVerdict.REFUTED: 1}.get(result.verdict, 2)  # type: ignore[arg-type]

    raise typer.Exit(asyncio.run(run()))


sandbox_app = typer.Typer(help="复现沙箱：自检、清理", no_args_is_help=True)
app.add_typer(sandbox_app, name="sandbox")


def build_sandbox(settings: Settings) -> DockerSandbox:
    return DockerSandbox(
        settings.docker_bin,
        limits=SandboxLimits(memory=settings.sandbox_memory, cpus=settings.sandbox_cpus),
        install_network=settings.sandbox_install_network,
        artifacts_dir=Path(settings.sandbox_artifacts_dir),
    )


@sandbox_app.command("check")
def sandbox_check(
    image: Annotated[str | None, typer.Option(help="沙箱镜像，默认用配置")] = None,
) -> None:
    """真的起几个容器，逐项确认隔离参数生效：非 root、无 capabilities、断网、只读、资源上限。"""
    from failgate.repro.selfcheck import self_check

    settings = Settings()
    sandbox = build_sandbox(settings)
    typer.echo(f"docker: {sandbox.docker}")
    items = asyncio.run(self_check(sandbox, image or settings.sandbox_image))
    for it in items:
        typer.echo(f"{'✅' if it.ok else '❌'} {it.name:<12} {it.detail}")
    if not all(it.ok for it in items):
        raise typer.Exit(1)


@sandbox_app.command("prune")
def sandbox_prune(
    hours: Annotated[float, typer.Option(help="删除创建超过多少小时的工作区卷")] = 24.0,
) -> None:
    """按 TTL 清理残留的工作区卷（正常情况下 Case 结束时就会删除）。"""
    removed = asyncio.run(build_sandbox(Settings()).prune_workspaces(hours * 3600))
    typer.echo(f"删除了 {len(removed)} 个工作区卷")


repro_app = typer.Typer(help="复现：在沙箱里跑复现脚本并判定", no_args_is_help=True)
app.add_typer(repro_app, name="repro")


@repro_app.command("package")
def repro_package(
    name: Annotated[str, typer.Argument(help="PyPI 包名")],
    version: Annotated[str, typer.Argument(help="报告的版本（原文即可，如 'black 23.11.0'）")],
    script: Annotated[Path, typer.Option(help="复现脚本（在沙箱里以 python repro.py 运行）")],
    traceback_file: Annotated[
        Path | None, typer.Option(help="issue 里报告的堆栈，用于判定")
    ] = None,
    import_name: Annotated[str | None, typer.Option(help="import 名，默认由包名推出")] = None,
    python: Annotated[str | None, typer.Option(help="用户报告的 Python 版本")] = None,
    latest: Annotated[bool, typer.Option(help="复现后是否在最新正式版上复查")] = True,
) -> None:
    """package 模式：装报告的版本 → 跑脚本 → 判定；复现了再到最新版上看是否已修复。"""
    from failgate.repro.config import PackageConfig
    from failgate.repro.package import PackageReproducer
    from failgate.repro.pypi import PyPIClient

    settings = Settings()
    sandbox = build_sandbox(settings)
    cfg = PackageConfig(name=name, import_name=import_name)
    tb = traceback_file.read_text(encoding="utf-8") if traceback_file else None

    async def run() -> None:
        pypi = PyPIClient(settings.pypi_url)
        try:
            result = await PackageReproducer(
                sandbox, _env_cache(settings, sandbox), pypi,
                run_timeout_s=settings.sandbox_run_timeout_seconds,
            ).reproduce(
                cfg,
                reported_version=version,
                script=script.read_text(encoding="utf-8"),
                reported_traceback=tb,
                env_python=python,
                check_latest=latest,
            )
        finally:
            await pypi.aclose()
        _echo_repro(result)

    asyncio.run(run())


def _env_cache(settings: Settings, sandbox: DockerSandbox) -> EnvCache:
    from failgate.repro.envcache import EnvCache

    return EnvCache(
        sandbox,
        Path(settings.sandbox_artifacts_dir) / "envcache.json",
        max_bytes=int(settings.sandbox_env_cache_gb * 1024**3),
        index_url=settings.pip_index_url,
    )


def _echo_repro(result: PackageRepro) -> None:
    typer.echo(f"证据等级：{result.level}")
    typer.echo(result.summary())
    for label, vr in (("报告版本", result.reported), ("最新版本", result.latest)):
        if vr is not None:
            v = vr.verdict
            hit = "命中" if vr.cache_hit else "新建"
            typer.echo(
                f"  {label} {vr.version} / py{vr.python} / 环境{hit} {vr.env_key[:12]}："
                f"{v.kind} 一致度={v.match}（{v.match_method}） 运行 {v.runs} 次"
            )
            typer.echo(f"    日志={vr.log_dir}")


async def _load_issue_docs(db_url: str, repo: str, numbers: list[int]) -> list[IssueDoc]:
    db = Database(db_url)
    await db.create_all()  # 老的回放库可能缺新加的列
    try:
        async with db.session() as s:
            repo_row = await s.scalar(select(Repo).where(Repo.full_name == repo))
            if repo_row is None:
                return []
            rows = (await s.scalars(select(IssueDoc).where(
                IssueDoc.repo_id == repo_row.id, IssueDoc.number.in_(numbers)
            ))).all()
    finally:
        await db.dispose()
    by_number = {d.number: d for d in rows}
    return [by_number[n] for n in numbers if n in by_number]


class _ReproRuntime:
    """复现需要的一组长生命周期对象：LLM、沙箱、环境缓存、PyPI 客户端。"""

    def __init__(self, settings: Settings) -> None:
        from failgate.app import build_llm
        from failgate.repro.package import PackageReproducer
        from failgate.repro.pypi import PyPIClient

        llm = build_llm(settings)
        if llm is None:
            raise typer.BadParameter("请先在 .env 中设置 LLM_API_KEY")
        self.settings = settings
        self.llm = llm
        sandbox = build_sandbox(settings)
        self.pypi = PyPIClient(settings.pypi_url)
        self.reproducer = PackageReproducer(
            sandbox, _env_cache(settings, sandbox), self.pypi,
            run_timeout_s=settings.sandbox_run_timeout_seconds,
        )

    async def run(
        self, repo: str, doc: IssueDoc, cfg: PackageConfig, *, check_latest: bool = True,
        max_steps: int | None = None,
    ) -> IssueReproReport:
        from failgate.repro.issue import reproduce_issue

        s = self.settings
        return await reproduce_issue(
            repo=repo, number=doc.number, title=doc.title, body=doc.body,
            created_at=doc.created_at, cfg=cfg,
            llm=self.llm, small_model=s.llm_model_small, large_model=s.llm_model_large,
            reproducer=self.reproducer, max_steps=max_steps or s.repro_max_steps,
            max_attempts=s.repro_max_attempts, budget_usd=s.repro_budget_usd,
            artifacts_dir=Path(s.sandbox_artifacts_dir), check_latest=check_latest,
        )

    async def __aenter__(self) -> _ReproRuntime:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.pypi.aclose()
        await self.llm.aclose()


def _repro_runtime(settings: Settings) -> _ReproRuntime:
    return _ReproRuntime(settings)


def _echo_issue_report(report: IssueReproReport) -> None:
    typer.echo(
        f"#{report.number} {report.title[:70]}\n"
        f"Intake：版本={report.intake_version!r} Python={report.intake_python!r} "
        f"堆栈={'有' if report.has_traceback else '无'}"
    )
    ar = report.agent
    if ar is not None:
        typer.echo(
            f"Agent：{ar.status} · {ar.steps} 步 {dict(ar.tool_counts)} · "
            f"提交 {len(ar.attempts)} 次 · {ar.duration_s}s"
        )
        for at in ar.attempts:
            typer.echo(f"  提交 {at.n} {at.name}：{at.kind} 一致度={at.match}")
            typer.echo(f"    声明：{at.claim[:100]}")
        if ar.give_up_reason:
            typer.echo(f"  放弃原因：{ar.give_up_reason}（可疑位置：{ar.suspect}）")
        if ar.error:
            typer.echo(f"  出错：{ar.error}")
        if ar.final_script:
            typer.echo("最终脚本：\n" + ar.final_script)
        typer.echo(f"对话记录：{ar.transcript_path}")
    typer.echo(
        f"花费：${report.total_cost_usd:.4f}（Intake ${report.intake_cost_usd:.4f}，"
        f"Agent ${ar.cost_usd if ar else 0:.4f}，评委 ${report.judge_cost_usd:.4f}）"
    )
    _echo_repro(report.repro)


@repro_app.command("issue")
def repro_issue(
    repo: Annotated[str, typer.Argument(help="owner/name")],
    number: Annotated[int, typer.Argument(help="issue 编号（必须已在索引里）")],
    package: Annotated[str, typer.Option(help="PyPI 包名")],
    import_name: Annotated[str | None, typer.Option(help="import 名，默认由包名推出")] = None,
    db_url: Annotated[str | None, typer.Option("--db", help="数据库连接串，默认用配置")] = None,
    latest: Annotated[bool, typer.Option(help="复现后是否在最新正式版上复查")] = True,
    max_steps: Annotated[int | None, typer.Option(help="工具调用步数上限")] = None,
) -> None:
    """复现 Agent：Intake 抽版本 → 装报告的版本 → Agent 读源码、写脚本、提交判定 → 查最新版。"""
    from failgate.repro.config import PackageConfig

    settings = Settings()
    cfg = PackageConfig(name=package, import_name=import_name)

    async def run() -> None:
        docs = await _load_issue_docs(db_url or settings.failgate_db_url, repo, [number])
        if not docs:
            raise typer.BadParameter(f"{repo}#{number} 不在索引里，先运行 failgate index build")
        async with _repro_runtime(settings) as rt:
            report = await rt.run(repo, docs[0], cfg, check_latest=latest, max_steps=max_steps)
        _echo_issue_report(report)

    asyncio.run(run())


@replay_app.command("repro")
def replay_repro(
    repo: Annotated[str, typer.Argument(help="owner/name")],
    package: Annotated[str, typer.Option(help="PyPI 包名")],
    import_name: Annotated[str | None, typer.Option(help="import 名，默认由包名推出")] = None,
    limit: Annotated[int, typer.Option(help="按选样规则取最新的多少个 issue")] = 12,
    offset: Annotated[
        int, typer.Option(help="跳过最新的多少个（开发时用过的样本），用来取留出集")
    ] = 0,
    since: Annotated[str, typer.Option(help="只取这个日期之后创建的 issue")] = "2022-01-01",
    numbers: Annotated[
        str | None, typer.Option(help="逗号分隔的编号，指定时不按规则选样（调试用）")
    ] = None,
    db_url: Annotated[str, typer.Option("--db", help="回放语料库")] = REPLAY_DB,
) -> None:
    """复现回放：在已修复的历史 bug 上跑复现 Agent，报告 L1 复现率和 FB/PA（代理）。会花钱。"""
    from failgate.replay.repro import dump, render, select_issues
    from failgate.repro.agent import PROMPT_VERSION
    from failgate.repro.config import PackageConfig

    settings = Settings()
    cfg = PackageConfig(name=package, import_name=import_name)
    started = datetime.now()
    run_id = f"{repo.replace('/', '__')}__repro__{started:%Y%m%d-%H%M}"
    run_path, report_path = _run_paths(run_id)
    selection = (
        f"指定编号 {numbers}" if numbers else
        f"{since} 之后创建、以完成状态关闭的 T: bug，类别为 crash / invalid code / "
        f"unstable formatting / parser，排除 duplicate / not a bug / invalid / outdated，"
        f"按编号从新到旧跳过 {offset} 个、取 {limit} 个"
    )
    meta = {
        "started": started.isoformat(timespec="seconds"), "model": settings.llm_model_large,
        "prompt": PROMPT_VERSION, "selection": selection, "max_steps": settings.repro_max_steps,
        "max_attempts": settings.repro_max_attempts, "budget_usd": settings.repro_budget_usd,
    }

    async def run() -> None:
        if numbers:
            wanted = [int(x) for x in numbers.split(",") if x.strip()]
        else:
            db = Database(db_url)
            await db.create_all()
            async with db.session() as s:
                repo_row = await s.scalar(select(Repo).where(Repo.full_name == repo))
                if repo_row is None:
                    raise typer.BadParameter(f"{repo} 不在回放库里")
                all_docs = (await s.scalars(
                    select(IssueDoc).where(IssueDoc.repo_id == repo_row.id)
                )).all()
            await db.dispose()
            wanted = [d.number for d in select_issues(
                all_docs, since=datetime.fromisoformat(since), limit=limit, offset=offset
            )]
        docs = await _load_issue_docs(db_url, repo, wanted)
        typer.echo(f"选中 {len(docs)} 个：{[d.number for d in docs]}")
        reports: list[IssueReproReport] = []
        run_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        async with _repro_runtime(settings) as rt:
            for i, doc in enumerate(docs, 1):
                typer.echo(f"\n===== [{i}/{len(docs)}] #{doc.number} {doc.title[:70]}")
                report = await rt.run(repo, doc, cfg)
                reports.append(report)
                a = report.agent
                typer.echo(
                    f"  → {report.repro.level} · {a.status if a else report.repro.error} · "
                    f"{report.repro.summary()[:160]} · ${report.total_cost_usd:.4f}"
                )
                # 每跑完一个就落盘，中途出错不丢已有结果
                run_path.write_text(dump(reports, meta), encoding="utf-8")
                report_path.write_text(render(repo, reports, meta), encoding="utf-8")
        typer.echo(f"\n报告：{report_path}")

    asyncio.run(run())


@repro_app.command("source")
def repro_source(
    repo: Annotated[str, typer.Argument(help="源码仓库 owner/name")],
    ref: Annotated[str, typer.Argument(help="提交 SHA、分支或 tag")],
    script: Annotated[Path, typer.Option(help="复现脚本（在沙箱里以 python repro.py 运行）")],
    package: Annotated[str, typer.Option(help="PyPI 包名：推算伪版本号、按包名过滤栈帧")],
    import_name: Annotated[str | None, typer.Option(help="import 名，默认由包名推出")] = None,
    python: Annotated[str | None, typer.Option(help="Python 版本，默认按提交日期选")] = None,
    traceback_file: Annotated[
        Path | None, typer.Option(help="issue 里报告的堆栈，用于判定")
    ] = None,
) -> None:
    """source 模式：从某个提交的源码构建环境 → 跑脚本 → 判定（不调用 LLM）。"""
    from failgate.platforms.github_rest import GitHubRest
    from failgate.repro.config import PackageConfig
    from failgate.repro.package import SETUP_ERRORS, PackageReproducer
    from failgate.repro.pypi import PyPIClient
    from failgate.repro.source import (
        SourceError,
        fetch_github_tree,
        pick_python_for_commit,
        pretend_version,
        source_env,
    )

    settings = Settings()
    sandbox = build_sandbox(settings)
    cache = _env_cache(settings, sandbox)
    cfg = PackageConfig(name=package, import_name=import_name)
    tb = traceback_file.read_text(encoding="utf-8") if traceback_file else None

    async def run() -> None:
        gh, pypi = GitHubRest(settings.github_token), PyPIClient(settings.pypi_url)
        try:
            tree = await fetch_github_tree(gh, repo, ref)
            py = pick_python_for_commit(tree, reported=python)
            version = pretend_version(await pypi.releases(package), tree.committed_at)
            day = f"{tree.committed_at:%Y-%m-%d}" if tree.committed_at else "日期未知"
            size = len(tree.tarball) / 1e6
            typer.echo(f"{repo}@{tree.sha[:10]}（{day}） · Python {py} · "
                       f"伪版本号 {version} · 源码包 {size:.1f} MB")
            env = await source_env(cache, tree, python=py, version=version)
            vr = await PackageReproducer(
                sandbox, cache, pypi, run_timeout_s=settings.sandbox_run_timeout_seconds
            ).evaluate(cfg, env, f"{tree.sha[:10]}", script.read_text(encoding="utf-8"),
                       reported_traceback=tb)
        except (*SETUP_ERRORS, SourceError) as e:
            typer.echo(f"环境没搭起来：{e}")
            raise typer.Exit(1) from e
        finally:
            await gh.aclose()
            await pypi.aclose()
        v = vr.verdict
        typer.echo(f"环境{'命中' if vr.cache_hit else '新建'} {vr.env_key[:12]}：{v.kind}"
                   f" 一致度={v.match}（{v.match_method}） 运行 {v.runs} 次 · {v.reason}")
        typer.echo(f"日志={vr.log_dir}\n{vr.output_tail}")

    asyncio.run(run())


@replay_app.command("fbpa")
def replay_fbpa(
    repo: Annotated[str, typer.Argument(help="owner/name（修复提交和源码都从这里取）")],
    source_run: Annotated[
        Path, typer.Option("--from", help="复现回放的结果 eval/runs/*__repro__*.json")
    ],
    runs: Annotated[int, typer.Option(help="每个版本跑几次，结果必须一致")] = 2,
    numbers: Annotated[str | None, typer.Option(help="逗号分隔的编号，只跑这些（调试用）")] = None,
) -> None:
    """严格 FB/PA：把复现回放里的 L1 脚本放到修复提交的前后各跑一次。不调用 LLM。

    需要 GITHUB_TOKEN（查修复提交用 GraphQL）和 Docker。
    """
    import httpx

    from failgate.platforms.github_rest import GitHubRest, GraphQLError
    from failgate.replay import fbpa
    from failgate.replay.fixes import FixCommit, find_fix
    from failgate.repro.issue import IssueReproReport
    from failgate.repro.package import SETUP_ERRORS
    from failgate.repro.pypi import PyPIClient
    from failgate.repro.sandbox import ExecResult
    from failgate.repro.source import (
        SourceError,
        SourceTree,
        fetch_github_tree,
        pretend_version,
        run_script,
        source_env,
    )

    settings = Settings()
    if not settings.github_token:
        raise typer.BadParameter("需要 GITHUB_TOKEN（GraphQL 必须认证）")
    data = json.loads(source_run.read_text(encoding="utf-8"))
    reports = [IssueReproReport.model_validate(r) for r in data["reports"]]
    if numbers:
        wanted = {int(x) for x in numbers.split(",") if x.strip()}
        reports = [r for r in reports if r.number in wanted]
    if not reports:
        raise typer.BadParameter("没有可回放的 issue")
    package = reports[0].repro.package
    sandbox = build_sandbox(settings)
    cache = _env_cache(settings, sandbox)
    started = datetime.now()
    run_id = f"{repo.replace('/', '__')}__fbpa__{started:%Y%m%d-%H%M}"
    run_path, report_path = _run_paths(run_id)
    meta = {"started": started.isoformat(timespec="seconds"),
            "source_run": source_run.as_posix(), "runs": runs, "package": package,
            "kind": "L1 脚本"}

    async def run() -> None:
        gh, pypi = GitHubRest(settings.github_token), PyPIClient(settings.pypi_url)
        trees: dict[str, SourceTree] = {}

        async def pretend(fix: FixCommit) -> str:
            return pretend_version(await pypi.releases(package), fix.committed_at)

        async def run_at(sha: str, python: str, version: str, script: str) -> list[ExecResult]:
            if sha not in trees:
                trees[sha] = await fetch_github_tree(gh, repo, sha)
            env = await source_env(cache, trees[sha], python=python, version=version)
            typer.echo(f"    {sha[:10]} 环境{'命中' if env.cache_hit else '新建'}")
            return [
                await run_script(sandbox, env, script,
                                 timeout_s=settings.sandbox_run_timeout_seconds)
                for _ in range(runs)
            ]

        cases: list[fbpa.FbpaCase] = []
        try:
            for i, report in enumerate(reports, 1):
                typer.echo(f"\n===== [{i}/{len(reports)}] #{report.number} {report.title[:70]}")
                case = await fbpa.evaluate_case(
                    fbpa.candidate_from_l1(report),
                    find_fix=lambda n: find_fix(gh, repo, n),
                    pretend=pretend, run_at=run_at,
                    setup_errors=(*SETUP_ERRORS, SourceError, GraphQLError, httpx.HTTPError),
                )
                cases.append(case)
                pr = f"PR #{case.fix.pr}" if case.fix and case.fix.pr else "—"
                typer.echo(f"  → {case.outcome}（{pr}；代理：{case.proxy}）"
                           + (f" {case.error[:160]}" if case.error else ""))
                # 每跑完一个就落盘，中途出错不丢已有结果
                run_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.parent.mkdir(parents=True, exist_ok=True)
                run_path.write_text(fbpa.dump(cases, meta), encoding="utf-8")
                report_path.write_text(fbpa.render(repo, cases, meta), encoding="utf-8")
        finally:
            await gh.aclose()
            await pypi.aclose()
        s = fbpa.summarize(cases)
        typer.echo(f"\n严格 FB/PA：{s['fb_pa']}/{s['eligible']}（代理 {s['proxy_fb_pa']}）"
                   f"\n报告：{report_path}")

    asyncio.run(run())



class _L2Runtime:
    """L2 需要的一组长生命周期对象：LLM、沙箱、环境缓存、PyPI、GitHub。"""

    def __init__(self, settings: Settings) -> None:
        from failgate.app import build_llm
        from failgate.platforms.github_rest import GitHubRest
        from failgate.repro.l2 import TestReproducer
        from failgate.repro.pypi import PyPIClient

        llm = build_llm(settings)
        if llm is None:
            raise typer.BadParameter("请先在 .env 中设置 LLM_API_KEY")
        self.settings = settings
        self.llm = llm
        self.sandbox = build_sandbox(settings)
        self.pypi = PyPIClient(settings.pypi_url)
        self.gh = GitHubRest(settings.github_token)
        self.tester = TestReproducer(
            self.sandbox, _env_cache(settings, self.sandbox), self.pypi,
            run_timeout_s=settings.sandbox_run_timeout_seconds,
        )

    async def run(
        self, repo: str, source_repo: str, doc: IssueDoc, cfg: PackageConfig,
        *, max_steps: int | None = None,
    ) -> L2IssueReport:
        from failgate.repro.issue import reproduce_issue_l2

        s = self.settings
        if doc.created_at is None:
            raise typer.BadParameter(f"#{doc.number} 没有创建时间，无法确定 issue 时的代码")
        return await reproduce_issue_l2(
            repo=repo, number=doc.number, title=doc.title, body=doc.body,
            created_at=doc.created_at, cfg=cfg, source_repo=source_repo, gh=self.gh,
            llm=self.llm, small_model=s.llm_model_small, large_model=s.llm_model_large,
            tester=self.tester, max_steps=max_steps or s.repro_max_steps,
            max_attempts=s.repro_max_attempts, budget_usd=s.repro_budget_usd,
            artifacts_dir=Path(s.sandbox_artifacts_dir),
        )

    async def aclose(self) -> None:
        await self.gh.aclose()
        await self.pypi.aclose()
        await self.llm.aclose()


def _echo_l2_report(report: L2IssueReport) -> None:
    src, a = report.source, report.agent
    typer.echo(f"#{report.number} {report.title[:70]}")
    sha = src.sha[:10] if src.sha else "—"
    typer.echo(f"  issue 时的提交 {sha} · Python {src.python} · {src.pytest} · {src.test_path}")
    if a is not None:
        typer.echo(f"  Agent：{a.status} · {a.steps} 步 · 提交 {len(a.attempts)} 次 · "
                   f"{a.duration_s}s · ${a.cost_usd:.4f}")
        for att in a.attempts:
            typer.echo(f"    提交 {att.n}：{att.kind} · {att.reason[:120]}")
    typer.echo(f"  证据等级：{src.level}" + (f" · {src.error[:300]}" if src.error else ""))
    if src.level.value == "L2" and a and a.final_script:
        typer.echo(f"\n--- {src.test_path} ---\n{a.final_script}")
    typer.echo(f"  花费 ${report.total_cost_usd:.4f}；对话记录 {a.transcript_path if a else '—'}")


@repro_app.command("l2")
def repro_l2(
    repo: Annotated[str, typer.Argument(help="owner/name（issue 所在仓库）")],
    number: Annotated[int, typer.Argument(help="issue 编号（必须已在索引里）")],
    package: Annotated[str, typer.Option(help="PyPI 包名")],
    source_repo: Annotated[
        str | None, typer.Option(help="源码仓库，默认就是 issue 所在仓库")
    ] = None,
    import_name: Annotated[str | None, typer.Option(help="import 名，默认由包名推出")] = None,
    db_url: Annotated[str | None, typer.Option("--db", help="数据库连接串，默认用配置")] = None,
    max_steps: Annotated[int | None, typer.Option(help="工具调用步数上限")] = None,
) -> None:
    """L2：在 issue 创建时的代码上，Agent 写一个仓库内的失败测试（source 模式）。会花钱。"""
    from failgate.repro.config import PackageConfig

    settings = Settings()
    cfg = PackageConfig(name=package, import_name=import_name)

    async def run() -> None:
        docs = await _load_issue_docs(db_url or settings.failgate_db_url, repo, [number])
        if not docs:
            raise typer.BadParameter(f"{repo}#{number} 不在索引里，先运行 failgate index build")
        rt = _L2Runtime(settings)
        try:
            report = await rt.run(repo, source_repo or repo, docs[0], cfg, max_steps=max_steps)
        finally:
            await rt.aclose()
        _echo_l2_report(report)

    asyncio.run(run())


@replay_app.command("l2")
def replay_l2(
    repo: Annotated[str, typer.Argument(help="owner/name")],
    package: Annotated[str, typer.Option(help="PyPI 包名")],
    import_name: Annotated[str | None, typer.Option(help="import 名，默认由包名推出")] = None,
    limit: Annotated[int, typer.Option(help="按选样规则取最新的多少个 issue")] = 12,
    offset: Annotated[int, typer.Option(help="跳过最新的多少个（开发集），用来取留出集")] = 0,
    since: Annotated[str, typer.Option(help="只取这个日期之后创建的 issue")] = "2022-01-01",
    numbers: Annotated[
        str | None, typer.Option(help="逗号分隔的编号，指定时不按规则选样（调试用）")
    ] = None,
    runs: Annotated[int, typer.Option(help="严格 FB/PA 时每个版本跑几次")] = 2,
    db_url: Annotated[str, typer.Option("--db", help="回放语料库")] = REPLAY_DB,
) -> None:
    """L2 回放：Agent 在 issue 时的代码上写仓库内的失败测试，再用严格 FB/PA 检验。会花钱。

    选样规则和 replay repro 相同；需要 GITHUB_TOKEN 和 Docker。
    """
    import httpx

    from failgate.platforms.github_rest import GraphQLError
    from failgate.replay import fbpa
    from failgate.replay import l2 as l2replay
    from failgate.replay.fixes import FixCommit, find_fix
    from failgate.replay.repro import select_issues
    from failgate.repro.agent import TEST_PROMPT_VERSION
    from failgate.repro.config import PackageConfig
    from failgate.repro.l2 import L2Unsupported
    from failgate.repro.package import SETUP_ERRORS
    from failgate.repro.sandbox import ExecResult
    from failgate.repro.source import SourceError, SourceTree, fetch_github_tree

    settings = Settings()
    if not settings.github_token:
        raise typer.BadParameter("需要 GITHUB_TOKEN（查修复提交用 GraphQL）")
    cfg = PackageConfig(name=package, import_name=import_name)
    started = datetime.now()
    run_id = f"{repo.replace('/', '__')}__l2__{started:%Y%m%d-%H%M}"
    run_path, report_path = _run_paths(run_id)
    selection = (
        f"指定编号 {numbers}" if numbers else
        f"和 replay repro 相同的规则，{since} 之后创建，按编号从新到旧跳过 {offset} 个、"
        f"取 {limit} 个"
    )
    meta = {
        "started": started.isoformat(timespec="seconds"), "model": settings.llm_model_large,
        "prompt": TEST_PROMPT_VERSION, "selection": selection,
        "max_steps": settings.repro_max_steps, "max_attempts": settings.repro_max_attempts,
        "budget_usd": settings.repro_budget_usd, "runs": runs, "kind": "L2 测试",
    }

    async def run() -> None:
        if numbers:
            wanted = [int(x) for x in numbers.split(",") if x.strip()]
        else:
            db = Database(db_url)
            await db.create_all()
            async with db.session() as s:
                repo_row = await s.scalar(select(Repo).where(Repo.full_name == repo))
                if repo_row is None:
                    raise typer.BadParameter(f"{repo} 不在回放库里")
                all_docs = (await s.scalars(
                    select(IssueDoc).where(IssueDoc.repo_id == repo_row.id)
                )).all()
            await db.dispose()
            wanted = [d.number for d in select_issues(
                all_docs, since=datetime.fromisoformat(since), limit=limit, offset=offset
            )]
        docs = await _load_issue_docs(db_url, repo, wanted)
        typer.echo(f"选中 {len(docs)} 个：{[d.number for d in docs]}")
        rt = _L2Runtime(settings)
        trees: dict[str, SourceTree] = {}
        reports: list[L2IssueReport] = []
        cases: list[fbpa.FbpaCase] = []
        run_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            for i, doc in enumerate(docs, 1):
                typer.echo(f"\n===== [{i}/{len(docs)}] #{doc.number} {doc.title[:70]}")
                report = await rt.run(repo, repo, doc, cfg)
                reports.append(report)
                a = report.agent
                typer.echo(f"  → {report.source.level} · {a.status if a else report.source.error}"
                           f" · ${report.total_cost_usd:.4f}")
                pin, src_version = report.source.pytest, report.source.version

                async def pretend(fix: FixCommit, v: str | None = src_version) -> str:
                    return v or "0.0.0.dev0"

                async def run_at(sha: str, python: str, version: str, code: str,
                                 pin: str | None = pin, number: int = doc.number
                                 ) -> list[ExecResult]:
                    # 修复前后用和 L2 时同一套 Python、pytest、伪版本号：只让代码变化
                    if sha not in trees:
                        trees[sha] = await fetch_github_tree(rt.gh, repo, sha)
                    prepared = await rt.tester.prepare(
                        cfg, trees[sha], number=number, python=python, version=version,
                        pytest=pin,
                    )
                    return [await rt.tester.run_once(prepared, code) for _ in range(runs)]

                case = await fbpa.evaluate_case(
                    fbpa.candidate_from_l2(report),
                    find_fix=lambda n: find_fix(rt.gh, repo, n),
                    pretend=pretend, run_at=run_at,
                    setup_errors=(*SETUP_ERRORS, SourceError, L2Unsupported, GraphQLError,
                                  httpx.HTTPError),
                )
                cases.append(case)
                if case.outcome != "no_script":
                    typer.echo(f"  严格 FB/PA：{case.outcome}")
                # 每跑完一个就落盘，中途出错不丢已有结果
                run_path.write_text(l2replay.dump(reports, cases, meta), encoding="utf-8")
                report_path.write_text(
                    l2replay.render(repo, reports, cases, meta), encoding="utf-8"
                )
        finally:
            await rt.aclose()
        sm = l2replay.summarize(reports, cases)
        typer.echo(f"\nL2 {sm['l2']}/{sm['n']}；L2 测试严格 FB/PA "
                   f"{sm['fb_pa']}/{sm['fbpa_eligible']}；花费 ${sm['total_cost_usd']}"
                   f"\n报告：{report_path}")

    asyncio.run(run())


@replay_app.command("fixtures")
def replay_fixtures(
    only: Annotated[
        str | None, typer.Option(help="逗号分隔的 fixture 名，只跑这些（调试用）")
    ] = None,
    runs: Annotated[int, typer.Option(help="严格 FB/PA 时每个版本跑几次")] = 2,
) -> None:
    """fixture 仓库验收：每个 fixture 上 Agent 写 L2 测试，再在有 bug / 打上 fix 的代码上检验。

    不需要 GitHub；会花 LLM 的钱（每个 fixture 约 $0.01）。
    """
    from failgate.replay import fixtures as fx_mod
    from failgate.repro.agent import TEST_PROMPT_VERSION

    settings = Settings()
    names = [x.strip() for x in only.split(",")] if only else []
    items = fx_mod.load_all(only=names)
    if not items:
        raise typer.BadParameter("没有找到 fixture（fixtures/repos/*/fixture.json）")
    started = datetime.now()
    run_path, report_path = _run_paths(f"fixtures__l2__{started:%Y%m%d-%H%M}")
    meta = {"started": started.isoformat(timespec="seconds"), "model": settings.llm_model_large,
            "prompt": TEST_PROMPT_VERSION, "runs": runs}

    async def run() -> None:
        rt = _L2Runtime(settings)
        s = settings
        results: list[fx_mod.FixtureResult] = []
        run_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            for fx in items:
                typer.echo(f"\n===== {fx.name}：{fx.title}")
                intake, intake_cost = await fx_mod.run_intake(fx, rt.llm, s.llm_model_small)
                report = await fx_mod.reproduce_fixture(
                    fx, intake, llm=rt.llm, model=s.llm_model_large, tester=rt.tester,
                    max_steps=s.repro_max_steps, max_attempts=s.repro_max_attempts,
                    budget_usd=s.repro_budget_usd, artifacts_dir=Path(s.sandbox_artifacts_dir),
                )
                report.intake_cost_usd = intake_cost
                case = None
                if report.source.level.value == "L2":
                    case = await fx_mod.fixture_fbpa(fx, report, rt.tester, runs=runs)
                result = fx_mod.FixtureResult(name=fx.name, kind=fx.kind, expect=fx.expect,
                                              report=report, fbpa=case)
                results.append(result)
                typer.echo(f"  → {report.source.level} · {result.verdict or report.source.error}"
                           f" · 严格 FB/PA {case.outcome if case else '—'}"
                           f" · ${report.total_cost_usd:.4f} · {'✅' if result.passed else '❌'}")
                run_path.write_text(fx_mod.dump(results, meta), encoding="utf-8")
                report_path.write_text(fx_mod.render(results, meta), encoding="utf-8")
        finally:
            await rt.aclose()
        ok = sum(r.passed for r in results)
        typer.echo(f"\n验收 {ok}/{len(results)}\n报告：{report_path}")

    asyncio.run(run())


if __name__ == "__main__":
    app()
