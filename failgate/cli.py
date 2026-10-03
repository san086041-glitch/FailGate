from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
import uvicorn
from sqlalchemy import select

from failgate.db import Case, Database, IssueDoc, Repo
from failgate.i18n import t
from failgate.repro.sandbox import DockerSandbox
from failgate.settings import Settings

if TYPE_CHECKING:
    from failgate.repro.config import PackageConfig
    from failgate.repro.envcache import EnvCache
    from failgate.repro.issue import IssueReproReport, L2IssueReport
    from failgate.repro.package import PackageRepro

_CJK = re.compile(r"[\u4e00-\u9fff]")

app = typer.Typer(help="FailGate：bug 的验收层。修复谁都能写，FailGate 负责证明它修对了。")

# --help 里的分组（ADR 0036）；没列出的命令落在默认的 Commands 组里
PANELS = {
    "出题 · 答题 · 阅卷": ("repro", "hidden", "evidence", "fix", "verify"),
    "服务与集成": ("up", "serve", "worker", "console", "mcp", "github", "fixer", "repo"),
    "评测与回放": ("checkup", "replay", "try", "answer", "memory", "llm"),
    "运维": ("doctor", "db", "db-init", "cases", "effects", "sandbox", "trace", "index"),
}


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    env_file: Annotated[Path | None, typer.Option(
        "--env-file",
        help="配置文件；不给时依次找 $FAILGATE_ENV_FILE、当前目录、项目目录")] = None,
    quiet: Annotated[bool, typer.Option(
        "--quiet", "-q", help="首页不显示 logo（也可以设 FAILGATE_NO_BANNER=1）")] = False,
    shell: Annotated[bool, typer.Option(
        "--shell/--no-shell", help="显示首页后进入交互模式（只在终端里生效）")] = True,
    ui_lang: Annotated[str | None, typer.Option(
        "--lang", help="界面语言 zh / en（默认读 FAILGATE_LANG，再默认中文）")] = None,
) -> None:
    """找到 .env 并让之后所有 Settings() 都读它；不带子命令时显示首页，在终端里再进入交互模式。"""
    from failgate.settings import find_env_file, use_env_file

    try:
        choice = find_env_file(env_file)
    except FileNotFoundError as exc:
        raise typer.BadParameter(str(exc), param_hint="--env-file") from exc
    use_env_file(choice.path)
    ctx.obj = choice
    from failgate.i18n import set_lang

    set_lang(ui_lang or Settings().failgate_lang)
    if ctx.invoked_subcommand is not None or _SHELL_ACTIVE:
        return
    from failgate import home

    settings = Settings()
    out = home.make_console()

    def show_home(banner: bool) -> None:
        report = asyncio.run(home.collect(Settings(), choice, full=False))
        home.render_home(report, out, banner=banner)

    show_home(home.banner_enabled(out, quiet or settings.failgate_no_banner))
    if shell and out.is_terminal and sys.stdin.isatty():
        _run_shell(choice.path, ui_lang, lambda: show_home(False))


_SHELL_ACTIVE = False  # 交互模式里每条命令都会再走一遍根回调：别再嵌套进一个交互模式


def _run_shell(env_file: Path | None, ui_lang: str | None,
               show_home: Callable[[], None]) -> None:
    global _SHELL_ACTIVE
    from failgate import shell as sh

    command = typer.main.get_command(app)
    # 启动时用的 .env 固定下来：之后每条命令都带上，不会因为找的顺序变了而换文件
    base = ["--env-file", str(env_file)] if env_file is not None else []
    if ui_lang:  # 启动时给了 --lang：之后每条命令都带上
        base += ["--lang", ui_lang]
    _SHELL_ACTIVE = True
    try:
        sh.loop(sh.Shell(command, base_args=base, show_home=show_home), sh.make_session(command))
    finally:
        _SHELL_ACTIVE = False


@app.command()
def doctor(ctx: typer.Context) -> None:
    """逐项检查运行环境，失败的给出修复办法（有失败时退出码 1）。"""
    from failgate import home
    from failgate.settings import find_env_file

    choice = ctx.obj or find_env_file()
    report = asyncio.run(home.collect(Settings(), choice, full=True))
    home.render_doctor(report, home.make_console())
    if report.failed:
        raise typer.Exit(1)


@app.command()
def up(
    ctx: typer.Context,
    host: str = "127.0.0.1",
    port: int = 8080,
    tunnel: Annotated[bool, typer.Option(
        "--tunnel/--no-tunnel", help="用 smee 把 GitHub 的 webhook 转发到本机")] = True,
    worker: Annotated[bool, typer.Option(
        help="另起一个只跑沙箱车道的 worker（要 QUEUE_BACKEND=redis）")] = False,
    allow_pending: Annotated[bool, typer.Option(
        help="有待发的写操作也启动（服务一起来就会把它们发出去）")] = False,
    logs: Annotated[bool, typer.Option(
        "--logs/--no-logs", help="显示子进程的全部日志（关掉时只留警告和错误）")] = True,
) -> None:
    """本机一键上线：检查环境 → 起服务和 smee 转发 → 底部实时状态板 → Ctrl+C 一次全部停掉
    （ADR 0037、0038）。"""
    import time
    from datetime import UTC, datetime

    from rich.live import Live
    from rich.text import Text

    from failgate import home
    from failgate import up as u
    from failgate.settings import find_env_file

    settings = Settings()
    choice = ctx.obj or find_env_file()
    out = home.make_console()

    # 1. 环境：任何一项失败都不启动
    report = asyncio.run(home.collect(settings, choice, full=True))
    if report.failed:
        home.render_doctor(report, out)
        out.print(Text(t("环境有问题，没有启动。", "Environment problems: not started."),
                       style="red"))
        raise typer.Exit(1)
    if worker and settings.queue_backend != "redis":
        raise typer.BadParameter(t("--worker 要 QUEUE_BACKEND=redis"
                                   "（进程内队列只能在服务进程里跑）",
                                   "--worker needs QUEUE_BACKEND=redis (the in-process queue "
                                   "only runs inside the server)"))
    out.print(Text(t("✓ 环境检查通过", "✓ environment OK"), style="green"))

    # 2. 待发的写操作：默认不启动，先列出来
    if report.stats is not None and report.stats.get("pending_effects"):
        pending = asyncio.run(u.pending_effects(settings))
        if not allow_pending:
            count = report.stats["pending_effects"]
            out.print(Text(t(f"有 {count} 条待发的写操作，服务一启动就会把它们发出去：",
                             f"{count} pending write(s) will be sent as soon as the server "
                             "starts:"), style="yellow"))
            for e in pending:
                out.print(f"  {e['at']:%m-%d %H:%M}  {e['action']:<16} "
                          f"{e['repo']}#{e['number']} "
                          + t(f"（{e['mode']}，已试 {e['attempts']} 次）",
                              f"({e['mode']}, {e['attempts']} attempt(s))"))
            out.print(t("确认可以发出去：failgate up --allow-pending；"
                        "先看清楚：failgate effects list",
                        "OK to send: failgate up --allow-pending; review: failgate effects list"))
            raise typer.Exit(1)
        out.print(Text(t(f"! 有 {len(pending)} 条待发的写操作，启动后会补发（--allow-pending）",
                         f"! {len(pending)} pending write(s) will be sent after start "
                         "(--allow-pending)"),
                       style="yellow"))
    # 3. 端口
    if u.port_in_use(host, port):
        out.print(Text(t(f"端口 {port} 已经被占用（可能是之前起的 failgate serve）。",
                         f"Port {port} is in use (maybe an earlier failgate serve)."),
                       style="red"))
        out.print(t(f"  停掉它：{u.stop_port_hint(port)}\n"
                    "  之前如果还手动起过 smee 转发，也一起关掉（同一个通道会转发给两个服务）；\n"
                    "  或者换端口：failgate up --port 8081",
                    f"  stop it: {u.stop_port_hint(port)}\n"
                    "  also stop any smee client you started by hand (one channel would feed "
                    "two servers);\n  or use another port: failgate up --port 8081"))
        raise typer.Exit(1)
    # 4. 转发通道
    smee, npx = None, None
    if tunnel:
        smee, source = asyncio.run(u.resolve_smee(settings))
        npx = u.find_npx()
        if smee and not npx:
            out.print(Text(t("! 没找到 npx（要装 Node.js）：只起服务，不转发",
                             "! npx not found (install Node.js): server only, no tunnel"),
                           style="yellow"))
            smee = None
        elif smee:
            out.print(Text(t(f"✓ 转发通道：{smee}（{source}）", f"✓ tunnel: {smee} ({source})"),
                           style="green"))
        else:
            out.print(Text(t(f"! 不转发：{source}（可以在 .env 里设 SMEE_URL）",
                             f"! no tunnel: {source} (set SMEE_URL in .env)"), style="yellow"))
    live_repos = [r["repo"] for r in (report.stats or {}).get("repos", [])
                  if r["mode"] == "live"]
    if live_repos:
        repos_txt = ", ".join(live_repos)
        out.print(Text(t(f"! live 模式的仓库会真的发评论、打标签：{repos_txt}",
                         f"! live-mode repos will really get comments and labels: {repos_txt}"),
                       style="yellow"))

    # 5. 起进程：先服务，等 /healthz，再 worker 和转发（转发早了事件会打到还没起来的服务上）
    procs = u.plan(env_file=choice.path, host=host, port=port, smee=smee, npx=npx,
                   worker=worker)
    sup = u.Supervisor(out, verbose=logs)
    base = f"http://{host}:{port}"
    code = 0
    board = u.Board(procs, base, datetime.now(UTC))
    live: Live | None = None
    loop = asyncio.new_event_loop()
    db = Database(settings.failgate_db_url)
    try:
        serve_proc = procs[0]
        sup.start(serve_proc)

        def serving() -> bool:
            return serve_proc.popen is not None and serve_proc.popen.poll() is None

        if not u.wait_healthy(f"{base}/healthz", serving):
            out.print(Text(t("服务没能启动（看上面 serve 的输出）",
                             "the server did not start (see the serve output above)"),
                           style="red"))
            code = 1
        else:
            for proc in procs[1:]:
                sup.start(proc)
            out.print(Text(t(f"\n▶ 服务    {base}    工作台 {base}/console",
                             f"\n▶ server  {base}    console {base}/console"),
                           style="bold green"))
            if smee:
                out.print(Text(t("▶ 转发    ", "▶ tunnel  ") + f"{smee} → {base}/webhooks/github",
                               style="bold green"))
            if worker:
                out.print(Text(t("▶ worker  沙箱车道（复现 / 核验 / 修复）",
                                 "▶ worker  sandbox lane (repro / verify / fix)"),
                               style="bold green"))
            out.print(Text(t("  Ctrl+C 停止全部\n", "  Ctrl+C stops everything\n"), style="dim"))
            if out.is_terminal:  # 底部状态板；日志照常在上面滚
                loop.run_until_complete(board.refresh(db))
                live = Live(board, console=out, refresh_per_second=2)
                live.start()
            last = time.monotonic()
            while code == 0:
                dead = sup.exited()
                if dead is not None and dead.popen is not None:
                    rc = dead.popen.returncode
                    out.print(Text(t(f"{dead.name} 退出了（退出码 {rc}），停止全部",
                                     f"{dead.name} exited (code {rc}); stopping everything"),
                                   style="red"))
                    code = 1
                if live is not None and time.monotonic() - last >= 2:
                    loop.run_until_complete(board.refresh(db))
                    last = time.monotonic()
                time.sleep(0.25)
    except KeyboardInterrupt:
        out.print(Text(t("\n正在停止…", "\nStopping…"), style="yellow"))
    finally:
        if live is not None:
            live.stop()
        sup.stop_all()
        loop.run_until_complete(db.dispose())
        loop.close()
    out.print(Text(t("已全部停止", "All stopped"), style="dim"))
    if code:
        raise typer.Exit(code)


@app.command()
def console(
    host: str = "127.0.0.1",
    port: int = 8080,
    open_browser: Annotated[bool, typer.Option("--open/--no-open", help="打开浏览器")] = True,
) -> None:
    """在浏览器打开工作台（先确认 `failgate serve` 在跑）。"""
    import webbrowser

    import httpx

    base = f"http://{host}:{port}"
    try:
        health = httpx.get(f"{base}/healthz", timeout=2.0)
        page = httpx.get(f"{base}/console", timeout=2.0, follow_redirects=False)
    except httpx.HTTPError:
        typer.echo(t(f"{base} 上没有服务在跑：先另开一个终端运行 failgate serve",
                     f"nothing is serving on {base}: run failgate up (or serve) first"),
                   err=True)
        raise typer.Exit(1) from None
    if health.status_code != 200:
        typer.echo(t(f"{base}/healthz 返回 {health.status_code}，不像是 FailGate 服务",
                     f"{base}/healthz returned {health.status_code}: not a FailGate server"),
                   err=True)
        raise typer.Exit(1)
    if page.status_code == 404:
        from failgate.up import stop_port_hint

        stop = stop_port_hint(port)
        typer.echo(t(f"{base} 上跑的是旧版本的 failgate serve（没有工作台）。\n"
                     f"  1. 停掉它：{stop}\n"
                     "  2. 用新代码重新启动：failgate serve\n"
                     "  或者另起一个端口：failgate serve --port 8081，"
                     "再 failgate console --port 8081",
                     f"{base} runs an old failgate serve (no console).\n"
                     f"  1. stop it: {stop}\n"
                     "  2. restart with the new code: failgate serve\n"
                     "  or use another port: failgate serve --port 8081, "
                     "then failgate console --port 8081"),
                   err=True)
        raise typer.Exit(1)
    url = f"{base}/console"
    typer.echo(t("工作台：", "Console: ") + url)
    if Settings().console_token:
        typer.echo(t("配置了 CONSOLE_TOKEN：第一次访问用 /console?token=<令牌>，之后存在 cookie 里",
                     "CONSOLE_TOKEN is set: open /console?token=<token> once; it is kept "
                     "in a cookie"))
    if open_browser:
        webbrowser.open(url)


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8080, reload: bool = False) -> None:
    """启动 API 服务；worker 默认也在这个进程里跑（WORKER_LANES，见 `failgate worker`）。"""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    uvicorn.run("failgate.app:create_app", factory=True, host=host, port=port, reload=reload)


