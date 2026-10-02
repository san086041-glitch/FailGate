"""CLI 首页和 `failgate doctor`（ADR 0036）。

- 首页：直接输入 `failgate`。logo + 环境 + 数据概览 + 常用命令，1 秒左右出结果；
- doctor：逐项检查，失败的每项给一句能直接复制的修复办法，有失败时退出码 1。

检查都是只读的：不建表、不写库、不打印任何 key（只看有没有）。输出不是终端时（管道、
重定向、测试）不画 logo、不上色；终端编码不是 UTF-8 时用 ASCII 符号。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from rich.cells import cell_len
from rich.console import Console, Group
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from failgate import __version__
from failgate.i18n import t
from failgate.settings import EnvChoice, Settings

DOCKER_DESKTOP = r"C:\Users\<you>\AppData\Local\Programs\DockerDesktop\Docker Desktop.exe"


def tagline() -> str:
    return t("修复谁都能写，FailGate 负责证明它修对了。",
             "Anyone can write a fix. FailGate proves it is right.")

LOGO = r"""
 ███████╗ █████╗ ██╗██╗      ██████╗  █████╗ ████████╗███████╗
 ██╔════╝██╔══██╗██║██║     ██╔════╝ ██╔══██╗╚══██╔══╝██╔════╝
 █████╗  ███████║██║██║     ██║  ███╗███████║   ██║   █████╗
 ██╔══╝  ██╔══██║██║██║     ██║   ██║██╔══██║   ██║   ██╔══╝
 ██║     ██║  ██║██║███████╗╚██████╔╝██║  ██║   ██║   ███████╗
 ╚═╝     ╚═╝  ╚═╝╚═╝╚══════╝ ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚══════╝""".strip("\n")
LOGO_WIDTH = max(len(line) for line in LOGO.splitlines())
# 青 → 蓝的渐变，每行一个颜色
LOGO_COLORS = ("#22d3ee", "#1fbfef", "#1caaf0", "#3b8ef3", "#4f7bf5", "#6366f1")

def common() -> tuple[tuple[str, str], ...]:
    return (
        (t("failgate verify <仓库>#<PR>", "failgate verify <repo>#<PR>"),
         t("用封存考卷核验一个 PR", "verify a PR against the sealed exam")),
        (t("failgate fix run <仓库> <N>", "failgate fix run <repo> <N>"),
         t("修复 Agent 修一个 issue", "let the fix agent fix an issue")),
        ("failgate mcp", t("怎么接到 Claude Code / Cursor（由客户端启动）",
                           "how to plug into Claude Code / Cursor (client-launched)")),
        ("failgate up", t("一键上线：检查环境、起服务和 webhook 转发",
                          "go live: checks, server and webhook tunnel")),
        ("failgate console", t("在浏览器打开工作台", "open the web console")),
    )


# 找 .env 的来源（settings.EnvChoice.source）的英文
SOURCE_EN = {"参数": "--env-file", "当前目录": "current dir", "项目目录": "project dir",
             "未找到": "not found"}

# 和工作台一致：通过绿、驳回红、无法判定黄、进行中蓝
VERDICT_STYLE = {"VERIFIED": "green", "REFUTED": "red", "INCONCLUSIVE": "yellow"}


@dataclass
class Check:
    name: str
    ok: bool | None  # True 好、False 坏、None 没启用 / 不确定（不算失败）
    detail: str
    hint: str = ""  # 坏的时候怎么修（doctor 显示）


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    stats: dict[str, Any] | None = None
    latest: dict[str, Any] | None = None
    db_name: str = ""

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.ok is False]


# ---------------------------------------------------------------- 检查


def check_config(choice: EnvChoice) -> Check:
    name = t("配置", "Config")
    note = choice.note
    if choice.path is None:
        return Check(name, False, t("没找到 .env，只用环境变量和默认值",
                                    "no .env found; using env vars and defaults")
                     + (f" ({note})" if note else ""),
                     t("在项目目录运行，或设置环境变量 FAILGATE_ENV_FILE=<.env 的路径>，"
                       "或加 --env-file",
                       "run in the project dir, set FAILGATE_ENV_FILE=<path to .env>, "
                       "or pass --env-file"))
    source = t(choice.source, SOURCE_EN.get(choice.source, choice.source))
    detail = f"{choice.path} ({source})"
    return Check(name, True, detail + (f"; {note}" if note else ""))


def check_docker(settings: Settings, timeout: float = 3.0) -> Check:
    from failgate.repro.sandbox import docker_env, find_docker

    docker = find_docker(settings.docker_bin)
    if docker is None:
        return Check("Docker", False, t("找不到 docker 命令", "docker command not found"),
                     t("安装 Docker Desktop，或在 .env 里设置 DOCKER_BIN",
                       "install Docker Desktop, or set DOCKER_BIN in .env"))
    try:
        out = subprocess.run([docker, "version", "--format", "{{.Server.Version}}"],
                             capture_output=True, text=True, timeout=timeout,
                             env=docker_env(docker), check=False)
    except subprocess.TimeoutExpired:
        return Check("Docker", None, t(f"{timeout:g} 秒内没响应（引擎可能正在启动）",
                                       f"no answer in {timeout:g}s (engine may be starting)"))
    version = out.stdout.strip()
    if out.returncode != 0 or not version:
        return Check("Docker", False, t("引擎连不上", "engine unreachable"),
                     t(f'Docker Desktop 可能没开：Start-Process "{DOCKER_DESKTOP}"，等约 60 秒',
                       f'Docker Desktop may be closed: Start-Process "{DOCKER_DESKTOP}", '
                       "then wait ~60s"))
    return Check("Docker", True, version)


def _db_label(url: str) -> tuple[str, str]:
    """(展示名, 库名)。不显示用户名和密码。"""
    parts = urlsplit(url)
    if parts.scheme.startswith("sqlite"):
        return "SQLite", url.split("///", 1)[-1] or ":memory:"
    if parts.scheme.startswith("postgresql"):
        return "PostgreSQL", parts.path.lstrip("/")
    return parts.scheme, parts.path.lstrip("/")


def _sqlite_file(url: str) -> Path | None:
    if not url.startswith("sqlite") or ":memory:" in url:
        return None
    return Path(url.split("///", 1)[-1])


async def check_database(settings: Settings, report: Report, wait: float) -> Check:
    """连库读统计（只读，不建表：首页不该在当前目录里凭空建出一个 failgate.db）。"""
    from sqlalchemy import inspect

    from failgate.db import Database
    from failgate.overview import collect_stats, latest_verification

    kind, name = _db_label(settings.failgate_db_url)
    report.db_name = name if kind == "PostgreSQL" else Path(name).name
    file = _sqlite_file(settings.failgate_db_url)
    if file is not None and not file.is_file():
        return Check(t("数据库", "Database"), None,
                     t(f"SQLite {file} 还不存在", f"SQLite {file} does not exist yet"),
                     t("failgate db-init 建表", "create tables: failgate db-init"))
    db = Database(settings.failgate_db_url)
    label = t("数据库", "Database")
    running = t("确认数据库在跑（本机是 Docker 容器 failgate-pg）",
                "make sure the database is running (Docker container failgate-pg here)")

    async def read() -> None:
        async with db.engine.connect() as conn:
            tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
        if "cases" not in tables:
            raise LookupError
        async with db.session() as s:
            report.stats = await collect_stats(s)
            report.latest = await latest_verification(s)

    try:
        await asyncio.wait_for(read(), wait)
    except LookupError:
        return Check(label, None, t(f"{kind} {name} 还没建表", f"{kind} {name} has no tables"),
                     "failgate db-init")
    except TimeoutError:
        return Check(label, False, t(f"{kind} {name} {wait:g} 秒内没连上",
                                     f"{kind} {name}: no connection in {wait:g}s"), running)
    except Exception as exc:  # noqa: BLE001 —— 连不上的原因五花八门，只报类型
        return Check(label, False, t(f"{kind} {name} 连不上（{type(exc).__name__}）",
                                     f"{kind} {name} unreachable ({type(exc).__name__})"),
                     running + t("、FAILGATE_DB_URL 正确", "; check FAILGATE_DB_URL"))
    finally:
        await db.dispose()
    return Check(label, True, f"{kind} ({name})")


async def check_redis(settings: Settings, wait: float) -> Check:
    if settings.queue_backend != "redis":
        return Check(t("队列", "Queue"), None,
                     t("进程内（QUEUE_BACKEND=local，重启会丢排队中的任务）",
                       "in-process (QUEUE_BACKEND=local; queued jobs are lost on restart)"))
    from redis.asyncio import Redis

    client = Redis.from_url(settings.redis_url, socket_connect_timeout=wait,
                            socket_timeout=wait)
    try:
        await asyncio.wait_for(client.ping(), wait)
    except Exception as exc:  # noqa: BLE001
        return Check(t("队列", "Queue"), False,
                     t(f"Redis 连不上（{type(exc).__name__}）",
                       f"Redis unreachable ({type(exc).__name__})"),
                     t("确认 Redis 在跑（本机是 Docker 容器 failgate-redis）、REDIS_URL 正确",
                       "make sure Redis is running (container failgate-redis); check REDIS_URL"))
    finally:
        await client.aclose()
    return Check(t("队列", "Queue"), True, "Redis")


def check_llm(settings: Settings) -> Check:
    host = urlsplit(settings.llm_base_url).hostname or settings.llm_base_url
    if not settings.llm_api_key:
        return Check("LLM", False, t(f"{host}：没有 LLM_API_KEY", f"{host}: no LLM_API_KEY"),
                     t("在 .env 里设置 LLM_API_KEY", "set LLM_API_KEY in .env"))
    gateway = t("，多模型网关已启用", ", multi-model gateway on") if settings.llm_providers else ""
    return Check("LLM", True, f"{host} · {settings.llm_model_large} "
                              + t(f"（key 已配置{gateway}）", f"(key set{gateway})"))


def check_embedding(settings: Settings) -> Check:
    if not (settings.embed_base_url and settings.embed_api_key and settings.embed_model):
        return Check("Embedding", None, t("没配置（查重只用词法和堆栈通道）",
                                          "not set (dedup uses lexical + stack channels only)"))
    host = urlsplit(settings.embed_base_url).hostname or settings.embed_base_url
    return Check("Embedding", True, f"{host} · {settings.embed_model}")


def check_github_token(settings: Settings) -> Check:
    if settings.github_token or os.environ.get("GITHUB_TOKEN"):
        return Check("GitHub", True, t("GITHUB_TOKEN 已设置", "GITHUB_TOKEN set"))
    hint = (t("PowerShell：", "PowerShell: ") + "$env:GITHUB_TOKEN = gh auth token"
            if os.name == "nt" else 'export GITHUB_TOKEN="$(gh auth token)"')
    # 不算失败：令牌一般按会话临时设置，不写进 .env
    return Check("GitHub", None, t("没有 GITHUB_TOKEN（verify、fix run、回放要用）",
                                   "no GITHUB_TOKEN (needed by verify, fix run, replays)"), hint)


def _app_check(name: str, app_id: str | None, key_path: str | None, *, setup: str) -> Check:
    if not app_id and not key_path:
        return Check(name, None, t("没配置", "not set"), setup)
    if not app_id or not key_path:
        return Check(name, False, t("App ID 和私钥路径只配了一个",
                                    "only one of App ID / private key path is set"), setup)
    if not Path(key_path).expanduser().is_file():
        return Check(name, False, t(f"App {app_id}：私钥文件不存在",
                                    f"App {app_id}: private key file missing"),
                     t("检查私钥路径", "check the private key path"))
    return Check(name, True, t(f"App {app_id}，私钥在", f"App {app_id}, key present"))


def check_github_app(settings: Settings) -> Check:
    return _app_check("GitHub App", settings.github_app_id, settings.github_app_private_key_path,
                      setup=t("见 docs/github-app-setup.md；配好后 failgate github check",
                              "see docs/github-app-setup.md, then failgate github check"))


def check_fixer_app(settings: Settings) -> Check:
    return _app_check("Fixer App", settings.fixer_app_id, settings.fixer_app_private_key_path,
                      setup=t("可选（/failgate fix 推分支用），见 docs/fixer-app-setup.md",
                              "optional (pushes /failgate fix branches), see "
                              "docs/fixer-app-setup.md"))


def check_pending_effects(report: Report) -> Check:
    """待发的写操作：服务一启动补偿任务就会把它们发出去（演示前要先看）。"""
    name = t("待发写操作", "Pending writes")
    if report.stats is None:
        return Check(name, None, t("数据库不可用，没查", "database unavailable, not checked"))
    pending = report.stats.get("pending_effects", 0)
    if pending:
        return Check(name, None, t(f"{pending} 条（服务启动后会自动补发）",
                                   f"{pending} (will be sent once the server starts)"),
                     t("先看清楚：failgate effects list", "review first: failgate effects list"))
    return Check(name, True, t("没有", "none"))


async def collect(settings: Settings, choice: EnvChoice, *, full: bool,
                  wait: float = 1.5) -> Report:
    """并行跑检查。首页（full=False）只跑主要几项，每项最多等 wait 秒。"""
    report = Report()
    docker = asyncio.to_thread(check_docker, settings, wait if not full else 5.0)
    db_wait = wait if not full else 5.0
    results: list[Check] = list(await asyncio.gather(
        docker,
        check_database(settings, report, db_wait),
        check_redis(settings, db_wait),
    ))
    if report.stats is not None and full:
        report.stats["pending_effects"] = await _pending_effects(settings)
    report.checks = [check_config(choice), *results, check_llm(settings)]
    if full:
        report.checks += [check_embedding(settings), check_github_token(settings),
                          check_github_app(settings), check_fixer_app(settings),
                          check_pending_effects(report)]
    else:
        report.checks.append(check_github_token(settings))
    return report


async def _pending_effects(settings: Settings) -> int:
    from sqlalchemy import func, select

    from failgate.db import Database, Effect

    db = Database(settings.failgate_db_url)
    try:
        async with db.session() as s:
            return int(await s.scalar(select(func.count()).select_from(Effect)
                                      .where(Effect.status == "pending")) or 0)
    finally:
        await db.dispose()


# ---------------------------------------------------------------- 渲染


def _symbols(console: Console) -> dict[bool | None, tuple[str, str]]:
    utf = (console.encoding or "").lower().replace("-", "").startswith("utf")
    if utf:
        # ✔（U+2714）在 Windows Terminal 里会被画成彩色 emoji、吃掉后面的空格；✓ 不会
        return {True: ("✓", "green"), False: ("✗", "red"), None: ("·", "yellow")}
    return {True: ("OK", "green"), False: ("X ", "red"), None: ("- ", "yellow")}


def _banner(console: Console) -> Text | Group:
    if console.width < LOGO_WIDTH + 2:
        title = Text("▌FailGate", style="bold #22d3ee")
        return Group(title, Text(f" {tagline()}  v{__version__}", style="dim"))
    logo = Text()
    for i, (line, color) in enumerate(zip(LOGO.splitlines(), LOGO_COLORS, strict=True)):
        logo.append(("\n" if i else "") + line, style=f"bold {color}")
    line = tagline()
    tag = Text(f" {line}", style="italic")
    version = f"v{__version__}"
    gap = max(2, LOGO_WIDTH - 1 - cell_len(line) - len(version))
    tag.append(" " * gap + version, style="dim")
    return Group(logo, tag)


def _checks_table(report: Report, console: Console, *, hints: bool) -> Table:
    sym = _symbols(console)
    table = Table.grid(padding=(0, 1))
    table.add_column(no_wrap=True)
    table.add_column(no_wrap=True, style="bold")
    table.add_column()
    for c in report.checks:
        mark, style = sym[c.ok]
        table.add_row(Text(mark, style=style), c.name, c.detail)
        if hints and c.hint and c.ok is not True:
            table.add_row("", "", Text(f"→ {c.hint}", style="cyan"))
    return table


def _stats_table(report: Report, console: Console) -> Table | Text:
    if report.stats is None:
        return Text(t("数据库不可用", "database unavailable"), style="dim")
    st = report.stats
    sym = _symbols(console)
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim", no_wrap=True)
    table.add_column()
    table.add_row("Case", str(st["cases"]))
    exams = st.get("evidence") or {}
    table.add_row(t("封存考卷", "Sealed exams"),
                  ", ".join(f"{n} ({lv})" for lv, n in sorted(exams.items())) or "0")
    v = st.get("verifications") or {}
    verdicts = Text()
    for key, mark in (("VERIFIED", sym[True][0]), ("REFUTED", sym[False][0]),
                      ("INCONCLUSIVE", "?")):
        verdicts.append(f"{mark} {v.get(key, 0)}  ", style=VERDICT_STYLE[key])
    table.add_row(t("核验", "Verifications"), verdicts)
    table.add_row(t("花费", "Spent"), f"${st['spent_usd']:.4f}")
    if report.latest:
        lt = report.latest
        last = Text(f"{lt['repo'].split('/')[-1]} PR #{lt['pr']} ")
        last.append(str(lt["verdict"] or "NO_CLAIM"),
                    style=VERDICT_STYLE.get(str(lt["verdict"]), "dim"))
        day = f"{lt['at']:%m-%d}"
        last.append(t(f"（{day}）", f" ({day})"), style="dim")
        table.add_row(t("最近", "Latest"), last)
    return table


def _common(console: Console) -> Table:
    table = Table.grid(padding=(0, 3))
    table.add_column(style="bold cyan", no_wrap=True)
    table.add_column(style="dim")
    for cmd, desc in common():
        table.add_row(cmd, desc)
    return table


def render_home(report: Report, console: Console, *, banner: bool) -> None:
    if banner:
        console.print(_banner(console))
        console.print()
    env = Panel(_checks_table(report, console, hints=False), title=t("环境", "Environment"),
                title_align="left", border_style="blue", expand=False)
    data_label = t("数据", "Data")
    data_title = f"{data_label} ({report.db_name})" if report.db_name else data_label
    data = Panel(_stats_table(report, console), title=data_title, title_align="left",
                 border_style="blue", expand=False)
    if console.width >= 110:
        row = Table.grid(padding=(0, 1))
        row.add_row(env, data)
        console.print(row)
    else:  # 上下叠放时一样宽
        env.expand = data.expand = True
        console.print(env)
        console.print(data)
    console.print(Padding(Text(t("常用", "Common commands"), style="bold"), (1, 0, 0, 1)))
    console.print(Padding(_common(console), (0, 0, 0, 3)))
    foot = Text(t(" 全部命令：", " All commands: "), style="dim")
    foot.append("failgate --help", style="bold")
    foot.append(t("    环境详情：", "    Environment details: "), style="dim")
    foot.append("failgate doctor", style="bold")
    if report.failed:
        foot.append(t(f"（{len(report.failed)} 项有问题）", f" ({len(report.failed)} problem(s))"),
                    style="red")
    console.print(foot)


def render_doctor(report: Report, console: Console) -> None:
    console.print(Text(t("FailGate 环境检查", "FailGate environment check"), style="bold"))
    console.print(_checks_table(report, console, hints=True))
    sym = _symbols(console)
    if report.failed:
        console.print(Text(f"\n{sym[False][0]} "
                           + t(f"{len(report.failed)} 项有问题",
                               f"{len(report.failed)} problem(s)"), style="red"))
    else:
        console.print(Text(f"\n{sym[True][0]} " + t("都正常", "all good"), style="green"))
    console.print(Text(t("更深的沙箱隔离自检：failgate sandbox check；"
                         "GitHub App 权限：failgate github check",
                         "deeper sandbox isolation self-test: failgate sandbox check; "
                         "GitHub App permissions: failgate github check"), style="dim"))


def make_console() -> Console:
    return Console(highlight=False, soft_wrap=False)


def banner_enabled(console: Console, quiet: bool) -> bool:
    """quiet 来自 -q 或 Settings.failgate_no_banner（环境变量和 .env 都认）。"""
    return console.is_terminal and not quiet
