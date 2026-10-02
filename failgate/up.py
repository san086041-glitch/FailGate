"""`failgate up`：本机一键上线（ADR 0037）。

把原来要手动做的几步串起来：启动前检查（复用 doctor）→ 待发评论保护 → 端口检查 →
起 `failgate serve`（可选再起沙箱 worker）→ 等 /healthz → 起 smee 转发 → 合并输出 →
Ctrl+C 一次全部停掉。

子进程都放在单独的进程组里：终端的 Ctrl+C 只到本进程，由本进程按顺序停子进程
（Windows 先发 Ctrl+Break——uvicorn 会优雅退出——超时再 taskkill /T 连子进程树一起杀，
否则 npx 拉起的 node 会变成孤儿；POSIX 先 SIGINT 整个进程组、超时 SIGKILL）。
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any
from urllib.parse import urlsplit

import httpx
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from failgate.i18n import t
from failgate.settings import Settings

STYLES = {"serve": "cyan", "worker": "yellow", "smee": "magenta"}
IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------- 检查


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def stop_port_hint(port: int) -> str:
    """停掉占着端口的进程的命令（只给命令，不替用户执行）。"""
    if IS_WINDOWS:
        return (f"Get-NetTCPConnection -LocalPort {port} -State Listen | "
                "ForEach-Object { Stop-Process -Id $_.OwningProcess }")
    return f"kill $(lsof -t -i :{port} -sTCP:LISTEN)"


async def pending_effects(settings: Settings, limit: int = 20) -> list[dict[str, Any]]:
    """待发的写操作：服务一启动补偿任务就会把它们发出去。"""
    from sqlalchemy import select

    from failgate.db import Case, Database, Effect, Repo

    db = Database(settings.failgate_db_url)
    try:
        async with db.session() as s:
            rows = (await s.execute(
                select(Effect.action, Effect.mode, Effect.attempts, Effect.created_at,
                       Repo.full_name, Case.number)
                .join(Case, Effect.case_id == Case.id).join(Repo, Case.repo_id == Repo.id)
                .where(Effect.status == "pending").order_by(Effect.created_at)
                .limit(limit))).all()
    finally:
        await db.dispose()
    return [{"action": a, "mode": m, "attempts": n, "at": at, "repo": r, "number": num}
            for a, m, n, at, r, num in rows]


async def resolve_smee(settings: Settings) -> tuple[str | None, str]:
    """(通道地址, 来源说明)。只认 smee.io：App 的 webhook 地址是公网地址时不需要转发。"""
    if settings.smee_url:
        return settings.smee_url, "SMEE_URL"
    from failgate.app import build_github_app

    gh = build_github_app(settings)
    if gh is None:
        return None, t("没配置 SMEE_URL，也没配置 GitHub App", "no SMEE_URL and no GitHub App")
    try:
        url = str((await gh.get_hook_config()).get("url") or "")
    except Exception as exc:  # noqa: BLE001 —— 读不到就只起服务，原因报出来
        return None, t(f"读 App 的 webhook 配置失败（{type(exc).__name__}）",
                       f"could not read the App webhook config ({type(exc).__name__})")
    finally:
        await gh.aclose()
    if urlsplit(url).hostname == "smee.io":
        return url, t("GitHub App 的 webhook 地址", "GitHub App webhook URL")
    host = urlsplit(url).hostname or t("空", "empty")
    return None, t(f"App 的 webhook 地址不是 smee 通道（{host}）",
                   f"the App webhook URL is not a smee channel ({host})")


def find_npx() -> list[str] | None:
    """npx 的启动命令。Windows 上 npx 是批处理（npx.cmd），被中断时会问
    "Terminate batch job (Y/N)?"：能找到 npm 自带的 npx-cli.js 就直接用 node 跑它。"""
    if IS_WINDOWS:
        node = shutil.which("node")
        if node:
            cli = Path(node).parent / "node_modules" / "npm" / "bin" / "npx-cli.js"
            if cli.is_file():
                return [node, str(cli)]
        found = shutil.which("npx.cmd")
        return [found] if found else None
    found = shutil.which("npx")
    return [found] if found else None


# ---------------------------------------------------------------- 子进程


@dataclass
class Proc:
    name: str
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    popen: subprocess.Popen[str] | None = None


def child_env(env_file: Path | None, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"})
    if env_file is not None:
        env["FAILGATE_ENV_FILE"] = str(env_file)
    env.update(extra or {})
    return env


def plan(*, env_file: Path | None, host: str, port: int, smee: str | None,
         npx: list[str] | None, worker: bool) -> list[Proc]:
    """要起哪些进程（纯函数，方便测试）。"""
    py = [sys.executable, "-m", "failgate"]
    if env_file is not None:
        py += ["--env-file", str(env_file)]
    serve_extra = {"WORKER_LANES": "events"} if worker else {}
    procs = [Proc("serve", [*py, "serve", "--host", host, "--port", str(port)],
                  child_env(env_file, serve_extra))]
    if worker:
        procs.append(Proc("worker", [*py, "worker", "--lanes", "sandbox"], child_env(env_file)))
    if smee and npx:
        target = f"http://{host}:{port}/webhooks/github"
        procs.append(Proc("smee", [*npx, "--yes", "smee-client", "--url", smee,
                               "--target", target], child_env(env_file)))
    return procs


class Supervisor:
    """起子进程、把输出加前缀合并到一个屏幕、按顺序停掉。"""

    def __init__(self, console: Console,
                 popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
                 *, verbose: bool = True) -> None:
        self.console = console
        self.verbose = verbose
        self.popen = popen
        self.procs: list[Proc] = []
        self.threads: list[threading.Thread] = []

    def start(self, proc: Proc) -> None:
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        proc.popen = self.popen(proc.argv, env=proc.env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                                encoding="utf-8", errors="replace", bufsize=1, **kwargs)
        self.procs.append(proc)
        if proc.popen.stdout is not None:
            pump = threading.Thread(target=self._pump, args=(proc.name, proc.popen.stdout),
                                    daemon=True)
            pump.start()
            self.threads.append(pump)

    def _pump(self, name: str, stream: IO[str]) -> None:
        tag = Text(f"{name:<6}│ ", style=STYLES.get(name, "white"))
        for line in stream:
            if keep_line(name, line, self.verbose):
                self.console.print(tag + Text(line.rstrip("\n")), soft_wrap=True)

    def exited(self) -> Proc | None:
        """第一个已经退出的子进程。"""
        return next((p for p in self.procs if p.popen is not None
                     and p.popen.poll() is not None), None)

    def stop_all(self, grace: float = 8.0) -> None:
        """后起的先停（先断 smee 转发，再停 worker，最后停服务）。"""
        for proc in reversed(self.procs):
            self._stop(proc, grace)
        for thread in self.threads:
            thread.join(timeout=1.0)

    def _stop(self, proc: Proc, grace: float) -> None:
        p = proc.popen
        if p is None or p.poll() is not None:
            return
        try:
            if sys.platform == "win32":
                p.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(p.pid, signal.SIGINT)
        except (OSError, ValueError):
            pass
        try:
            p.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            pass
        if sys.platform == "win32":  # 连子进程树一起杀（npx → node）
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)],
                           capture_output=True, check=False)
        else:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
        p.wait(timeout=5)


def wait_healthy(url: str, alive: Callable[[], bool], timeout: float = 90.0,
                 get: Callable[..., httpx.Response] = httpx.get) -> bool:
    """等服务的 /healthz 返回 200（启动时会跑数据库迁移，可能要十几秒）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not alive():
            return False
        try:
            if get(url, timeout=1.0).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return False