@app.command()
def worker(
    lanes: Annotated[str, typer.Option(help="跑哪几条车道：events、sandbox，逗号分隔")] = (
        "events,sandbox"),
) -> None:
    """独立的 worker 进程（QUEUE_BACKEND=redis 才有意义，ADR 0023）。

    例：serve 设 WORKER_LANES=events 只跑快车道，另起 `failgate worker --lanes sandbox`
    专门跑复现 / 核验；沙箱 worker 要能访问 Docker。"""
    from failgate.app import FailGate
    from failgate.orchestrator.redis_queue import LANES, RedisQueues

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = Settings()
    if settings.queue_backend != "redis":
        raise typer.BadParameter("独立 worker 需要 QUEUE_BACKEND=redis")
    chosen = [x.strip() for x in lanes.split(",") if x.strip()]
    unknown = [x for x in chosen if x not in LANES]
    if unknown or not chosen:
        raise typer.BadParameter(f"未知车道：{unknown or lanes}（可选 {', '.join(LANES)}）")

    async def run() -> None:
        fg = FailGate(settings)
        await fg.start(run_worker=False)
        assert isinstance(fg.worker, RedisQueues)
        typer.echo(f"worker 启动：{', '.join(chosen)}（{settings.redis_url}）")
        try:
            await fg.worker.run_forever(chosen)
        finally:
            await fg.stop()

    asyncio.run(run())


@app.command("db-init")
def db_init() -> None:
    """创建数据库表（PostgreSQL 上是升级到最新迁移；等同 `failgate db upgrade`）。"""

    async def run() -> None:
        db = Database(Settings().failgate_db_url)
        await db.create_all()
        await db.dispose()

    asyncio.run(run())
    typer.echo("数据库已初始化")


db_app = typer.Typer(help="数据库：迁移、查看版本、搬库（ADR 0024）", no_args_is_help=True)
app.add_typer(db_app, name="db")


@db_app.command("upgrade")
def db_upgrade(
    db_url: Annotated[str | None, typer.Option("--db", help="数据库连接串，默认用配置")] = None,
) -> None:
    """升级到最新迁移（PostgreSQL）；SQLite 上是建表 + 补列。服务启动时也会自动做。"""
    from failgate.migrations import current, head

    async def run() -> str | None:
        db = Database(db_url or Settings().failgate_db_url)
        try:
            await db.create_all()
            if db.is_sqlite:
                return None
            async with db.engine.connect() as conn:
                return await conn.run_sync(current)
        finally:
            await db.dispose()

    rev = asyncio.run(run())
    typer.echo("SQLite：已建表并补齐列" if rev is None else f"已升级到 {rev}（最新 {head()}）")


@db_app.command("current")
def db_current(
    db_url: Annotated[str | None, typer.Option("--db", help="数据库连接串，默认用配置")] = None,
) -> None:
    """查看库的迁移版本和代码里的最新版本。"""
    from failgate.migrations import current, head

    async def run() -> str | None:
        db = Database(db_url or Settings().failgate_db_url)
        try:
            async with db.engine.connect() as conn:
                return await conn.run_sync(current)
        finally:
            await db.dispose()

    typer.echo(f"库：{asyncio.run(run()) or '（没有迁移记录）'}；代码最新：{head()}")


@db_app.command("copy")
def db_copy(
    src: Annotated[str, typer.Option("--from", help="源库连接串（例如本机的 SQLite）")],
    dst: Annotated[str, typer.Option("--to", help="目标库（必须是空库，会先升级）")],
) -> None:
    """把整个库复制到另一个空库（例如 SQLite → PostgreSQL），不经过 ORM、原样搬运。"""
    from failgate.db_copy import copy_database

    counts = asyncio.run(copy_database(src, dst, echo=typer.echo))
    typer.echo(f"完成：{len(counts)} 张表，共 {sum(counts.values())} 行")


@app.command()
def cases(
    limit: int = 20,
    state: Annotated[str | None, typer.Option(help="只看这个状态，例如 VERIFIED")] = None,
    repo: Annotated[str | None, typer.Option(help="只看这个仓库 owner/name")] = None,
    kind: Annotated[str | None, typer.Option(help="issue 或 pull")] = None,
) -> None:
    """列出最近的 Case（状态按工作台的颜色：通过绿、驳回红、进行中蓝）。"""
    from failgate.home import make_console
    from failgate.views import cases_table

    async def run() -> list[tuple[Case, Repo]]:
        db = Database(Settings().failgate_db_url)
        async with db.session() as s:
            q = select(Case, Repo).join(Repo, Case.repo_id == Repo.id)
            if state:
                q = q.where(Case.state == state.upper())
            if repo:
                q = q.where(Repo.full_name == repo)
            if kind:
                q = q.where(Case.kind == kind)
            rows = (await s.execute(q.order_by(Case.id.desc()).limit(limit))).all()
        await db.dispose()
        return [(c, r) for c, r in rows]

    rows = asyncio.run(run())
    if not rows:
        typer.echo(t("没有符合条件的 Case", "no matching cases"))
        return
    con = make_console()
    if con.is_terminal:
        con.print(cases_table(rows))
        return
    for c, r in rows:  # 管道 / 重定向：保持原来一行一个的纯文本，方便脚本处理
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


def _code_changed(gh: Any, repo: str, trees: dict[str, Any]) -> Any:
    """严格 FB/PA 用：关闭 issue 的提交有没有改源码（ADR 0040 补充）。

    trees 是按提交缓存的源码包（和 run_at 共用，父提交不重复下载）。
    """

    async def check(fix: Any) -> bool:
        from failgate.replay import fixset as fxs
        from failgate.replay import verify_eval as ve
        from failgate.repro.source import fetch_github_tree

        if fix.parent not in trees:
            trees[fix.parent] = await fetch_github_tree(gh, repo, fix.parent)
        files = ve._pull_files(await gh.compare_files(repo, fix.parent, fix.sha))
        return bool(fxs.source_changes(files, trees[fix.parent].test_dir()))

    return check


def _require_docker(settings: Settings) -> None:
    """回放 / 复现开跑前确认 Docker 引擎连得上。

    2026-10-02 留出集回放时 Docker Desktop 停了：24 题每题先花一次 Intake 再失败，
    报告里全是"拉取镜像失败"。现在一开始就停下，不花钱。
    """
    from failgate.home import check_docker

    c = check_docker(settings, timeout=10)
    if c.ok is not True:
        raise typer.BadParameter(f"Docker 不可用：{c.detail}。{c.hint}")


def _selection(repo: str) -> Any:
    """仓库的选题规则（eval/datasets/<repo>/selection.json，ADR 0040）。"""
    from failgate.replay import selection
    from failgate.replay.dataset import EVAL_ROOT

    try:
        return selection.load(repo, EVAL_ROOT)
    except selection.SelectionMissing as e:
        raise typer.BadParameter(str(e)) from None


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
    thinking: Annotated[str | None, typer.Option(
        help="评委的思考模式：disabled / low / high / max（默认不传，用服务方默认）")] = None,
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
        recall_k=recall_k or settings.dedup_recall_k, thinking=thinking,
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
            f"{f'__think-{thinking}' if thinking else ''}"
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


@replay_app.command("dedup-compare")
def replay_dedup_compare(
    runs: Annotated[list[str], typer.Argument(
        help="标签=运行记录（JSON），第一个是成对比较的基准，如 high=eval/runs/a.json")],
    cascade: Annotated[str | None, typer.Option(
        help="离线模拟级联：便宜的标签,贵的标签（如 disabled,high）")] = None,
) -> None:
    """查重评委的思考模式对比（ADR 0026）：质量、每次调用的开销、逐样本成对比较。"""
    from failgate.replay.dataset import load_labels, repo_slug
    from failgate.replay.dedup import RunResult
    from failgate.replay.dedup_compare import cascade as simulate_cascade
    from failgate.replay.dedup_compare import render

    loaded: list[tuple[str, RunResult]] = []
    sources: list[str] = []
    for item in runs:
        label, sep, path = item.partition("=")
        if not sep:
            raise typer.BadParameter(f"要写成 标签=路径：{item}")
        loaded.append((label, RunResult.model_validate_json(Path(path).read_text("utf-8"))))
        sources.append(f"{label}=`{Path(path).name}`")
    repo = loaded[0][1].config.repo
    labels = load_labels(repo)
    text = render(loaded, labels, notes=[f"- 运行记录：{', '.join(sources)}"])
    if cascade:
        by_label = dict(loaded)
        cheap, _, expensive = cascade.partition(",")
        if cheap not in by_label or expensive not in by_label:
            raise typer.BadParameter(f"--cascade 的标签不在运行记录里：{cascade}")
        text += "\n" + simulate_cascade(by_label[cheap], by_label[expensive], labels)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    out = Path("eval/reports") / f"{repo_slug(repo)}__dedup-thinking__{stamp}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, "utf-8")
    typer.echo(text)
    typer.echo(f"报告：{out.as_posix()}")


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
            typer.echo(t("没有证据", "no evidence"))
            return
        from failgate.home import make_console
        from failgate.views import evidence_table

        con = make_console()
        if con.is_terminal:
            con.print(evidence_table(refs))
            return
        for ref in refs:  # 管道 / 重定向：原来的纯文本
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
        from failgate.home import make_console
        from failgate.views import audit_lines

        con = make_console()
        ev = ref.evidence
        receipt = json.dumps(ev.receipt, ensure_ascii=False, indent=2, sort_keys=True)
        if con.is_terminal:
            con.print_json(receipt)
        else:
            typer.echo(receipt)
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
        con.print(audit_lines(problems, ev.receipt_sha256, ev.test_sha256))
        return 1 if problems else 0

    raise typer.Exit(asyncio.run(run()))


@app.command("checkup")
def checkup(
    repo: Annotated[str, typer.Argument(help="owner/name，例如 pylint-dev/astroid")],
    package: Annotated[str, typer.Option(help="PyPI 包名")],
    import_name: Annotated[str | None, typer.Option(help="import 名，默认由包名推出")] = None,
    since: Annotated[str, typer.Option(help="只看这一天之后创建的 issue")] = "2022-01-01",
    limit: Annotated[int, typer.Option(help="留出集取多少题")] = 12,
    offset: Annotated[int, typer.Option(help="跳过最新的多少题（开发集）")] = 12,
    accept_rule: Annotated[bool, typer.Option(
        "--accept-rule", help="确认体检起草的选题规则，继续往下跑")] = False,
    survey_only: Annotated[bool, typer.Option(
        "--survey-only", help="只做概况和规则（只调 API，$0）")] = False,
    skip_verify: Annotated[bool, typer.Option(
        "--skip-verify", help="不跑 ClaimVerify 正负例")] = False,
    backfill_limit: Annotated[int, typer.Option(help="最多回填多少个 issue")] = 4000,
    refresh: Annotated[bool, typer.Option("--refresh", help="忽略之前的进度，从头来")] = False,
    report_only: Annotated[bool, typer.Option(
        "--report-only", help="只按已有结果重新生成报告，什么都不跑")] = False,
) -> None:
    """仓库体检（ADR 0043）：把评测流程跑在任意公开 Python 仓库上，给出报告和配置建议。

    概况 → 选题规则（没有就起草，确认后加 --accept-rule）→ 回填 → 留出集 L2 + 严格 FB/PA
    （调 LLM，每题约 $0.02）→ ClaimVerify 正负例含 break_other（$0）→ 报告。
    进度记在 eval/checkups/<repo>/checkup.json，中断后重跑会跳过已完成的步骤。
    需要 GITHUB_TOKEN；出题和核验还要 Docker。
    """
    import httpx

    from failgate.platforms.github_rest import GitHubRest
    from failgate.replay import checkup as cu
    from failgate.replay import l2 as l2replay
    from failgate.replay import selection
    from failgate.replay.dataset import EVAL_ROOT

    settings = Settings()
    if not settings.github_token:
        raise typer.BadParameter("需要 GITHUB_TOKEN")
    state_file = cu.state_path(repo)
    state = None if refresh else cu.load_state(state_file)
    if state is None:
        state = cu.Checkup(repo=repo, package=package, since=since, offset=offset, limit=limit)

    def step(msg: str) -> None:
        typer.echo(f"\n== {msg}")

    if report_only:
        rule_file = selection.path_for(repo, EVAL_ROOT)
        rule = selection.load(repo, EVAL_ROOT) if rule_file.exists() else None
        l2_sum = None
        if state.l2_run:
            reports, cases, _ = l2replay.load(Path(state.l2_run))
            l2_sum = l2replay.summarize(reports, cases)
        rows = cu.load_rows(Path(state.verify_run) if state.verify_run else None)
        state.report = _checkup_report(state, rule, l2_sum, rows)
        cu.save_state(state, state_file)
        typer.echo(f"报告：{state.report}")
        return

    # 1. 概况
    if state.survey is None:
        step("概况（只调 GitHub / PyPI API）")

        async def do_survey() -> cu.Survey:
            gh = GitHubRest(settings.github_token)
            try:
                async with httpx.AsyncClient(timeout=30) as http:
                    return await cu.survey(gh, http, repo, package, since=since,
                                           pypi_url=settings.pypi_url)
            finally:
                await gh.aclose()

        state.survey = asyncio.run(do_survey())
        cu.save_state(state, state_file)
    s = state.survey
    typer.echo(f"{repo}：⭐ {s.stars}，Python {s.python_share:.0%}，PyPI {s.pypi or '无'}；"
               f"{since} 后已完成关闭 {s.closed}，可用题估计约 {s.est_usable}")

    # 2. 选题规则
    rule_file = selection.path_for(repo, EVAL_ROOT)
    if not rule_file.exists():
        rule = cu.draft_selection(s.labels)
        rule_file.parent.mkdir(parents=True, exist_ok=True)
        rule_file.write_text(rule.model_dump_json(indent=1), encoding="utf-8")
        state.rule_drafted = True
        state.rule_path = rule_file.as_posix()
        cu.save_state(state, state_file)
        step("选题规则：已按标签起草")
        typer.echo(f"{rule_file}\n  bug：{rule.bug_labels}\n  复现另外要求：{rule.repro_labels}\n"
                   f"  排除：{rule.exclude_labels}")
        if not accept_rule:
            typer.echo("\n先看一下这份规则是否符合这个仓库的标签用法（可以直接改文件），"
                       "确认后加 --accept-rule 重跑。规则要在跑之前定好，否则就是挑样本。")
            return
    rule = selection.load(repo, EVAL_ROOT)
    state.rule_path = rule_file.as_posix()
    if not rule.bug_labels:
        raise typer.BadParameter(f"{rule_file} 里没有 bug_labels：这个仓库的 bug 没打标签，"
                                 "按标签选不出题")
    if survey_only:
        state.report = _checkup_report(state, rule, None, [])
        cu.save_state(state, state_file)
        typer.echo(f"\n报告：{state.report}")
        return

    # 3. 回填
    if state.backfilled is None:
        step("回填 issue（只调 GitHub）")
        index_build(repo, limit=backfill_limit, db_url=REPLAY_DB)
        state.backfilled = backfill_limit
        cu.save_state(state, state_file)

    # 4. 留出集 L2 + 严格 FB/PA
    if state.l2_run is None:
        step(f"留出集 L2 + 严格 FB/PA（跳过 {offset}、取 {limit}，会花钱）")
        run_path = replay_l2(repo, package=package, import_name=import_name, limit=limit,
                             offset=offset, since=since, numbers=None, runs=2, db_url=REPLAY_DB)
        state.l2_run = run_path.as_posix()
        cu.save_state(state, state_file)
    reports, cases, _meta = l2replay.load(Path(state.l2_run))
    l2_summary = l2replay.summarize(reports, cases)

    # 5. ClaimVerify 正负例
    if not skip_verify and l2_summary["fb_pa"]:
        step("ClaimVerify 正负例（含 break_other，$0）")
        resume = Path(state.verify_run) if state.verify_run else None
        jsonl = replay_verify(repo, source=Path(state.l2_run), resume=resume)
        state.verify_run = jsonl.as_posix()
        cu.save_state(state, state_file)
    rows = cu.load_rows(Path(state.verify_run) if state.verify_run else None)

    state.report = _checkup_report(state, rule, l2_summary, rows)
    cu.save_state(state, state_file)
    typer.echo(f"\n报告：{state.report}")


