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
from rich.console import Console
from rich.text import Text

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
        return None, "没配置 SMEE_URL，也没配置 GitHub App"
    try:
        url = str((await gh.get_hook_config()).get("url") or "")
    except Exception as exc:  # noqa: BLE001 —— 读不到就只起服务，原因报出来
        return None, f"读 App 的 webhook 配置失败（{type(exc).__name__}）"
    finally:
        await gh.aclose()
    if urlsplit(url).hostname == "smee.io":
        return url, "GitHub App 的 webhook 地址"
    return None, f"App 的 webhook 地址不是 smee 通道（{urlsplit(url).hostname or '空'}）"


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
                 popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen) -> None:
        self.console = console
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
            t = threading.Thread(target=self._pump, args=(proc.name, proc.popen.stdout),
                                 daemon=True)
            t.start()
            self.threads.append(t)

    def _pump(self, name: str, stream: IO[str]) -> None:
        tag = Text(f"{name:<6}│ ", style=STYLES.get(name, "white"))
        for line in stream:
            self.console.print(tag + Text(line.rstrip("\n")), soft_wrap=True)

    def exited(self) -> Proc | None:
        """第一个已经退出的子进程。"""
        return next((p for p in self.procs if p.popen is not None
                     and p.popen.poll() is not None), None)

    def stop_all(self, grace: float = 8.0) -> None:
        """后起的先停（先断 smee 转发，再停 worker，最后停服务）。"""
        for proc in reversed(self.procs):
            self._stop(proc, grace)
        for t in self.threads:
            t.join(timeout=1.0)

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