# ---------------------------------------------------------------- 实时状态板（ADR 0038）


@dataclass
class Snapshot:
    events: int = 0  # 本次启动以来收到的 webhook
    cases: int = 0  # 本次启动以来状态变过的 Case
    cost: float = 0.0  # 本次启动以来模块运行的花费
    verdicts: dict[str, int] = field(default_factory=dict)
    recent: list[tuple[Any, str, int, str, str, str]] = field(default_factory=list)
    working: list[tuple[str, int, str, str, Any]] = field(default_factory=list)
    error: str = ""


async def snapshot(db: Any, since: Any, recent: int = 6) -> Snapshot:
    """只读查询：本次启动（since 之后）发生了什么、现在有哪些 Case 在跑。"""
    from sqlalchemy import func, select

    from failgate.db import Case, Delivery, Repo, Run, TransitionLog, VerificationRecord
    from failgate.views import WORKING

    snap = Snapshot()
    async with db.session() as s:
        snap.events = int(await s.scalar(select(func.count()).select_from(Delivery)
                                         .where(Delivery.received_at >= since)) or 0)
        snap.cases = int(await s.scalar(select(func.count(func.distinct(TransitionLog.case_id)))
                                        .where(TransitionLog.at >= since)) or 0)
        snap.cost = float(await s.scalar(select(func.coalesce(func.sum(Run.usd), 0.0))
                                         .where(Run.started_at >= since)) or 0.0)
        rows = (await s.execute(select(VerificationRecord.verdict, func.count())
                                .where(VerificationRecord.created_at >= since)
                                .group_by(VerificationRecord.verdict))).all()
        snap.verdicts = {str(v): n for v, n in rows}
        logs = (await s.execute(
            select(TransitionLog.at, Repo.full_name, Case.number, Case.kind,
                   TransitionLog.from_state, TransitionLog.to_state)
            .join(Case, TransitionLog.case_id == Case.id).join(Repo, Case.repo_id == Repo.id)
            .where(TransitionLog.at >= since)
            .order_by(TransitionLog.id.desc()).limit(recent))).all()
        snap.recent = [tuple(r) for r in logs]  # type: ignore[misc]
        busy = (await s.execute(
            select(Repo.full_name, Case.number, Case.kind, Case.state, Case.updated_at)
            .join(Repo, Case.repo_id == Repo.id).where(Case.state.in_(WORKING))
            .order_by(Case.updated_at).limit(4))).all()
        snap.working = [tuple(r) for r in busy]  # type: ignore[misc]
    return snap