def _checkup_report(state: Any, rule: Any, l2: dict[str, Any] | None,
                    rows: list[dict[str, Any]]) -> str:
    from failgate.replay import checkup as cu

    path = Path("eval/reports") / f"{state.repo.replace('/', '__')}__checkup__" \
        f"{datetime.now():%Y%m%d-%H%M}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cu.render(state, rule, l2, rows), encoding="utf-8")
    return path.as_posix()


@app.command("verify")
def verify_pr(
    target: Annotated[str, typer.Argument(help="owner/name#PR 编号")],
    db_url: Annotated[str | None, typer.Option("--db", help="数据库 URL，默认读配置")] = None,
    out: Annotated[Path | None, typer.Option(help="把核验收据写到这个 JSON 文件")] = None,
    lang: Annotated[str, typer.Option(help="报告语言 zh / en")] = "zh",
    strength: Annotated[bool | None, typer.Option(
        "--strength/--no-strength", help="是否评估考卷强度（默认读 VERIFY_STRENGTH）")] = None,
    markdown: Annotated[bool, typer.Option(
        help="输出和 PR 评论一样的 Markdown 报告（输出不是终端时默认就是）")] = False,
    related_always: Annotated[str, typer.Option(
        help="第三层总要跑的测试，逗号分隔（如 pylint 的 tests/test_functional.py，ADR 0041）"
    )] = "",
    test_deps: Annotated[str, typer.Option(
        help="相关测试要的第三方依赖，逗号分隔（如 packaging 的 pretend；按提交日期锁版本）"
    )] = "",
) -> None:
    """用封存的考卷核验一个 PR（ClaimVerify 三层 + 考卷强度）。需要 Docker 和 GITHUB_TOKEN。

    退出码：0 通过验收，1 驳回，2 无法判定或没有声明。"""
    from failgate.home import make_console
    from failgate.platforms.github_rest import GitHubRest
    from failgate.progress import live_progress, progress_console
    from failgate.repro.l2 import TestReproducer
    from failgate.repro.pypi import PyPIClient
    from failgate.verify.claims import parse_claims
    from failgate.verify.engine import ClaimVerdict, ClaimVerifier, Exam
    from failgate.verify.report import render_verification
    from failgate.verify.store import latest_exam
    from failgate.verify.workbench import SandboxWorkbench, fetch_pull
    from failgate.views import verification_panel

    m = re.fullmatch(r"([\w.-]+/[\w.-]+)#(\d+)", target)
    if m is None:
        raise typer.BadParameter(t("要写成 owner/name#PR编号", "expected owner/name#PR"))
    repo, number = m.group(1), int(m.group(2))
    settings = Settings()

    async def run() -> int:
        gh = GitHubRest(settings.github_token)
        sandbox = DockerSandbox.from_settings(settings)
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
            claimed = claims or t("（无）", "(none)")
            typer.echo(t(f"{repo}#{number}：声称修复 {claimed}；"
                         f"合并基点 {pr.base_sha[:7]} → head {pr.head_sha[:7]}",
                         f"{repo}#{number}: claims to fix {claimed}; "
                         f"base {pr.base_sha[:7]} → head {pr.head_sha[:7]}"), err=True)
            tester = TestReproducer(sandbox, _env_cache(settings, sandbox), pypi,
                                    run_timeout_s=settings.sandbox_run_timeout_seconds)
            verifier = ClaimVerifier(
                SandboxWorkbench.for_github(
                    gh, tester, [x.strip() for x in test_deps.split(",") if x.strip()]),
                strength=settings.verify_strength if strength is None else strength,
                max_mutants=settings.strength_max_mutants,
                related_always=[x.strip() for x in related_always.split(",") if x.strip()],
            )
            with live_progress(t("核验 ", "Verify ") + f"{repo}#{number}", progress_console()):
                result = await verifier.verify(pr, claims, exams)
        finally:
            await db.dispose()
            await gh.aclose()
            await pypi.aclose()
        con = make_console()
        if markdown or not con.is_terminal:
            typer.echo(render_verification(result, lang))
        else:
            con.print(verification_panel(result))
        if out is not None:
            out.write_text(json.dumps(result.receipt(), ensure_ascii=False, indent=2,
                                      sort_keys=True) + "\n", encoding="utf-8")
        return {ClaimVerdict.VERIFIED: 0, ClaimVerdict.REFUTED: 1}.get(result.verdict, 2)  # type: ignore[arg-type]

    raise typer.Exit(asyncio.run(run()))


hidden_app = typer.Typer(help="隐藏考卷：只根据 issue 出的变体题，封存不公开（ADR 0021）",
                         no_args_is_help=True)
app.add_typer(hidden_app, name="hidden")


def _issue_target(target: str) -> tuple[str, int]:
    m = re.fullmatch(r"([\w.-]+/[\w.-]+)#(\d+)", target)
    if m is None:
        raise typer.BadParameter("要写成 owner/name#issue编号")
    return m.group(1), int(m.group(2))


@hidden_app.command("seal")
def hidden_seal(
    target: Annotated[str, typer.Argument(help="owner/name#issue 编号（要已经有封存的 L2 考卷）")],
    db_url: Annotated[str | None, typer.Option("--db", help="数据库 URL，默认读配置")] = None,
) -> None:
    """给 #N 当前的考卷出隐藏题：LLM 只看 issue 和公开考卷出题，在 issue 时的代码上挑出按预期
    失败的题，封存进 hidden_exams 表。需要 Docker、GITHUB_TOKEN 和 LLM；约 $0.001–0.01。"""
    from failgate.app import build_llm
    from failgate.platforms.github_rest import GitHubRest
    from failgate.repro.l2 import TestReproducer
    from failgate.repro.pypi import PyPIClient
    from failgate.verify.hidden import HiddenWriter, seal_hidden
    from failgate.verify.store import hidden_row, latest_exam
    from failgate.verify.workbench import SandboxWorkbench

    repo, number = _issue_target(target)
    settings = Settings()

    async def run() -> int:
        llm = build_llm(settings)
        if llm is None:
            typer.echo("没有配置 LLM（LLM_API_KEY）", err=True)
            return 2
        gh = GitHubRest(settings.github_token)
        pypi = PyPIClient(settings.pypi_url)
        db = Database(db_url or settings.failgate_db_url)
        await db.create_all()
        try:
            async with db.session() as s:
                exam = await latest_exam(s, repo, number)
            if exam is None:
                typer.echo(f"{repo}#{number} 没有封存的 L2 考卷", err=True)
                return 2
            source_sha = exam.receipt.get("source_sha")
            if not source_sha:
                typer.echo("考卷收据里没有 source_sha（不是 source 模式的 L2），出不了隐藏题",
                           err=True)
                return 2
            issue = await gh.issue(repo, number)
            sandbox = build_sandbox(settings)
            tester = TestReproducer(sandbox, _env_cache(settings, sandbox), pypi,
                                    run_timeout_s=settings.sandbox_run_timeout_seconds)
            writer = HiddenWriter(llm, settings.llm_model_large)
            out = await seal_hidden(
                SandboxWorkbench.for_github(gh, tester), writer, exam, repo=repo,
                title=issue.get("title") or "", body=issue.get("body") or "",
                source_repo=exam.receipt.get("source_repo"), source_sha=source_sha,
            )
            typer.echo(f"归纳的规律：{out.rule}")
            typer.echo(f"出题 {len(out.generated)} 道；丢掉 {len(out.dropped)} 道"
                       + (f"：{out.dropped}" if out.dropped else "") + f"；${out.cost_usd:.4f}")
            if out.hidden is None:
                typer.echo(f"❌ 没有封存（{out.reason}）")
                return 1
            async with db.session() as s, s.begin():
                s.add(hidden_row(out.hidden))
            typer.echo(f"✅ 封存 {len(out.hidden.tests)} 道隐藏题：{out.hidden.test_path}，"
                       f"sha256 {out.hidden.test_sha256[:12]}（只公布这个哈希，不公布题目）")
            return 0
        finally:
            await db.dispose()
            await gh.aclose()
            await pypi.aclose()
            await llm.aclose()

    raise typer.Exit(asyncio.run(run()))


@hidden_app.command("show")
def hidden_show(
    target: Annotated[str, typer.Argument(help="owner/name#issue 编号")],
    db_url: Annotated[str | None, typer.Option("--db", help="数据库 URL，默认读配置")] = None,
) -> None:
    """（维护者本地查看）打印 #N 当前考卷的隐藏题和收据，并核对哈希。不要贴到公开的地方。"""
    from failgate.verify.receipt import check_receipt
    from failgate.verify.store import latest_exam

    repo, number = _issue_target(target)
    settings = Settings()

    async def run() -> int:
        db = Database(db_url or settings.failgate_db_url)
        try:
            async with db.session() as s:
                exam = await latest_exam(s, repo, number)
        finally:
            await db.dispose()
        if exam is None or exam.hidden is None:
            typer.echo(f"{repo}#{number} 没有隐藏考卷", err=True)
            return 2
        h = exam.hidden
        typer.echo(json.dumps(h.receipt, ensure_ascii=False, indent=2, sort_keys=True))
        typer.echo(f"\n# {h.test_path}（{len(h.tests)} 道）\n")
        typer.echo(h.code)
        problems = check_receipt(h.receipt, h.code)
        for p in problems:
            typer.echo(f"❌ {p}")
        return 1 if problems else 0

    raise typer.Exit(asyncio.run(run()))


sandbox_app = typer.Typer(help="复现沙箱：自检、清理", no_args_is_help=True)
app.add_typer(sandbox_app, name="sandbox")


