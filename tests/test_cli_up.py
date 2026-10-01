"""`failgate up`：本机一键上线（ADR 0037）。"""

from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

import httpx
import pytest
from rich.console import Console
from typer.testing import CliRunner

from failgate import home
from failgate import up as u
from failgate.cli import app
from failgate.db import Case, Database, Effect, Repo
from failgate.settings import Settings


def sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


@pytest.fixture
def cli(monkeypatch, tmp_path) -> CliRunner:
    monkeypatch.chdir(tmp_path)
    for name in ("WARDEN_DB_URL", "GITHUB_TOKEN", "QUEUE_BACKEND", "GITHUB_APP_ID",
                 "GITHUB_APP_PRIVATE_KEY_PATH", "FIXER_APP_ID", "FIXER_APP_PRIVATE_KEY_PATH",
                 "SMEE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("FAILGATE_DB_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    (tmp_path / ".env").write_text(
        f"FAILGATE_DB_URL={sqlite_url(tmp_path / 'up.db')}\nLLM_API_KEY=sk-test\n",
        encoding="utf-8")
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(home, "check_docker",
                        lambda settings, wait=3.0: home.Check("Docker", True, "29.0.0"))
    db = Database(sqlite_url(tmp_path / "up.db"))
    asyncio.run(db.create_all())
    asyncio.run(db.dispose())
    return CliRunner()


# ---------------------------------------------------------------- 计划与检查


def test_plan_serve_only_without_tunnel(tmp_path):
    env = tmp_path / ".env"
    procs = u.plan(env_file=env, host="127.0.0.1", port=8090, smee=None, npx=None, worker=False)
    assert [p.name for p in procs] == ["serve"]
    argv = procs[0].argv
    assert argv[:3] == [sys.executable, "-m", "failgate"]
    assert argv[3:] == ["--env-file", str(env), "serve", "--host", "127.0.0.1", "--port", "8090"]
    assert procs[0].env["FAILGATE_ENV_FILE"] == str(env)
    assert "WORKER_LANES" not in procs[0].env or procs[0].env["WORKER_LANES"] != "events"


def test_plan_with_worker_and_tunnel():
    procs = u.plan(env_file=None, host="127.0.0.1", port=8080, smee="https://smee.io/abc",
                   npx=["node", "npx-cli.js"], worker=True)
    assert [p.name for p in procs] == ["serve", "worker", "smee"]
    assert procs[0].env["WORKER_LANES"] == "events"  # 服务只跑快车道
    assert procs[1].argv[-3:] == ["worker", "--lanes", "sandbox"]
    assert procs[2].argv == ["node", "npx-cli.js", "--yes", "smee-client", "--url",
                             "https://smee.io/abc", "--target",
                             "http://127.0.0.1:8080/webhooks/github"]


def test_plan_skips_tunnel_without_npx():
    procs = u.plan(env_file=None, host="h", port=1, smee="https://smee.io/x", npx=None,
                   worker=False)
    assert [p.name for p in procs] == ["serve"]


def test_find_npx_prefers_node_script_on_windows(monkeypatch, tmp_path):
    node = tmp_path / "node.exe"
    cli = tmp_path / "node_modules" / "npm" / "bin" / "npx-cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("")
    monkeypatch.setattr(u, "IS_WINDOWS", True)
    monkeypatch.setattr(u.shutil, "which", lambda name: str(node) if name == "node" else None)
    assert u.find_npx() == [str(node), str(cli)]  # 不走 npx.cmd：中断时不会问 Terminate batch job
    cli.unlink()
    monkeypatch.setattr(u.shutil, "which",
                        lambda name: {"node": str(node), "npx.cmd": "C:/x/npx.cmd"}.get(name))
    assert u.find_npx() == ["C:/x/npx.cmd"]


def test_port_in_use():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        assert u.port_in_use("127.0.0.1", port)
    assert not u.port_in_use("127.0.0.1", port)


def test_wait_healthy():
    answers = iter([httpx.ConnectError("not yet"), httpx.Response(503), httpx.Response(200)])

    def get(url, **kw):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a

    assert u.wait_healthy("http://x/healthz", lambda: True, timeout=10, get=get)
    assert not u.wait_healthy("http://x/healthz", lambda: False, timeout=10, get=get)


class FakeApp:
    def __init__(self, hook: dict | Exception) -> None:
        self.hook = hook

    async def get_hook_config(self) -> dict:
        if isinstance(self.hook, Exception):
            raise self.hook
        return self.hook

    async def aclose(self) -> None:
        pass


@pytest.mark.parametrize(("hook", "expect"), [
    ({"url": "https://smee.io/H6km"}, "https://smee.io/H6km"),
    ({"url": "https://failgate.example.com/webhooks/github"}, None),  # 公网地址：不用转发
    (RuntimeError("401"), None),
])
def test_resolve_smee_from_app(monkeypatch, hook, expect):
    monkeypatch.setattr("failgate.app.build_github_app", lambda settings: FakeApp(hook))
    url, why = asyncio.run(u.resolve_smee(Settings(smee_url="")))
    assert url == expect and why


def test_resolve_smee_prefers_setting(monkeypatch):
    def boom(settings):
        raise AssertionError("配了 SMEE_URL 就不该去问 App")

    monkeypatch.setattr("failgate.app.build_github_app", boom)
    assert asyncio.run(u.resolve_smee(Settings(smee_url="https://smee.io/x"))) == (
        "https://smee.io/x", "SMEE_URL")


# ---------------------------------------------------------------- 子进程


def test_supervisor_prefixes_output_and_stops_children():
    console = Console(record=True, width=200, force_terminal=False)
    sup = u.Supervisor(console)
    code = "import time; print('hello', flush=True); time.sleep(60)"
    proc = u.Proc("serve", [sys.executable, "-c", code], u.child_env(None))
    sup.start(proc)
    assert proc.popen is not None
    for _ in range(100):
        if "hello" in console.export_text(clear=False):
            break
        asyncio.run(asyncio.sleep(0.05))
    assert sup.exited() is None
    sup.stop_all(grace=5)
    assert proc.popen.poll() is not None  # 停掉了
    assert "serve │ hello" in console.export_text()


def test_supervisor_notices_a_crashed_child():
    sup = u.Supervisor(Console(record=True))
    proc = u.Proc("smee", [sys.executable, "-c", "raise SystemExit(3)"], u.child_env(None))
    sup.start(proc)
    assert proc.popen is not None
    proc.popen.wait(timeout=10)
    dead = sup.exited()
    assert dead is proc and proc.popen.returncode == 3
    sup.stop_all()


# ---------------------------------------------------------------- 命令


def test_up_refuses_when_environment_is_broken(cli: CliRunner, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "")
    res = cli.invoke(app, ["up"])
    assert res.exit_code == 1 and "没有启动" in res.output and "LLM_API_KEY" in res.output


def test_up_refuses_pending_effects(cli: CliRunner, tmp_path, monkeypatch):
    async def seed() -> None:
        db = Database(sqlite_url(tmp_path / "up.db"))
        async with db.session() as s, s.begin():
            repo = Repo(platform="github", full_name="acme/app", mode="live")
            s.add(repo)
            await s.flush()
            case = Case(repo_id=repo.id, kind="issue", number=9, title="t", state="ANSWERED")
            s.add(case)
            await s.flush()
            s.add(Effect(effect_key="k1", case_id=case.id, action="create_comment",
                         payload={}, mode="live", status="pending"))
        await db.dispose()

    asyncio.run(seed())
    started: list[object] = []
    monkeypatch.setattr(u.Supervisor, "start", lambda self, proc: started.append(proc))
    res = cli.invoke(app, ["up", "--no-tunnel"])
    assert res.exit_code == 1, res.output
    assert "1 条待发" in res.output and "acme/app#9" in res.output
    assert "--allow-pending" in res.output and started == []  # 什么都没起


def test_up_refuses_busy_port(cli: CliRunner, monkeypatch):
    monkeypatch.setattr(u, "port_in_use", lambda host, port: True)
    started: list[object] = []
    monkeypatch.setattr(u.Supervisor, "start", lambda self, proc: started.append(proc))
    res = cli.invoke(app, ["up", "--port", "8080", "--no-tunnel"])
    assert res.exit_code == 1 and "8080 已经被占用" in res.output and started == []


def test_up_worker_needs_redis(cli: CliRunner):
    res = cli.invoke(app, ["up", "--worker", "--no-tunnel"])
    assert res.exit_code != 0 and "QUEUE_BACKEND=redis" in res.output