class Board:
    """`up` 底部的状态板：上面照常滚日志，它固定在最下面每 2 秒刷新。"""

    def __init__(self, procs: list[Proc], base: str, since: Any) -> None:
        self.procs = procs
        self.base = base
        self.since = since
        self.started = time.monotonic()
        self.snap = Snapshot()

    async def refresh(self, db: Any) -> None:
        try:
            self.snap = await snapshot(db, self.since)
        except Exception as exc:  # noqa: BLE001 —— 库一时读不到不该让 up 退出
            self.snap.error = t(f"数据库读不到（{type(exc).__name__}）",
                                f"cannot read the database ({type(exc).__name__})")

    def __rich__(self) -> Any:
        from datetime import UTC, datetime

        from failgate.views import state_text

        snap = self.snap
        services = Text()
        for proc in self.procs:
            alive = proc.popen is not None and proc.popen.poll() is None
            services.append("● " if alive else "○ ", style="green" if alive else "red")
            services.append(f"{proc.name}   ")
        stats = Text(t("本次  ", "This run  "), style="bold")
        stats.append(t(f"事件 {snap.events} · Case {snap.cases} · 核验 ",
                       f"events {snap.events} · cases {snap.cases} · verified "))
        stats.append(f"✓{snap.verdicts.get('VERIFIED', 0)} ", style="green")
        stats.append(f"✗{snap.verdicts.get('REFUTED', 0)} ", style="red")
        stats.append(f"?{snap.verdicts.get('INCONCLUSIVE', 0)}", style="yellow")
        stats.append(t(" · 花费 ", " · spent ") + f"${snap.cost:.4f}")
        rows: list[Any] = [services, stats]
        now = datetime.now(UTC)
        if snap.working:
            busy = Table.grid(padding=(0, 2))
            for repo, number, kind, state, since in snap.working:
                kind_label = "PR" if kind == "pull" else "issue"
                took = int((now - _aware(since)).total_seconds())
                busy.add_row(Text(t("进行中", "running"), style="bold blue"),
                             f"{repo.split('/')[-1]} {kind_label} #{number}",
                             state_text(state), Text(f"{took // 60}:{took % 60:02d}", style="dim"))
            rows.append(busy)
        if snap.recent:
            recent = Table.grid(padding=(0, 2))
            for at, repo, number, _kind, old, new in snap.recent:
                line = Text(f"{old} → ")
                line.append_text(state_text(new))
                recent.add_row(Text(f"{_aware(at).astimezone():%H:%M:%S}", style="dim"),
                               f"{repo.split('/')[-1]}#{number}", line)
            rows.append(Text(t("最近", "Recent"), style="bold"))
            rows.append(recent)
        else:
            rows.append(Text(t("还没有新事件：在演示仓库开 issue 或评论 /failgate … 试试",
                               "no events yet: open an issue or comment /failgate … on the "
                               "demo repo"), style="dim"))
        if snap.error:
            rows.append(Text(snap.error, style="red"))
        up_for = int(time.monotonic() - self.started)
        h, rem = divmod(up_for, 3600)
        uptime = f"{h}:{rem // 60:02d}:{rem % 60:02d}"
        title = Text(t(f" FailGate 在线 · 已运行 {uptime} ", f" FailGate live · up {uptime} "),
                     style="bold green")
        return Panel(Group(*rows), title=title, title_align="left", border_style="green",
                     subtitle=t(f"工作台 {self.base}/console · Ctrl+C 停止",
                                f"console {self.base}/console · Ctrl+C to stop"),
                     subtitle_align="right")


def _aware(at: Any) -> Any:
    """SQLite 读回来的时间可能不带时区（都是按 UTC 存的）。"""
    from datetime import UTC

    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


LOG_KEEP = ("WARNING", "ERROR", "CRITICAL", "Traceback", "Exception", "error", "Error")


def keep_line(name: str, line: str, verbose: bool) -> bool:
    """--logs 关掉时只留警告和错误；/healthz 的访问日志任何时候都不显示（up 自己在轮询）。"""
    if '"GET /healthz' in line:
        return False
    return verbose or any(k in line for k in LOG_KEEP)