def build_sandbox(settings: Settings) -> DockerSandbox:
    return DockerSandbox.from_settings(settings)


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
    hours: Annotated[
        float, typer.Option(help="删除超过多少小时的已退出沙箱容器和工作区卷")
    ] = 24.0,
) -> None:
    """按 TTL 清理残留的沙箱容器和工作区卷（正常情况下 Case 结束时就会删除）。

    先删已退出的 exec 容器（运行中的不动），再删卷；仍被占用而删不掉的卷单独列出。
    """
    result = asyncio.run(build_sandbox(Settings()).prune_workspaces(hours * 3600))
    typer.echo(f"删除了 {len(result.containers)} 个已退出的沙箱容器")
    typer.echo(f"删除了 {len(result.volumes)} 个工作区卷")
    if result.failed:
        typer.echo(f"{len(result.failed)} 项删除失败：")
        for name, reason in result.failed:
            typer.echo(f"  {name}: {reason}")
        raise typer.Exit(1)


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
        _require_docker(settings)
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
            judge_model=s.llm_model_judge or None,
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
    rule = None if numbers else _selection(repo)
    started = datetime.now()
    run_id = f"{repo.replace('/', '__')}__repro__{started:%Y%m%d-%H%M}"
    run_path, report_path = _run_paths(run_id)
    selection = (
        f"指定编号 {numbers}" if numbers else
        f"{since} 之后创建、以完成状态关闭，{rule.describe(repro=True) if rule else ''}，"
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
            assert rule is not None  # 没指定编号时已经读过规则
            wanted = [d.number for d in select_issues(
                all_docs, rule=rule, since=datetime.fromisoformat(since), limit=limit,
                offset=offset,
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
                    code_changed=_code_changed(gh, repo, trees),
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
        _require_docker(settings)
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
            artifacts_dir=Path(s.sandbox_artifacts_dir), judge_model=s.llm_model_judge or None,
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
) -> Path:
    """L2 回放：Agent 在 issue 时的代码上写仓库内的失败测试，再用严格 FB/PA 检验。会花钱。

    选样规则和 replay repro 相同；需要 GITHUB_TOKEN 和 Docker。
    """
    from failgate.replay import fbpa
    from failgate.replay import l2 as l2replay
    from failgate.replay.repro import select_issues
    from failgate.repro.agent import TEST_PROMPT_VERSION
    from failgate.repro.config import PackageConfig
    from failgate.repro.source import SourceTree

    settings = Settings()
    if not settings.github_token:
        raise typer.BadParameter("需要 GITHUB_TOKEN（查修复提交用 GraphQL）")
    cfg = PackageConfig(name=package, import_name=import_name)
    rule = None if numbers else _selection(repo)
    started = datetime.now()
    run_id = f"{repo.replace('/', '__')}__l2__{started:%Y%m%d-%H%M}"
    run_path, report_path = _run_paths(run_id)
    selection = (
        f"指定编号 {numbers}" if numbers else
        f"和 replay repro 相同的规则：{since} 之后创建、以完成状态关闭，"
        f"{rule.describe(repro=True) if rule else ''}，"
        f"按编号从新到旧跳过 {offset} 个、取 {limit} 个"
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
            assert rule is not None  # 没指定编号时已经读过规则
            wanted = [d.number for d in select_issues(
                all_docs, rule=rule, since=datetime.fromisoformat(since), limit=limit,
                offset=offset,
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
                case = await _l2_fbpa(rt, repo, cfg, report, trees, runs)
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
    return run_path


async def _l2_fbpa(rt: Any, repo: str, cfg: Any, report: Any, trees: dict[str, Any],
                   runs: int) -> Any:
    """一个 L2 报告的严格 FB/PA（replay l2 和 replay l2-fbpa 共用）。"""
    import httpx

    from failgate.platforms.github_rest import GraphQLError
    from failgate.replay import fbpa
    from failgate.replay.fixes import FixCommit, find_fix
    from failgate.repro.l2 import L2Unsupported
    from failgate.repro.package import SETUP_ERRORS
    from failgate.repro.sandbox import ExecResult
    from failgate.repro.source import SourceError, fetch_github_tree

    pin, src_version = report.source.pytest, report.source.version

    async def pretend(fix: FixCommit) -> str:
        return src_version or "0.0.0.dev0"

    async def run_at(sha: str, python: str, version: str, code: str) -> list[ExecResult]:
        # 修复前后用和 L2 时同一套 Python、pytest、伪版本号：只让代码变化
        if sha not in trees:
            trees[sha] = await fetch_github_tree(rt.gh, repo, sha)
        prepared = await rt.tester.prepare(
            cfg, trees[sha], number=report.number, python=python, version=version, pytest=pin,
        )
        return [await rt.tester.run_once(prepared, code) for _ in range(runs)]

    return await fbpa.evaluate_case(
        fbpa.candidate_from_l2(report),
        find_fix=lambda n: find_fix(rt.gh, repo, n),
        pretend=pretend, run_at=run_at,
        code_changed=_code_changed(rt.gh, repo, trees),
        setup_errors=(*SETUP_ERRORS, SourceError, L2Unsupported, GraphQLError, httpx.HTTPError),
    )


@replay_app.command("l2-fbpa")
def replay_l2_fbpa(
    from_run: Annotated[Path, typer.Option("--from", help="replay l2 的回放记录 JSON")],
    package: Annotated[str, typer.Option(help="PyPI 包名（和当初 replay l2 一样）")],
    import_name: Annotated[str | None, typer.Option(help="import 名，默认由包名推出")] = None,
    numbers: Annotated[str | None, typer.Option(
        help="逗号分隔的编号；默认只重跑 setup_failed 的（网络 / 环境出错）")] = None,
    runs: Annotated[int, typer.Option(help="每个版本跑几次")] = 2,
) -> None:
    """只重跑严格 FB/PA，不重跑出题 Agent（$0）：L2 测试用回放记录里的。

    用于环境 / 网络出错后补跑，或判定规则变了之后重算（ADR 0040 补充）。写成新的记录
    `<原名>-refbpa.json`，原记录不动；报告里写明重跑了哪些、原来的结论是什么。
    需要 GITHUB_TOKEN 和 Docker。
    """
    from failgate.replay import l2 as l2replay
    from failgate.repro.config import PackageConfig

    settings = Settings()
    if not settings.github_token:
        raise typer.BadParameter("需要 GITHUB_TOKEN（查修复提交用 GraphQL）")
    reports, cases, meta = l2replay.load(from_run)
    repo = reports[0].repo if reports else ""
    wanted = ({int(x) for x in numbers.split(",") if x.strip()} if numbers
              else {c.number for c in cases if c.outcome == "setup_failed"})
    if not wanted:
        typer.echo("没有要重跑的（没有 setup_failed）")
        return
    cfg = PackageConfig(name=package, import_name=import_name)
    out_run = from_run.with_name(from_run.stem + "-refbpa.json")
    out_report = Path("eval/reports") / (from_run.stem + "-refbpa.md")

    async def run() -> None:
        rt = _L2Runtime(settings)
        trees: dict[str, Any] = {}
        redone: list[str] = []
        try:
            by_number = {r.number: r for r in reports}
            for i, c in enumerate(cases):
                if c.number not in wanted:
                    continue
                new = await _l2_fbpa(rt, repo, cfg, by_number[c.number], trees, runs)
                redone.append(f"#{c.number}：{c.outcome} → {new.outcome}")
                typer.echo(f"#{c.number}：{c.outcome} → {new.outcome}")
                cases[i] = new
        finally:
            await rt.aclose()
        meta2 = {**meta, "refbpa": {
            "from": from_run.as_posix(), "at": datetime.now().isoformat(timespec="seconds"),
            "redone": redone}}
        out_run.write_text(l2replay.dump(reports, cases, meta2), encoding="utf-8")
        out_report.write_text(l2replay.render(repo, reports, cases, meta2), encoding="utf-8")
        sm = l2replay.summarize(reports, cases)
        typer.echo(f"\nL2 {sm['l2']}/{sm['n']}；L2 测试严格 FB/PA "
                   f"{sm['fb_pa']}/{sm['fbpa_eligible']}\n记录：{out_run}\n报告：{out_report}")

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


@replay_app.command("verify")
def replay_verify(
    repo: Annotated[str, typer.Argument(help="owner/name，例如 psf/black")],
    source: Annotated[Path, typer.Option("--from", help="replay l2 的运行记录 JSON")],
    kinds: Annotated[str, typer.Option(help="逗号分隔的变体")] = ",".join(
        ("fix", "revert_code", "exam_skip", "conftest_skip", "unrelated", "break_other")),
    only: Annotated[str | None, typer.Option(help="逗号分隔的 issue 编号（调试用）")] = None,
    resume: Annotated[
        Path | None, typer.Option(help="接着一份没跑完的结果（.jsonl）继续，跳过已完成的")
    ] = None,
    strength: Annotated[bool, typer.Option(
        "--strength", help="第一层通过的案例顺带算考卷强度（变异测试，ADR 0020）")] = False,
    repo_config: Annotated[bool, typer.Option(
        "--repo-config/--no-repo-config",
        help="读 eval/datasets/<repo>/verify.json（第三层总要跑的测试）；关掉用来做对照")] = True,
) -> Path:
    """ClaimVerify 正负例评测：上游真实修复当正例，程序构造的 4 种作弊当负例（ADR 0019）。

    需要 Docker 和 GITHUB_TOKEN；不花 LLM 的钱。每个案例要建两个源码环境，几十秒到几分钟。
    """
    import httpx

    from failgate.platforms.github_rest import GitHubRest
    from failgate.replay import verify_eval as ve
    from failgate.repro.l2 import TestReproducer
    from failgate.repro.pypi import PyPIClient
    from failgate.repro.source import fetch_github_tree
    from failgate.verify.workbench import SandboxWorkbench

    settings = Settings()
    variants = [k.strip() for k in kinds.split(",") if k.strip()]
    if bad := [k for k in variants if k not in ve.VARIANTS]:
        raise typer.BadParameter(f"未知的变体：{bad}，可选 {ve.VARIANTS}")
    _require_docker(settings)
    run = json.loads(source.read_text(encoding="utf-8"))
    cases = ve.load_cases(run)
    if only:
        wanted = {int(x) for x in only.split(",")}
        cases = [c for c in cases if c.number in wanted]
    started = datetime.now()
    stem = resume.stem if resume else f"{repo.replace('/', '__')}__verify__{started:%Y%m%d-%H%M}"
    jsonl = resume or Path("eval/runs") / f"{stem}.jsonl"
    report_path = Path("eval/reports") / f"{stem}.md"
    vcfg = ve.load_verify_config(repo) if repo_config else ve.VerifyConfig()
    related_always, test_deps = vcfg.related_always, vcfg.test_deps
    meta = {"repo": repo, "started": started.isoformat(timespec="seconds"),
            "source": source.as_posix(), "related_always": related_always,
            "test_deps": test_deps}

    async def run_all() -> None:
        gh = GitHubRest(settings.github_token)
        pypi = PyPIClient(settings.pypi_url)
        sandbox = build_sandbox(settings)
        tester = TestReproducer(sandbox, _env_cache(settings, sandbox), pypi,
                                run_timeout_s=settings.sandbox_run_timeout_seconds)

        async def retrying(make: Any) -> Any:
            # 评测一跑几十分钟，GitHub 偶尔连不上不该让整个评测退出（实测遇到过 ConnectError）
            for attempt in range(3):
                try:
                    return await make()
                except httpx.TransportError:
                    if attempt == 2:
                        raise
                    typer.echo(f"  网络错误，{5 * (attempt + 1)} 秒后重试", err=True)
                    await asyncio.sleep(5 * (attempt + 1))

        async def fetch(r: str, sha: str) -> Any:
            return await retrying(lambda: fetch_github_tree(gh, r, sha))

        async def compare(r: str, base: str, head: str) -> Any:
            return await retrying(lambda: gh.compare_files(r, base, head))

        results = ve.load_done(jsonl)
        done = {(r["number"], r["kind"]) for r in results}
        jsonl.parent.mkdir(parents=True, exist_ok=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            for case in cases:
                trees: dict[str, Any] = {}
                for kind in variants:
                    if (case.number, kind) in done:
                        continue
                    typer.echo(f"#{case.number} {kind} …")
                    res = await ve.run_case(
                        repo, case, kind, fetch=fetch, compare=compare,
                        bench_for=lambda f: SandboxWorkbench(f, tester, test_deps),
                        trees=trees, strength=strength, related_always=related_always,
                    )
                    results.append(res)
                    with jsonl.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(res, ensure_ascii=False) + "\n")
                    if res.get("skipped"):
                        typer.echo(f"  → n/a（{res['skipped']}），不计入")
                        continue
                    mark = "✅" if res["correct"] else "❌"
                    st = res.get("strength") or {}
                    extra = (f" · 强度 {st['grade']} {st['killed']}/{st['killed'] + st['survived']}"
                             if st.get("status") == "ok" else
                             f" · 强度 n/a（{st['reason']}）" if st else "")
                    typer.echo(f"  → {res['verdict']} {mark} {', '.join(res['reasons'])}"
                               f" · {res['seconds']}s{extra}")
                    report_path.write_text(ve.render(results, meta), encoding="utf-8")
        finally:
            await gh.aclose()
            await pypi.aclose()
        s = ve.summarize(results)
        typer.echo(f"\n准确率 {s['correct']}/{s['n']}；报告：{report_path}")

    asyncio.run(run_all())
    return jsonl


CALIB_INSTANCES = Path("eval/datasets/swebench_utboost/instances.json")


@replay_app.command("calib-run")
def replay_calib_run(
    only: Annotated[str, typer.Option(help="逗号分隔的 instance_id")],
    out: Annotated[Path, typer.Option(help="每个题一个 JSON 写到这个目录")] = Path("calib-out"),
    instances: Annotated[Path, typer.Option(help="题目数据")] = CALIB_INSTANCES,
    max_mutants: Annotated[int, typer.Option(help="每个题最多多少个变异体")] = 30,
) -> None:
    """考卷强度的外部校准（ADR 0022）：在 SWE-bench 镜像里对官方考卷和 UTBoost 考卷算强度。

    设计为在 GitHub Actions 里运行（需要 Docker、`pip install swebench==4.1.0`、能拉镜像）。"""
    from failgate.replay import swebench_strength as ss

    wanted = [x.strip() for x in only.split(",") if x.strip()]
    todo = [i for i in ss.load_instances(instances) if i.instance_id in wanted]
    missing = set(wanted) - {i.instance_id for i in todo}
    if missing:
        raise typer.BadParameter(f"数据里没有：{sorted(missing)}")
    sweb = ss.SweBench.load()
    out.mkdir(parents=True, exist_ok=True)
    for inst in todo:
        typer.echo(f"{inst.instance_id}（{inst.group}）…")
        row = ss.run_instance(inst, sweb, max_mutants=max_mutants)
        (out / f"{inst.instance_id}.json").write_text(
            json.dumps(row, ensure_ascii=False, indent=1), encoding="utf-8")
        conds = row.get("conditions", {})
        rates = "、".join(f"{k} {v.get('kill_rate')}" for k, v in conds.items())
        typer.echo(f"  → {row['status']} · {rates} · {row.get('seconds')}s")


@replay_app.command("calib-report")
def replay_calib_report(
    results: Annotated[Path, typer.Argument(help="calib-run 输出的 JSON 所在目录（可以有子目录）")],
    out: Annotated[Path | None, typer.Option(help="报告写到这里，默认 eval/reports/")] = None,
    run: Annotated[str, typer.Option(help="GitHub Actions 运行编号或链接")] = "",
) -> None:
    """汇总 calib-run 的结果，写报告。"""
    from failgate.replay import swebench_strength as ss

    rows = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(results.rglob("*.json"))]
    rows = [r for r in rows if "instance_id" in r]
    started = datetime.now()
    meta = {"started": started.isoformat(timespec="seconds"), "run": run or "—"}
    path = out or Path("eval/reports") / f"swebench__strength_calib__{started:%Y%m%d-%H%M}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ss.render(rows, meta), encoding="utf-8")
    s = ss.summarize(rows)
    typer.echo(f"{s['ok']}/{s['n']} 个题算出来了；成对：上升 {s['up']}、下降 {s['down']}、"
               f"不变 {s['tie']}（p={s['sign_p']:.3g}）；报告：{path}")


@replay_app.command("hidden")
def replay_hidden(
    repo: Annotated[str, typer.Argument(help="owner/name")],
    source: Annotated[Path, typer.Option("--from", help="replay l2 的运行记录（JSON）")],
    db_url: Annotated[str, typer.Option("--db", help="回放语料库（取 issue 正文）")] = REPLAY_DB,
    only: Annotated[str | None, typer.Option(help="逗号分隔的 issue 编号（调试用）")] = None,
) -> None:
    """隐藏考卷误报评测（ADR 0021）：在修复的父提交上出题挑题，再在上游修复上跑。

    需要 Docker、GITHUB_TOKEN 和 LLM；每个案例一次出题（约 $0.01）和三次环境。"""
    from failgate.app import build_llm
    from failgate.platforms.github_rest import GitHubRest
    from failgate.replay import hidden_eval as he
    from failgate.replay import verify_eval as ve
    from failgate.repro.l2 import TestReproducer
    from failgate.repro.pypi import PyPIClient
    from failgate.verify.hidden import HiddenWriter
    from failgate.verify.workbench import SandboxWorkbench

    settings = Settings()
    cases = ve.load_cases(json.loads(source.read_text(encoding="utf-8")))
    if only:
        wanted = {int(x) for x in only.split(",")}
        cases = [c for c in cases if c.number in wanted]
    started = datetime.now()
    stem = f"{repo.replace('/', '__')}__hidden__{started:%Y%m%d-%H%M}"
    jsonl = Path("eval/runs") / f"{stem}.jsonl"
    report_path = Path("eval/reports") / f"{stem}.md"
    meta = {"repo": repo, "started": started.isoformat(timespec="seconds"),
            "source": source.as_posix(), "model": settings.llm_model_large}

    async def run_all() -> None:
        llm = build_llm(settings)
        if llm is None:
            raise typer.BadParameter("没有配置 LLM（LLM_API_KEY）")
        docs = {d.number: d for d in await _load_issue_docs(db_url, repo,
                                                             [c.number for c in cases])}
        gh = GitHubRest(settings.github_token)
        pypi = PyPIClient(settings.pypi_url)
        sandbox = build_sandbox(settings)
        tester = TestReproducer(sandbox, _env_cache(settings, sandbox), pypi,
                                run_timeout_s=settings.sandbox_run_timeout_seconds)
        bench = SandboxWorkbench.for_github(gh, tester)
        writer = HiddenWriter(llm, settings.llm_model_large)
        rows: list[dict[str, Any]] = []
        jsonl.parent.mkdir(parents=True, exist_ok=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            for case in cases:
                doc = docs.get(case.number)
                if doc is None:
                    typer.echo(f"#{case.number}：回放库里没有这个 issue，跳过", err=True)
                    continue
                typer.echo(f"#{case.number} …")
                row = await he.run_case(repo, case, title=doc.title, body=doc.body,
                                        bench=bench, writer=writer)
                rows.append(row)
                with jsonl.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                on_fix = row.get("on_fix") or {}
                typer.echo(f"  → 出题 {row['generated']}、留下 {row['kept']}"
                           + (f"；上游修复 {on_fix.get('passed')}/{on_fix.get('total')} 通过"
                              if on_fix else f"（{row['reason']}）")
                           + f" · ${row['cost_usd']} · {row['seconds']}s")
                report_path.write_text(he.render(rows, meta), encoding="utf-8")
        finally:
            await gh.aclose()
            await pypi.aclose()
            await llm.aclose()
        s = he.summarize(rows)
        typer.echo(f"\n误报 {s['false_alarm']}/{s['ran']}；报告：{report_path}")

    asyncio.run(run_all())


if __name__ == "__main__":
    app()


@replay_app.command("latency")
def replay_latency(
    repo: Annotated[str, typer.Argument(help="owner/name")],
    since: Annotated[str, typer.Option(help="开始时间（ISO 8601，本机时间或带时区）")],
    until: Annotated[str | None, typer.Option(help="结束时间（默认到现在）")] = None,
    title: Annotated[str, typer.Option(help="报告标题")] = "排队延迟测量",
    label: Annotated[str, typer.Option(help="报告文件名里的标签，如 before / after")] = "run",
    github: Annotated[bool, typer.Option(help="用 GitHub 时间戳补上触发 → bot 评论的延迟")] = True,
    bot: Annotated[str | None, typer.Option(help="bot 的登录名（默认任何 [bot]）")] = None,
    db_url: Annotated[str | None, typer.Option("--db", help="数据库连接串，默认用配置")] = None,
) -> None:
    """排队延迟（W6）：deliveries 表的排队 / 处理时间 + GitHub 上的触发到评论时间。"""
    from failgate.platforms.github_rest import GitHubRest
    from failgate.replay import latency as lat

    settings = Settings()

    def parse(ts: str) -> datetime:
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.astimezone()

    start = parse(since).astimezone(UTC)
    end = parse(until).astimezone(UTC) if until else None

    async def run() -> list[lat.EventTiming]:
        db = Database(db_url or settings.failgate_db_url)
        await db.create_all()
        try:
            rows = await lat.load_timings(db, repo, start.replace(tzinfo=None),
                                          end.replace(tzinfo=None) if end else None)
        finally:
            await db.dispose()
        if github and rows:
            rest = GitHubRest(settings.github_token)
            try:
                await lat.attach_github(rows, repo, rest, bot)
            finally:
                await rest.aclose()
        return rows

    rows = asyncio.run(run())
    summary = lat.summarize(rows)
    stem = f"{repo.replace('/', '__')}__latency-{label}__{datetime.now():%Y%m%d-%H%M}"
    report = Path("eval/reports") / f"{stem}.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    notes = [f"窗口：{start.isoformat(timespec='seconds')} 起"
             + (f"，到 {end.isoformat(timespec='seconds')}" if end else "") + "（UTC）。"]
    report.write_text(lat.render(rows, summary, repo=repo, title=title, notes=notes),
                      encoding="utf-8")
    typer.echo(json.dumps(summary, ensure_ascii=False, indent=1))
    typer.echo(f"报告：{report.as_posix()}")


@replay_app.command("load")
def replay_load(
    repo: Annotated[str, typer.Argument(help="owner/name（必须是影子模式）")],
    maintainer: Annotated[str, typer.Option(help="发命令的维护者登录名（会实时查权限）")],
    pulls: Annotated[str, typer.Option(help="PR 编号，逗号分隔；空 = 只发快 issue")] = "",
    fast: Annotated[int, typer.Option(help="快 issue 个数")] = 20,
    first_at: Annotated[float, typer.Option(help="第一个快 issue 的时间（秒）")] = 5.0,
    interval: Annotated[float, typer.Option(help="快 issue 的间隔（秒）")] = 6.0,
    base: Annotated[int, typer.Option(help="快 issue 的起始编号（用不存在的编号）")] = 90001,
    url: Annotated[str, typer.Option(help="服务地址")] = "http://127.0.0.1:8081/webhooks/github",
    db_url: Annotated[str | None, typer.Option("--db", help="服务用的库（查影子模式）")] = None,
) -> None:
    """合成负载（W6）：按固定剧本往本地服务发签名 webhook，之后用 replay latency 出报告。"""
    from failgate.replay import loadgen

    settings = Settings()

    async def check() -> int | None:
        db = Database(db_url or settings.failgate_db_url)
        try:
            async with db.session() as s:
                row = (await s.execute(select(Repo).where(Repo.full_name == repo))).scalar_one()
        finally:
            await db.dispose()
        if row.mode != "shadow":
            raise typer.BadParameter(f"{repo} 是 {row.mode} 模式：合成负载只能对影子模式的仓库用")
        return row.installation_id

    installation = asyncio.run(check())
    plan = loadgen.mixed_scenario(
        repo, pulls=[int(x) for x in pulls.split(",") if x.strip()],
        fast=fast, first_fast_at=first_at,
        interval=interval, base=base, maintainer=maintainer, installation=installation)
    started = datetime.now(UTC)
    typer.echo(f"开始（UTC）：{started.isoformat(timespec='seconds')}，{len(plan)} 个事件")
    codes = asyncio.run(loadgen.send_plan(plan, url, settings.github_webhook_secret,
                                          echo=typer.echo))
    typer.echo(f"已发出：{sum(c == 202 for c in codes)}/{len(codes)} 入队。"
               f"处理完后运行：failgate replay latency {repo} --since "
               f"{started.isoformat(timespec='seconds')} --no-github --db <同一个库>")


trace_app = typer.Typer(help="链路追踪：本地收 OTLP、打成树（ADR 0025）", no_args_is_help=True)
app.add_typer(trace_app, name="trace")


@trace_app.command("sink")
def trace_sink(
    out: Annotated[Path, typer.Argument(help="收到的 span 按 JSON 行追加到这个文件")],
    port: Annotated[int, typer.Option(help="监听端口（OTLP/HTTP 默认 4318）")] = 4318,
) -> None:
    """本地 OTLP/HTTP 接收器：服务端设 TRACING_EXPORTER=otlp、OTLP_ENDPOINT=http://127.0.0.1:4318/v1/traces。"""
    from failgate.tracing.sink import serve

    typer.echo(f"监听 127.0.0.1:{port}，写入 {out}（Ctrl+C 退出）")
    serve(out, port=port)


@trace_app.command("show")
def trace_show(
    path: Annotated[Path, typer.Argument(help="trace sink 写的 JSON 行文件")],
    hide: Annotated[str, typer.Option(help="只计数、不逐行显示的 span 名（逗号分隔）")] = (
        "sandbox run"),
) -> None:
    """把收到的 span 按 trace 打成树。"""
    from failgate.tracing.sink import render

    spans = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    typer.echo(render(spans, hide=tuple(h.strip() for h in hide.split(",") if h.strip())))


@trace_app.command("open")
def trace_open(
    open_browser: Annotated[bool, typer.Option("--open/--no-open", help="打开浏览器")] = True,
) -> None:
    """在浏览器打开 Langfuse 的链路页面（按 .env 里的 LANGFUSE_*）；没配时说明怎么在本地看。"""
    import webbrowser

    import httpx

    s = Settings()
    local = t("用本地接收器看：另开终端 failgate trace sink spans.jsonl，服务端设 "
              "TRACING_EXPORTER=otlp、OTLP_ENDPOINT=http://127.0.0.1:4318/v1/traces，"
              "之后 failgate trace show spans.jsonl",
              "View locally: run failgate trace sink spans.jsonl in another terminal, set "
              "TRACING_EXPORTER=otlp and OTLP_ENDPOINT=http://127.0.0.1:4318/v1/traces on the "
              "server, then failgate trace show spans.jsonl")
    if s.tracing_exporter == "none":
        typer.echo(t("提示：TRACING_EXPORTER=none，服务现在不发链路（要看新的链路先改成 otlp）",
                     "note: TRACING_EXPORTER=none, the server sends no traces now "
                     "(set it to otlp for new ones)"))
    host = s.langfuse_host.rstrip("/")
    if s.otlp_endpoint and not s.otlp_endpoint.startswith(host):
        typer.echo(t(f"链路发到 {s.otlp_endpoint}（不是 Langfuse）。",
                     f"traces go to {s.otlp_endpoint} (not Langfuse). ") + local)
        raise typer.Exit(1)
    if not (s.langfuse_public_key and s.langfuse_secret_key):
        typer.echo(t("没有配置 Langfuse（LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY）。",
                     "Langfuse is not configured (LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY). ")
                   + local)
        raise typer.Exit(1)
    url = host
    try:  # 用 key 查项目 id，直接打开这个项目的链路列表；查不到就打开首页
        resp = httpx.get(f"{host}/api/public/projects", timeout=5.0,
                         auth=(s.langfuse_public_key, s.langfuse_secret_key))
        resp.raise_for_status()
        projects = resp.json().get("data") or []
        if projects:
            url = f"{host}/project/{projects[0]['id']}/traces"
            name = projects[0].get("name", projects[0]["id"])
            typer.echo(t("Langfuse 项目：", "Langfuse project: ") + str(name))
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        typer.echo(t(f"没查到项目（{type(exc).__name__}），打开 Langfuse 首页",
                     f"project lookup failed ({type(exc).__name__}); opening the Langfuse home"))
    typer.echo(t("链路：", "Traces: ") + url)
    if open_browser:
        webbrowser.open(url)


llm_app = typer.Typer(help="多模型网关（ADR 0034）", no_args_is_help=True)
app.add_typer(llm_app, name="llm")


@llm_app.command("routes")
def llm_routes() -> None:
    """各角色用哪个厂商的哪个模型、厂商配置和 key 在不在。不发任何请求、不打印 key。"""
    from urllib.parse import urlparse

    from failgate.llm.gateway import DEFAULT_PROVIDER, key_env, parse_providers, parse_spec

    s = Settings()
    env = key_env()
    try:
        providers = parse_providers(s.llm_providers)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e
    host = urlparse(s.llm_base_url).netloc
    typer.echo("厂商：")
    typer.echo(f"  {DEFAULT_PROVIDER}（默认）  openai  {host}  "
               f"key：{'有' if s.llm_api_key else '没有（LLM_API_KEY）'}")
    for name, pc in sorted(providers.items()):
        where = urlparse(pc.base_url).netloc if pc.base_url else "litellm 默认地址"
        has = bool(env.get(pc.api_key_env))
        typer.echo(f"  {name}  {pc.backend}  {where}  key：{'有' if has else '没有'}"
                   f"（{pc.api_key_env}）  思考参数：{'传' if pc.thinking else '不传'}")
    if not providers:
        typer.echo("  （没有配置 LLM_PROVIDERS：直连默认厂商，不经过网关）")
    typer.echo("角色：")
    problems = []
    for role, spec in (("small（Intake、分诊、查重）", s.llm_model_small),
                       ("large（答疑、复现 / 修复 Agent、出题）", s.llm_model_large),
                       ("judge（复现评委）", s.llm_model_judge or s.llm_model_large)):
        ms = parse_spec(spec)
        known = ms.provider == DEFAULT_PROVIDER or ms.provider in providers
        if not known:
            problems.append(f"{role} 用的厂商 {ms.provider} 没有配置")
        typer.echo(f"  {role}：{ms.provider} / {ms.model}"
                   + ("" if known else "  ← 没有配置这个厂商"))
    if ":" in (s.llm_model_small + s.llm_model_large + s.llm_model_judge) and not providers:
        problems.append("模型名带了 厂商: 前缀，但没有配置 LLM_PROVIDERS，网关不会启用")
    for p in problems:
        typer.echo(f"问题：{p}", err=True)
    if problems:
        raise typer.Exit(1)


@app.command("mcp")
def mcp_serve(
    env_file: Annotated[Path | None, typer.Option(
        help="FailGate 的 .env（LLM、沙箱配置）。客户端会在任意目录启动本命令，"
             "所以要指定；相对路径（artifacts、环境缓存）以它所在的目录为准")] = None,
    home: Annotated[Path | None, typer.Option(
        help="证据存放目录，默认 ~/.failgate（也可用 FAILGATE_HOME）")] = None,
) -> None:
    """本地 stdio MCP 服务（ADR 0033）：给 Claude Code / Cursor 用的出题、跑考卷、核验工具。

    标准输出是协议通道：这个命令不往 stdout 打印任何东西，日志走 stderr。
    """
    import logging
    import os
    import sys

    from failgate.mcp_server.engine import FailGateEngine, Runtime
    from failgate.mcp_server.server import build_server
    from failgate.mcp_server.store import EvidenceStore

    if _stdin_is_terminal():
        # 人在终端里直接敲的（或在交互模式里）：stdio 服务会一直等协议输入，看起来像卡住了。
        # 客户端启动时 stdin 是管道，不会走到这里
        _mcp_how_to_register(env_file)
        raise typer.Exit(1)
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    if env_file is not None:
        env_file = env_file.expanduser().resolve()
        if not env_file.is_file():
            raise typer.BadParameter(f"{env_file} 不存在")
        os.chdir(env_file.parent)
    settings = Settings(_env_file=env_file) if env_file else Settings()  # type: ignore[call-arg]
    store = EvidenceStore(home.expanduser() if home else None)
    engine = FailGateEngine(Runtime.from_settings(settings), store)
    build_server(engine, store).run("stdio")


def _stdin_is_terminal() -> bool:
    return sys.stdin.isatty()


def _mcp_how_to_register(env_file: Path | None) -> None:
    from failgate.settings import active_env_file

    env = (env_file or Path(active_env_file())).expanduser().resolve().as_posix()
    python = Path(sys.executable).as_posix()
    args = f'["-m", "failgate", "mcp", "--env-file", "{env}"]'
    typer.echo(t("failgate mcp 是给 Claude Code / Cursor 这类客户端启动的 stdio 服务，"
                 "不用手动运行（在终端里运行会一直等协议输入）。\n",
                 "failgate mcp is a stdio server launched by clients such as Claude Code / "
                 "Cursor; don't run it by hand (in a terminal it just waits for protocol "
                 "input).\n"))
    typer.echo(t("注册到 Claude Code（在你要修的仓库目录里运行）：",
                 "Register with Claude Code (run inside the repo you are fixing):"))
    typer.echo(f"  claude mcp add failgate -- {python} -m failgate mcp --env-file {env}\n")
    typer.echo(t(f"Cursor 等客户端：command 填上面的 python 路径，args 填 {args}",
                 f"Cursor and others: command = the python path above, args = {args}"))
    typer.echo(t("提供的工具：", "Tools: ") + "reproduce_issue, run_acceptance_test, verify_fix, "
               "get_fix_task, get_job, list_evidence (ADR 0033)")


memory_app = typer.Typer(help="Agent 的长期记忆：情景记忆（ADR 0032）", no_args_is_help=True)
app.add_typer(memory_app, name="memory")


@memory_app.command("build")
def memory_build(
    repo: Annotated[str, typer.Argument(help="owner/name，例如 psf/black")],
    out: Annotated[Path | None, typer.Option(help="输出 .jsonl")] = None,
    concurrency: Annotated[int, typer.Option(help="同时取几个 PR 的 diff")] = 4,
) -> None:
    """从 GitHub 拉这个仓库合并过的、改了源码的 PR，存成情景记忆（不调 LLM，需要 GITHUB_TOKEN）。"""
    from failgate.memory.build import fetch_episodes
    from failgate.memory.episodic import EpisodicMemory
    from failgate.platforms.github_rest import GitHubRest

    settings = Settings()
    path = out or Path("eval/cache/memory") / f"{repo.replace('/', '__')}.jsonl"

    async def run() -> None:
        gh = GitHubRest(settings.github_token)
        try:
            eps = await fetch_episodes(gh, repo, concurrency=concurrency, progress=typer.echo)
        finally:
            await gh.aclose()
        EpisodicMemory.dump(eps, path)
        with_issue = sum(bool(e.issues) for e in eps)
        typer.echo(f"{len(eps)} 条情景（其中 {with_issue} 条关闭了 issue）→ {path}")

    asyncio.run(run())


@memory_app.command("show")
def memory_show(
    query: Annotated[str, typer.Argument(help="要找的修改（自然语言或函数名）")],
    before: Annotated[str, typer.Option(help="只看这个时间之前合并的（ISO 日期）")],
    path: Annotated[str | None, typer.Option(help="只看改过这个文件的")] = None,
    k: Annotated[int, typer.Option(help="返回几条")] = 3,
    memory: Annotated[Path, typer.Option(help="情景记忆 .jsonl")] = Path(
        "eval/cache/memory/psf__black.jsonl"),
    patch: Annotated[bool, typer.Option(help="带 diff")] = True,
) -> None:
    """按修复 Agent 看到的样子打印检索结果（检查记忆内容、调检索用）。"""
    from failgate.memory.episodic import EpisodicMemory, render

    mem = EpisodicMemory.load(memory)
    cutoff = datetime.fromisoformat(before)
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=UTC)
    typer.echo(f"{before} 之前可见 {mem.visible(cutoff)} / {len(mem)} 条")
    typer.echo(render(mem.recall(query, before=cutoff, path=path, k=k), with_patch=patch))


fix_app = typer.Typer(help="修复 Agent：LangGraph 规划 → 修改 → 验收 → 反思（ADR 0027）",
                      no_args_is_help=True)
app.add_typer(fix_app, name="fix")


@fix_app.command("run")
def fix_run(
    repo: Annotated[str, typer.Argument(help="owner/name，例如 psf/black")],
    number: Annotated[int, typer.Argument(help="issue 编号（必须在 --from 的记录里）")],
    source: Annotated[Path, typer.Option("--from", help="replay l2 的运行记录 JSON")],
    control: Annotated[bool, typer.Option(
        "--control", help="对照组：不给验收测试（提升实验用）")] = False,
    budget: Annotated[float, typer.Option(help="花费上限（美元）")] = 0.5,
    rounds: Annotated[int, typer.Option(help="最多几轮 规划→修改→验收")] = 3,
    thinking: Annotated[str | None, typer.Option(
        help="思考模式 disabled / low / high / max，不填用服务方默认")] = None,
    db_url: Annotated[str, typer.Option("--db", help="回放语料库（取 issue 正文）")] = REPLAY_DB,
    show_patch: Annotated[bool, typer.Option(help="打印补丁")] = True,
    handoff: Annotated[str, typer.Option(
        help="规划 → 修改的交接：reset / notes / continue（ADR 0030）")] = "reset",
    memory: Annotated[Path | None, typer.Option(
        help="带情景记忆（failgate memory build 的输出，ADR 0032）")] = None,
) -> None:
    """在某个 issue 的修复提交的父提交上跑修复 Agent（离线回放的最小单元）。会花钱。

    验收测试就是回放里封存的那份 L2 测试；这里只看它在全新工作区里过没过。
    真正的成功判定（上游金标准测试）在 fix-eval 里做。需要 GITHUB_TOKEN 和 Docker。
    """
    from failgate.fix.agent import HANDOFFS, FixTask
    from failgate.fix.run import fix_tree
    from failgate.home import make_console
    from failgate.memory.episodic import EpisodicMemory
    from failgate.progress import live_progress, progress_console
    from failgate.replay import fix_eval as fe
    from failgate.replay import fixset as fxs
    from failgate.replay import verify_eval as ve
    from failgate.repro.config import PackageConfig
    from failgate.repro.package import IssueContext
    from failgate.repro.source import fetch_github_tree
    from failgate.views import fix_panel

    if handoff not in HANDOFFS:
        raise typer.BadParameter(f"未知的交接方式：{handoff}，可选 {HANDOFFS}")
    settings = Settings()
    run = json.loads(source.read_text(encoding="utf-8"))
    if fxs.is_fixset(run):
        if not control:
            raise typer.BadParameter("评测集 v2 没有考卷，要加 --control")
        all_cases = fxs.eval_cases(fxs.Fixset.model_validate(run))
    else:
        all_cases = ve.load_cases(run)
    cases = [c for c in all_cases if c.number == number]
    if not cases:
        raise typer.BadParameter(f"#{number} 不在记录里，或严格 FB/PA 不成立")
    case = cases[0]
    episodic = EpisodicMemory.load(memory) if memory else None
    cutoff = fe.fix_cutoffs(run).get(number)

    async def go() -> None:
        docs = await _load_issue_docs(db_url, repo, [number])
        if not docs:
            raise typer.BadParameter(f"{repo}#{number} 不在回放库里")
        rt = _L2Runtime(settings)
        try:
            tree = await fetch_github_tree(rt.gh, repo, case.parent)
            cfg = PackageConfig(name=case.exam.package, import_name=case.exam.module)
            task = FixTask(
                repo=repo, number=number,
                issue=IssueContext(title=docs[0].title, body=docs[0].body),
                test_path=case.exam.test_path,
                test_code=None if control else case.exam.code,
                memory_before=cutoff,
                exclude_prs=[case.upstream_pr] if case.upstream_pr else [],
            )
            arm = t("对照组", "control") if control else t("实验组", "treatment")
            title = t(f"修复 {repo}#{number}（{arm}）", f"Fix {repo}#{number} ({arm})")
            with live_progress(title, progress_console()):
                res = await fix_tree(
                    rt.llm, settings.llm_model_large, rt.tester, cfg, tree, task,
                    python=case.exam.python, version=case.exam.version,
                    pytest=case.exam.pytest, max_rounds=rounds, budget_usd=budget,
                    thinking=thinking, artifacts_dir=Path(settings.sandbox_artifacts_dir),
                    handoff=handoff,  # type: ignore[arg-type]
                    memory=episodic,
                )
        finally:
            await rt.aclose()
        con = make_console()
        if con.is_terminal:
            from rich.syntax import Syntax

            con.print(fix_panel(number, res, control=control))
            if show_patch and res.patch:
                con.print(Syntax(res.patch, "diff", theme="ansi_dark", word_wrap=True))
            return
        typer.echo(f"#{number} {'对照组' if control else '实验组'} → {res.status}"
                   f"（{len(res.attempts)} 轮，{res.steps} 步，{res.duration_s} 秒，"
                   f"${res.cost_usd:.4f}，被拒写入 {res.denied} 次）")
        typer.echo(f"  tokens：输入 {res.prompt_tokens}（缓存 {res.cached_tokens}）"
                   f" 输出 {res.completion_tokens}（推理 {res.reasoning_tokens}）")
        typer.echo(f"  改动文件：{', '.join(res.files) or '无'}")
        typer.echo(f"  交接 {res.handoff}：修改阶段读 {res.edit_reads} 次，其中重读 {res.rereads}；"
                   f"第一次编辑在第 {res.first_edit_step} 步")
        if res.error:
            typer.echo(f"  错误：{res.error}")
        if res.give_up_reason:
            typer.echo(f"  放弃：{res.give_up_reason}")
        if res.transcript_path:
            typer.echo(f"  记录：{res.transcript_path}")
        if show_patch and res.patch:
            typer.echo("\n" + res.patch)

    asyncio.run(go())


@replay_app.command("fix")
def replay_fix(
    repo: Annotated[str, typer.Argument(help="owner/name，例如 psf/black")],
    source: Annotated[Path, typer.Option(
        "--from", help="replay l2 的运行记录 JSON，或 replay fixset 生成的评测集")],
    hidden: Annotated[Path, typer.Option(
        help="replay hidden 的结果（.jsonl），没有就不查隐藏考卷")] = Path(
        "eval/runs/psf__black__hidden__20260929-1721.jsonl"),
    reps: Annotated[int, typer.Option(help="每题每组重复几次")] = 3,
    arms: Annotated[str, typer.Option(
        help="逗号分隔：exam（给考卷）/ control（不给），可加 :交接方式（ADR 0030）、"
             "+mem 带情景记忆（ADR 0032），如 exam:notes、control+mem")] = "exam,control",
    only: Annotated[str | None, typer.Option(help="逗号分隔的 issue 编号")] = None,
    resume: Annotated[Path | None, typer.Option(
        help="接着一份没跑完的结果（.jsonl）继续")] = None,
    budget: Annotated[float, typer.Option(help="每次运行的花费上限（美元）")] = 0.15,
    rounds: Annotated[int, typer.Option(help="每次运行最多几轮")] = 2,
    max_cost: Annotated[float, typer.Option(
        help="整个实验的花费上限（美元），到了就停")] = 3.0,
    gold_only: Annotated[bool, typer.Option(
        "--gold-only", help="只算金标准，不跑 Agent（$0）")] = False,
    thinking: Annotated[str | None, typer.Option(help="思考模式，不填用服务方默认")] = None,
    db_url: Annotated[str, typer.Option("--db", help="回放语料库（取 issue 正文）")] = REPLAY_DB,
    concurrency: Annotated[int, typer.Option(help="同时跑几次（每次一个沙箱容器）")] = 3,
    memory: Annotated[Path, typer.Option(
        help="情景记忆（failgate memory build 的输出），+mem 的组才用")] = Path(
        "eval/cache/memory/psf__black.jsonl"),
) -> None:
    """修复 Agent 的提升实验：给考卷 vs 不给考卷，用上游修复自带的测试判成败（ADR 0028）。

    需要 Docker、GITHUB_TOKEN 和 LLM；先算每题的金标准（不花钱），再按 重复 → 题 → 组 的顺序跑，
    中途停下也是两组均衡的。结果逐行写进 .jsonl，--resume 续跑。
    """
    import httpx

    from failgate.fix.agent import FixTask
    from failgate.fix.run import fix_tree
    from failgate.replay import fix_eval as fe
    from failgate.replay import verify_eval as ve
    from failgate.repro.config import PackageConfig
    from failgate.repro.package import IssueContext
    from failgate.repro.source import fetch_github_tree
    from failgate.verify.hidden import hidden_path

    settings = Settings()
    arm_list = [a.strip() for a in arms.split(",") if a.strip()]
    if bad := [a for a in arm_list if not fe.valid_arm(a)]:
        raise typer.BadParameter(
            f"未知的组：{bad}，可选 {fe.ARMS}，可加 :reset / :notes / :continue")
    from failgate.replay import fixset as fxs

    source_data = json.loads(source.read_text(encoding="utf-8"))
    preset_golds: list[dict[str, Any]] = []
    if fxs.is_fixset(source_data):
        # 评测集 v2（ADR 0031）：没有考卷，金标准已经算好
        fs = fxs.Fixset.model_validate(source_data)
        cases = fxs.eval_cases(fs)
        preset_golds = fxs.gold_rows(fs)
        if any(fe.arm_base(a) == "exam" for a in arm_list):
            raise typer.BadParameter(
                "评测集 v2 没有考卷，只能跑 control 组（如 control、control:notes）")
    else:
        cases = ve.load_cases(source_data)
    # 情景记忆的时间截止（ADR 0032）：Agent 拿到的是修复提交的父提交，所以能看到的是
    # 上游修复合并之前合并的 PR（修复 PR 本身另外排除）；查不到就退回 issue 创建时间
    cutoffs = fe.fix_cutoffs(source_data)
    if only:
        wanted = {int(x) for x in only.split(",")}
        cases = [c for c in cases if c.number in wanted]
    hidden_cases = fe.load_hidden(hidden)
    episodic = None
    if any(fe.arm_memory(a) for a in arm_list):
        from failgate.memory.episodic import EpisodicMemory

        if not memory.exists():
            raise typer.BadParameter(f"{memory} 不存在，先运行 failgate memory build")
        episodic = EpisodicMemory.load(memory)
    started = datetime.now()
    stem = resume.stem if resume else f"{repo.replace('/', '__')}__fix__{started:%Y%m%d-%H%M}"
    jsonl = resume or Path("eval/runs") / f"{stem}.jsonl"
    report_path = Path("eval/reports") / f"{stem}.md"
    meta = {"repo": repo, "started": started.isoformat(timespec="seconds"),
            "source": source.as_posix(), "hidden": hidden.as_posix() if hidden_cases else None,
            "model": settings.llm_model_large, "budget": budget, "rounds": rounds, "reps": reps}

    def append(row: dict[str, Any]) -> None:
        with jsonl.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    async def retrying(make: Any) -> Any:
        # 实验要跑几个小时，GitHub 偶尔连不上不该让整个实验退出
        for attempt in range(3):
            try:
                return await make()
            except httpx.TransportError:
                if attempt == 2:
                    raise
                await asyncio.sleep(5 * (attempt + 1))

    async def run_all() -> None:
        rt = _L2Runtime(settings)
        rows = fe.load_rows(jsonl)
        jsonl.parent.mkdir(parents=True, exist_ok=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        have = {r["number"] for r in rows if r.get("type") == "gold"}
        wanted_numbers = {c.number for c in cases}
        for row in preset_golds:
            if row["number"] in wanted_numbers and row["number"] not in have:
                rows.append(row)
                append(row)
        docs = {d.number: d for d in await _load_issue_docs(
            db_url, repo, [c.number for c in cases])}
        state: dict[int, dict[str, Any]] = {}
        try:
            # 1. 金标准（不花钱）
            golds = {r["number"]: fe.Gold(**r["gold"]) for r in rows if r.get("type") == "gold"}
            for case in cases:
                parent = await retrying(lambda c=case: fetch_github_tree(rt.gh, repo, c.parent))
                cfg = PackageConfig(name=case.exam.package, import_name=case.exam.module)
                prepared = await rt.tester.prepare(
                    cfg, parent, number=case.number, python=case.exam.python,
                    version=case.exam.version, pytest=case.exam.pytest)
                bench = fe.GoldBench(rt.tester, prepared)
                state[case.number] = {"parent": parent, "cfg": cfg, "bench": bench}
                if case.number in golds:
                    continue
                fix = await retrying(lambda c=case: fetch_github_tree(rt.gh, repo, c.fix))
                files = ve._pull_files(await retrying(
                    lambda c=case: rt.gh.compare_files(repo, c.parent, c.fix)))
                gold, test_overlay = await bench.gold_for(parent, fix, files)
                typer.echo(f"#{case.number} 金标准：{', '.join(gold.targets)}")
                golds[case.number] = gold
                row = {"type": "gold", "number": case.number, "gold": gold.model_dump(),
                       "test_overlay": test_overlay}
                rows.append(row)
                append(row)
                typer.echo(f"  P 上失败 {len(gold.fail_parent)}，F 上失败 {len(gold.fail_fix)}，"
                           f"F2P {len(gold.f2p)} → {gold.status} {gold.reason}")
            if gold_only:
                return
            overlays = {r["number"]: r["test_overlay"] for r in rows if r.get("type") == "gold"}
            done = {(r["number"], r["rep"], r["arm"]) for r in rows if r.get("type") == "run"}
            spent = sum(r["fix"]["cost_usd"] for r in rows if r.get("type") == "run")
            # 2. Agent：按 重复 → 题 → 组 排队，最多 concurrency 个同时跑（每个都有自己的工作区卷）
            todo = [(rep, case, arm) for rep in range(1, reps + 1) for case in cases
                    if golds[case.number].status == "ok" for arm in arm_list
                    if (case.number, rep, arm) not in done]
            sem = asyncio.Semaphore(concurrency)
            stopped = False

            async def one(rep: int, case: Any, arm: str) -> None:
                nonlocal spent, stopped
                async with sem:
                    if spent >= max_cost:
                        if not stopped:
                            stopped = True
                            typer.echo(f"已花 ${spent:.3f}，到了上限 ${max_cost}，不再开新的运行。")
                        return
                    st, doc = state[case.number], docs[case.number]
                    task = FixTask(
                        repo=repo, number=case.number,
                        issue=IssueContext(title=doc.title, body=doc.body),
                        test_path=case.exam.test_path,
                        test_code=case.exam.code if fe.arm_base(arm) == "exam" else None,
                        memory_before=cutoffs.get(case.number) or (
                            doc.created_at.replace(tzinfo=UTC)
                            if doc.created_at.tzinfo is None else doc.created_at),
                        exclude_prs=[case.upstream_pr] if case.upstream_pr else [])
                    try:
                        res = await fix_tree(
                            rt.llm, settings.llm_model_large, rt.tester, st["cfg"],
                            st["parent"], task, python=case.exam.python,
                            version=case.exam.version, pytest=case.exam.pytest,
                            max_rounds=rounds, budget_usd=budget, thinking=thinking,
                            artifacts_dir=Path(settings.sandbox_artifacts_dir),
                            handoff=fe.arm_handoff(arm),
                            memory=episodic if fe.arm_memory(arm) else None)
                        spent += res.cost_usd
                        bench, gold = st["bench"], golds[case.number]
                        # 测试文件以上游为准（Agent 本来也改不了测试）
                        overlay = {**res.edits, **overlays[case.number]}
                        judged = fe.judge(await bench.gold_tests(overlay, gold.targets), gold)
                        hid = None
                        if res.edits and case.number in hidden_cases:
                            hc = hidden_cases[case.number]
                            failed = await bench.hidden(
                                res.edits, hidden_path(case.exam.test_path), hc.code)
                            if failed is not None:
                                hid = {"total": len(hc.tests), "failed": sorted(failed),
                                       "flagged": bool(failed)}
                    except Exception as e:  # 一次运行出错不拖垮整个实验；没写记录，--resume 会重跑
                        typer.echo(f"#{case.number} {arm} 第 {rep} 次出错：{type(e).__name__}: "
                                   f"{str(e)[:200]}", err=True)
                        return
                    row = {"type": "run", "number": case.number, "rep": rep, "arm": arm,
                           "fix": res.model_dump(mode="json", exclude={"edits"}),
                           "gold": judged, "hidden": hid}
                    rows.append(row)
                    append(row)
                    verdict = "修好" if judged["resolved"] else "没修好"
                    if not judged["valid"]:
                        verdict += f"（无效：{judged['reason']}）"
                    typer.echo(f"#{case.number} {arm} 第 {rep} 次 → {res.status}，金标准 "
                               f"{verdict}，${res.cost_usd:.4f}，累计 ${spent:.3f}")

            typer.echo(f"待跑 {len(todo)} 次，并发 {concurrency}")
            await asyncio.gather(*(one(*t) for t in todo))
        finally:
            await rt.aclose()
            if rows:
                report_path.write_text(fe.render(rows, meta), encoding="utf-8")
                typer.echo(f"报告：{report_path}")

    asyncio.run(run_all())


@replay_app.command("fixset")
def replay_fixset(
    repo: Annotated[str, typer.Argument(help="owner/name，例如 psf/black")],
    package: Annotated[str, typer.Option(help="包名，例如 black")],
    out: Annotated[Path | None, typer.Option(
        help="评测集 JSON（已存在就接着补），默认 eval/datasets/<repo>/fixset_v1.json")] = None,
    since: Annotated[str, typer.Option(help="只收这一天之后创建的 issue")] = "2022-01-01",
    target: Annotated[int, typer.Option(help="凑够多少题")] = 36,
    exclude_from: Annotated[str, typer.Option(
        help="逗号分隔的回放记录 JSON：里面用过的题都排除（开发集、留出集）；"
             "接着补时和文件里记下的排除名单合并")] = "",
    concurrency: Annotated[int, typer.Option(help="同时处理几题（每题一个沙箱）")] = 2,
    max_candidates: Annotated[int, typer.Option(help="最多看多少个候选")] = 150,
    db_url: Annotated[str, typer.Option("--db", help="回放语料库")] = REPLAY_DB,
) -> None:
    """生成修复评测集 v2（ADR 0031）：按时间从新到旧挑已修复的 bug，金标准能分出好坏的才收。

    不调 LLM；需要 GITHUB_TOKEN 和 Docker。中途停下再跑会接着补（已看过的题不重看）。
    """
    import httpx

    from failgate.platforms.base import PlatformError
    from failgate.replay import fix_eval as fe
    from failgate.replay import fixset as fxs
    from failgate.replay import verify_eval as ve
    from failgate.replay.dataset import dataset_dir
    from failgate.replay.fixes import find_fix
    from failgate.repro.config import PackageConfig
    from failgate.repro.source import SourceError, fetch_github_tree

    settings = Settings()
    rule = _selection(repo)
    out = out or dataset_dir(repo) / "fixset_v1.json"
    exclude = fxs.exclude_from_runs(Path(p) for p in exclude_from.split(",") if p.strip())
    if out.exists():
        fs = fxs.load(out)
        exclude |= set(fs.exclude)
        fs.exclude = sorted(exclude)
        typer.echo(f"接着补：已收 {len(fs.cases)}、已跳过 {len(fs.skipped)}")
    else:
        fs = fxs.Fixset(repo=repo, since=since, target=target, exclude=sorted(exclude),
                        rule=rule, started=datetime.now().isoformat(timespec="seconds"))
    cfg = PackageConfig(name=package)

    async def run() -> None:
        db = Database(db_url)
        await db.create_all()
        async with db.session() as s:
            repo_row = await s.scalar(select(Repo).where(Repo.full_name == repo))
            if repo_row is None:
                raise typer.BadParameter(f"{repo} 不在回放库里")
            all_docs = (await s.scalars(
                select(IssueDoc).where(IssueDoc.repo_id == repo_row.id))).all()
        await db.dispose()
        pool = fxs.candidates(all_docs, rule=rule, since=datetime.fromisoformat(since),
                              exclude=exclude)
        seen = fs.seen()
        todo = [d for d in pool if d.number not in seen][:max_candidates]
        typer.echo(f"候选 {len(pool)} 个（排除 {len(exclude)} 个），这次最多看 {len(todo)} 个")
        rt = _L2Runtime(settings)
        sem = asyncio.Semaphore(concurrency)

        def ok_count() -> int:
            return sum(c.gold.status == "ok" for c in fs.cases)

        async def one(doc: Any) -> None:
            async with sem:
                if ok_count() >= target:
                    return
                n = doc.number

                def skip(reason: str) -> None:
                    fs.skipped.append(fxs.Skipped(number=n, reason=reason))
                    fxs.save(fs, out)
                    typer.echo(f"#{n} 跳过：{reason}")

                try:  # GitHub 查询出错不记录，下次再看
                    fix = await find_fix(rt.gh, repo, n)
                    if fix is None:
                        return skip("no_fix_commit")
                    parent = await fetch_github_tree(rt.gh, repo, fix.parent)
                    files = ve._pull_files(await rt.gh.compare_files(repo, fix.parent, fix.sha))
                    if reason := fxs.skip_reason(files, parent.test_dir()):
                        return skip(reason)
                    fix_tree = await fetch_github_tree(rt.gh, repo, fix.sha)
                except (httpx.HTTPError, PlatformError) as e:
                    typer.echo(f"#{n} GitHub 出错，下次再看：{type(e).__name__}: {str(e)[:120]}",
                               err=True)
                    return
                except SourceError as e:  # 源码包有问题（太大、结构不对）
                    return skip(f"source:{str(e)[:120]}")
                try:  # 环境装不上、预检不过：记下原因
                    prepared = await rt.tester.prepare(cfg, parent, number=n)
                    bench = fe.GoldBench(rt.tester, prepared)
                    gold, overlay = await bench.gold_for(parent, fix_tree, files)
                except Exception as e:
                    return skip(f"env:{type(e).__name__}:{str(e)[:120]}")
                if gold.status != "ok":
                    return skip(f"gold_{gold.status}:{gold.reason}")
                fs.cases.append(fxs.FixsetCase(
                    number=n, title=doc.title, created_at=doc.created_at,
                    labels=list(doc.labels or []), fix=fix,
                    src_files=fxs.source_changes(files, parent.test_dir()),
                    package=cfg.name, module=cfg.import_name or cfg.name,
                    python=prepared.python, version=prepared.version, pytest=prepared.pytest,
                    test_path=prepared.test_path, gold=gold, test_overlay=overlay))
                fxs.save(fs, out)
                typer.echo(f"#{n} 收下（第 {ok_count()} 题）：F2P {len(gold.f2p)}，"
                           f"Python {prepared.python}")

        try:
            await asyncio.gather(*(one(d) for d in todo))
        finally:
            await rt.aclose()
        # 并发时可能多收几题：只留最新的 target 题
        ok = sorted((c for c in fs.cases if c.gold.status == "ok"), key=lambda c: -c.number)
        for extra in ok[target:]:
            fs.cases.remove(extra)
            fs.skipped.append(fxs.Skipped(number=extra.number, reason="over_target"))
        fxs.save(fs, out)
        report = out.with_suffix(".md")
        report.write_text(fxs.render(fs), encoding="utf-8")
        typer.echo(f"收下 {ok_count()} 题 → {out}；说明 {report}")

    asyncio.run(run())


@replay_app.command("fix-verify")
def replay_fix_verify(
    source: Annotated[Path, typer.Option("--from", help="replay fix 的结果（.jsonl）")],
    l2: Annotated[Path, typer.Option("--l2", help="replay l2 的运行记录 JSON（取考卷）")] = Path(
        "eval/runs/psf__black__l2__20260926-1551.json"),
    repo: Annotated[str, typer.Option(help="owner/name")] = "psf/black",
) -> None:
    """把实验组里过了封存考卷的补丁当成 PR，走完整的 ClaimVerify 三层（ADR 0028）。

    不花 LLM 的钱；每个补丁要建一个源码环境（约 1–2 分钟）。结果写进 <来源>__claimverify.jsonl，
    可以中断续跑；跑完把汇总追加到 replay fix 的报告末尾。
    """
    import httpx

    from failgate.replay import fix_eval as fe
    from failgate.replay import verify_eval as ve
    from failgate.repro.l2 import TestReproducer
    from failgate.repro.pypi import PyPIClient
    from failgate.repro.source import fetch_github_tree
    from failgate.verify.workbench import SandboxWorkbench

    settings = Settings()
    cases = {c.number: c for c in ve.load_cases(json.loads(l2.read_text(encoding="utf-8")))}
    rows = [r for r in fe.load_rows(source) if r.get("type") == "run"
            and r["arm"] == "exam" and r["fix"]["passed"]]
    out = source.with_name(f"{source.stem}__claimverify.jsonl")
    report_path = Path("eval/reports") / f"{source.stem}.md"
    done = {(r["number"], r["rep"]) for r in fe.load_rows(out)}

    async def run_all() -> None:
        from failgate.platforms.github_rest import GitHubRest

        gh = GitHubRest(settings.github_token)
        sandbox = build_sandbox(settings)
        tester = TestReproducer(sandbox, _env_cache(settings, sandbox),
                                PyPIClient(settings.pypi_url),
                                run_timeout_s=settings.sandbox_run_timeout_seconds)
        trees: dict[str, Any] = {}
        try:
            for r in rows:
                if (r["number"], r["rep"]) in done:
                    continue
                case = cases[r["number"]]
                if case.parent not in trees:
                    for attempt in range(3):
                        try:
                            trees[case.parent] = await fetch_github_tree(gh, repo, case.parent)
                            break
                        except httpx.TransportError:
                            if attempt == 2:
                                raise
                            await asyncio.sleep(5 * (attempt + 1))
                res = await fe.claimverify_patch(
                    case, trees[case.parent], fe.patch_edits(r),
                    label=f"{case.parent[:12]}+agent{r['rep']}",
                    bench_for=lambda f: SandboxWorkbench(f, tester))
                row = {"number": r["number"], "rep": r["rep"],
                       "gold_resolved": r["gold"]["resolved"],
                       "broken_n": r["gold"].get("broken_n", 0), **res}
                with out.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                typer.echo(f"#{r['number']} 第 {r['rep']} 次：金标准 "
                           f"{'修好' if r['gold']['resolved'] else '没修好'} → {res['verdict']}")
        finally:
            await gh.aclose()
        verified = fe.load_rows(out)
        if report_path.exists():
            text = report_path.read_text(encoding="utf-8").split(fe.VERIFY_HEADING)[0]
            report_path.write_text(text.rstrip("\n") + "\n\n" + fe.render_verify(verified),
                                   encoding="utf-8")
            typer.echo(f"报告：{report_path}")

    asyncio.run(run_all())


@replay_app.command("fix-feedback")
def replay_fix_feedback(
    source: Annotated[Path, typer.Option("--from", help="replay fix 的结果（.jsonl）")],
    l2: Annotated[Path, typer.Option("--l2", help="replay l2 的运行记录 JSON（取考卷）")] = Path(
        "eval/runs/psf__black__l2__20260926-1551.json"),
    repo: Annotated[str, typer.Option(help="owner/name")] = "psf/black",
    rounds: Annotated[int, typer.Option(help="每个补丁最多按驳回理由重修几轮")] = 2,
    budget: Annotated[float, typer.Option(help="每轮修复的花费上限（美元）")] = 0.15,
    db_url: Annotated[str, typer.Option("--db", help="回放语料库（取 issue 正文）")] = REPLAY_DB,
) -> None:
    """按驳回理由重修的离线实验（ADR 0029）：被 ClaimVerify 驳回的补丁，把理由交回修复 Agent。

    输入是 replay fix 和 replay fix-verify 的结果；对每个"过了考卷却被驳回"的补丁，从它接着改，
    新补丁要在全新工作区里通过考卷和第三层查出的测试；再用金标准和 ClaimVerify 各判一次。
    需要 Docker、GITHUB_TOKEN 和 LLM。结果写进 <来源>__feedback.jsonl，可以中断续跑。
    """
    from failgate.fix.agent import FixTask
    from failgate.fix.feedback import feedback_from_claim
    from failgate.fix.run import fix_tree
    from failgate.replay import fix_eval as fe
    from failgate.replay import verify_eval as ve
    from failgate.repro.config import PackageConfig
    from failgate.repro.package import IssueContext
    from failgate.repro.source import fetch_github_tree
    from failgate.verify.workbench import SandboxWorkbench

    settings = Settings()
    cases = {c.number: c for c in ve.load_cases(json.loads(l2.read_text(encoding="utf-8")))}
    runs = {(r["number"], r["rep"]): r for r in fe.load_rows(source)
            if r.get("type") == "run" and r["arm"] == "exam"}
    overlays = {r["number"]: r["test_overlay"] for r in fe.load_rows(source)
                if r.get("type") == "gold"}
    golds = {r["number"]: fe.Gold(**r["gold"]) for r in fe.load_rows(source)
             if r.get("type") == "gold"}
    refuted = [r for r in fe.load_rows(source.with_name(f"{source.stem}__claimverify.jsonl"))
               if r["verdict"] == "REFUTED"]
    out = source.with_name(f"{source.stem}__feedback.jsonl")
    report_path = Path("eval/reports") / f"{source.stem}__feedback.md"
    done = {(r["number"], r["rep"]) for r in fe.load_rows(out) if r.get("final")}

    def append(row: dict[str, Any]) -> None:
        with out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    async def run_all() -> None:
        rt = _L2Runtime(settings)
        docs = {d.number: d for d in await _load_issue_docs(
            db_url, repo, sorted({r["number"] for r in refuted}))}
        try:
            for cv in refuted:
                key = (cv["number"], cv["rep"])
                if key in done:
                    continue
                case = cases[cv["number"]]
                parent = await fetch_github_tree(rt.gh, repo, case.parent)
                cfg = PackageConfig(name=case.exam.package, import_name=case.exam.module)
                prepared = await rt.tester.prepare(
                    cfg, parent, number=case.number, python=case.exam.python,
                    version=case.exam.version, pytest=case.exam.pytest)
                bench = fe.GoldBench(rt.tester, prepared)
                edits = fe.patch_edits(runs[key])
                claim = fe.claim_from_row(cv, case.exam.test_path)
                doc = docs[case.number]
                for round_ in range(1, rounds + 1):
                    fb = feedback_from_claim(claim)
                    task = FixTask(repo=repo, number=case.number,
                                   issue=IssueContext(title=doc.title, body=doc.body),
                                   test_path=case.exam.test_path, test_code=case.exam.code,
                                   feedback=fb.text, must_pass=fb.must_pass)
                    res = await fix_tree(
                        rt.llm, settings.llm_model_large, rt.tester, cfg, parent, task,
                        python=case.exam.python, version=case.exam.version,
                        pytest=case.exam.pytest, max_rounds=2, budget_usd=budget,
                        artifacts_dir=Path(settings.sandbox_artifacts_dir), initial_edits=edits)
                    row: dict[str, Any] = {
                        "number": case.number, "rep": cv["rep"], "round": round_,
                        "status": res.status, "passed": res.passed, "files": res.files,
                        "cost_usd": res.cost_usd, "steps": res.steps,
                        "transcript_path": res.transcript_path, "feedback": fb.text,
                        "must_pass": fb.must_pass, "gold": {}, "verdict": None, "final": False}
                    changed = res.passed and res.edits and res.edits != edits
                    if changed:
                        edits = dict(res.edits)
                        row["gold"] = fe.judge(await bench.gold_tests(
                            {**edits, **overlays[case.number]},
                            golds[case.number].targets), golds[case.number])
                        cvr = await fe.claimverify_patch(
                            case, parent, edits, label=f"{case.parent[:12]}+fb{cv['rep']}{round_}",
                            bench_for=lambda f: SandboxWorkbench(f, rt.tester))
                        row["verdict"] = cvr["verdict"]
                        refuted_again = cvr["verdict"] == "REFUTED"
                        if refuted_again:
                            claim = fe.claim_from_row(
                                {"number": case.number, **cvr}, case.exam.test_path)
                    row["final"] = not changed or not refuted_again or round_ == rounds
                    append(row)
                    typer.echo(f"#{case.number} 第 {cv['rep']} 次的补丁，重修第 {round_} 轮 → "
                               f"{res.status}，交出 {bool(changed)}，金标准 "
                               f"{row['gold'].get('resolved')}，ClaimVerify {row['verdict']}，"
                               f"${res.cost_usd:.4f}")
                    if row["final"]:
                        break
        finally:
            await rt.aclose()
            rows = fe.load_rows(out)
            if rows:
                report_path.write_text(fe.render_feedback(rows), encoding="utf-8")
                typer.echo(f"报告：{report_path}")

    asyncio.run(run_all())


fixer_app_cmd = typer.Typer(help="Fixer App：自带修复 Agent 推分支、开 PR 的身份（ADR 0029）",
                            no_args_is_help=True)
app.add_typer(fixer_app_cmd, name="fixer")


@fixer_app_cmd.command("check")
def fixer_check(
    repo: Annotated[str, typer.Argument(
        help="要开修复 PR 的仓库")] = "san086041-glitch/failgate-demo",
) -> None:
    """确认 Fixer App 配置：身份、权限（要 contents / pull_requests 写）、装没装到这个仓库。

    打印的机器人登录名要写进 .env 的 FIXER_BOT_LOGIN：核验 App 只放行这个账号开的 PR 事件。
    """
    from failgate.app import build_fixer
    from failgate.platforms.github_app import GitHubApiError

    settings = Settings()
    fixer = build_fixer(settings)
    if fixer is None:
        raise typer.BadParameter(
            "请先在 .env 中设置 FIXER_APP_ID 和 FIXER_APP_PRIVATE_KEY_PATH")

    async def run() -> None:
        try:
            info = await fixer.app.get_app()
            perms = info.get("permissions", {})
            typer.echo(f"App: {info['name']} (slug={info['slug']}, id={info['id']})")
            typer.echo(f"权限: {', '.join(f'{k}:{v}' for k, v in sorted(perms.items()))}")
            missing = [k for k in ("contents", "pull_requests") if perms.get(k) != "write"]
            if missing:
                typer.echo(f"❌ 缺少写权限：{', '.join(missing)}")
            login = await fixer.bot_login()
            typer.echo(f"机器人登录名：{login}")
            if settings.fixer_bot_login != login:
                typer.echo(f"⚠️ .env 里 FIXER_BOT_LOGIN={settings.fixer_bot_login!r}，"
                           f"应设为 {login}")
            try:
                inst = await fixer.installation_id(repo)
                token = await fixer.app.installation_token(inst)
                typer.echo(f"✅ 已安装到 {repo}（安装 {inst}，令牌获取成功={bool(token)}）")
            except GitHubApiError as e:
                typer.echo(f"❌ 没有安装到 {repo}：{e}")
        finally:
            await fixer.aclose()

    asyncio.run(run())


def _group_commands() -> None:
    panel_of = {name: panel for panel, names in PANELS.items() for name in names}
    for cmd in app.registered_commands:
        name = cmd.name or (cmd.callback.__name__.replace("_", "-") if cmd.callback else "")
        cmd.rich_help_panel = panel_of.get(name, cmd.rich_help_panel)
    for group in app.registered_groups:
        group.rich_help_panel = panel_of.get(group.name or "", group.rich_help_panel)


_group_commands()
