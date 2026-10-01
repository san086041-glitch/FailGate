"""CLI 首页、doctor、console、找 .env、--help 分组（ADR 0036）。"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from failgate import home
from failgate.cli import app
from failgate.db import Case, Database, Repo, VerificationRecord
from failgate.llm.gateway import key_env
from failgate.settings import Settings, active_env_file, find_env_file, use_env_file


@pytest.fixture
def cli(monkeypatch, tmp_path) -> CliRunner:
    monkeypatch.chdir(tmp_path)
    for name in ("WARDEN_DB_URL", "FAILGATE_DB_URL", "LLM_API_KEY", "GITHUB_TOKEN",
                 "QUEUE_BACKEND", "GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY_PATH",
                 "FIXER_APP_ID", "FIXER_APP_PRIVATE_KEY_PATH", "FAILGATE_NO_BANNER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    # 不依赖本机有没有 Docker
    monkeypatch.setattr(home, "check_docker",
                        lambda settings, wait=3.0: home.Check("Docker", True, "29.0.0"))
    return CliRunner()


def write_env(folder: Path, **values: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ".env"
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    return path


def sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


# ---------------------------------------------------------------- 找 .env


def test_env_file_order(monkeypatch, tmp_path):
    cwd, project = tmp_path / "cwd", tmp_path / "project"
    cwd.mkdir()
    write_env(project, LLM_MODEL_LARGE="from-project")
    monkeypatch.setattr("failgate.settings.project_root", lambda: project)

    choice = find_env_file(cwd=cwd)  # 当前目录没有 → 项目目录
    assert (choice.path, choice.source) == (project / ".env", "项目目录")

    write_env(cwd)
    assert find_env_file(cwd=cwd).source == "当前目录"

    # 设了但文件不存在：照常往下找并提示
    monkeypatch.setenv("FAILGATE_ENV_FILE", str(tmp_path / "pinned" / ".env"))
    choice = find_env_file(cwd=cwd)
    assert choice.source == "当前目录" and "FAILGATE_ENV_FILE" in choice.note
    write_env(tmp_path / "pinned")
    assert find_env_file(cwd=cwd).source == "FAILGATE_ENV_FILE"
    # FAILGATE_HOME 是 MCP 的考卷目录（ADR 0033），和找 .env 无关
    monkeypatch.delenv("FAILGATE_ENV_FILE")
    monkeypatch.setenv("FAILGATE_HOME", str(tmp_path / "pinned"))
    assert find_env_file(cwd=cwd).source == "当前目录"

    explicit = write_env(tmp_path / "x")
    assert find_env_file(explicit, cwd=cwd).source == "参数"
    with pytest.raises(FileNotFoundError):
        find_env_file(tmp_path / "missing.env", cwd=cwd)


def test_env_file_not_found(monkeypatch, tmp_path):
    choice = find_env_file(cwd=tmp_path)
    assert choice.path is None and choice.source == "未找到"
    assert home.check_config(choice).ok is False


def test_use_env_file_drives_settings_and_key_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LLM_MODEL_LARGE", raising=False)
    monkeypatch.delenv("SF_KEY", raising=False)
    path = write_env(tmp_path / "proj", LLM_MODEL_LARGE="m-proj", SF_KEY="k")
    use_env_file(path)
    assert active_env_file() == str(path)
    assert Settings().llm_model_large == "m-proj"
    assert key_env()["SF_KEY"] == "k"  # 厂商 key 和 Settings 读同一个文件
    use_env_file(None)
    assert Settings().llm_model_large != "m-proj"


# ---------------------------------------------------------------- 首页


def test_home_without_terminal_has_no_logo_and_creates_no_db(cli: CliRunner, tmp_path):
    res = cli.invoke(app, [])
    assert res.exit_code == 0, res.output
    assert "███" not in res.output  # 不是终端：不画 logo
    for part in ("环境", "常用", "failgate verify", "failgate doctor", "还不存在"):
        assert part in res.output
    assert not (tmp_path / "failgate.db").exists()  # 首页只读，不会凭空建库


def test_home_reads_project_env_and_shows_stats(cli: CliRunner, monkeypatch, tmp_path):
    db_path = tmp_path / "proj" / "fg.db"
    write_env(tmp_path / "proj", FAILGATE_DB_URL=sqlite_url(db_path), LLM_API_KEY="sk-test")
    monkeypatch.setattr("failgate.settings.project_root", lambda: tmp_path / "proj")

    async def seed() -> None:
        db = Database(sqlite_url(db_path))
        await db.create_all()
        async with db.session() as s, s.begin():
            repo = Repo(platform="github", full_name="acme/app", mode="live")
            s.add(repo)
            await s.flush()
            case = Case(repo_id=repo.id, kind="pull", number=7, title="fix", state="VERIFIED",
                        spent_usd=0.25)
            s.add(case)
            await s.flush()
            s.add(VerificationRecord(case_id=case.id, pr_number=7, base_sha="a", head_sha="b",
                                     verdict="VERIFIED", receipt={}, receipt_sha256="0" * 64))
        await db.dispose()

    asyncio.run(seed())
    res = cli.invoke(app, [])  # 当前目录没有 .env：用项目目录的
    assert res.exit_code == 0, res.output
    assert "项目目录" in res.output and "fg.db" in res.output
    assert "$0.2500" in res.output and "app PR #7 VERIFIED" in res.output
    assert "sk-test" not in res.output  # key 只说有没有


def test_quiet_and_bad_env_file(cli: CliRunner, tmp_path):
    assert cli.invoke(app, ["-q"]).exit_code == 0
    res = cli.invoke(app, ["--env-file", str(tmp_path / "nope.env")])
    assert res.exit_code != 0 and "不存在" in res.output


def test_banner_rules(monkeypatch):
    from rich.console import Console

    term = Console(force_terminal=True, width=120)
    assert home.banner_enabled(term, quiet=False)
    assert not home.banner_enabled(term, quiet=True)
    monkeypatch.setenv("FAILGATE_NO_BANNER", "1")
    assert Settings().failgate_no_banner  # 环境变量和 .env 都认，由调用方并进 quiet
    piped = Console(file=io.StringIO(), width=120)  # 管道 / 重定向
    assert not home.banner_enabled(piped, quiet=False)


def test_narrow_terminal_uses_short_banner():
    from rich.console import Console

    wide = Console(force_terminal=True, width=120, record=True)
    wide.print(home._banner(wide))
    assert "███" in wide.export_text()
    narrow = Console(force_terminal=True, width=60, record=True)
    narrow.print(home._banner(narrow))
    out = narrow.export_text()
    assert "███" not in out and "FailGate" in out


# ---------------------------------------------------------------- doctor


def test_doctor_lists_hints_and_fails_without_llm_key(cli: CliRunner, tmp_path):
    write_env(tmp_path, FAILGATE_DB_URL=sqlite_url(tmp_path / "d.db"))
    res = cli.invoke(app, ["doctor"])
    assert res.exit_code == 1, res.output
    assert "LLM_API_KEY" in res.output
    assert "→" in res.output  # 每个失败项都有修复办法
    assert "GITHUB_TOKEN" in res.output  # 没令牌只提示，不算失败


def test_doctor_passes_when_configured(cli: CliRunner, tmp_path, monkeypatch):
    db_url = sqlite_url(tmp_path / "d.db")
    write_env(tmp_path, FAILGATE_DB_URL=db_url, LLM_API_KEY="sk-test")
    db = Database(db_url)
    asyncio.run(db.create_all())
    asyncio.run(db.dispose())
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test")
    res = cli.invoke(app, ["doctor"])
    assert res.exit_code == 0, res.output
    assert "都正常" in res.output and "待发写操作" in res.output
    assert "ghs_test" not in res.output and "sk-test" not in res.output


def test_app_check_states(tmp_path):
    assert home._app_check("A", "", "", setup="s").ok is None
    assert home._app_check("A", "1", "", setup="s").ok is False
    assert home._app_check("A", "1", str(tmp_path / "no.pem"), setup="s").ok is False
    key = tmp_path / "k.pem"
    key.write_text("x")
    assert home._app_check("A", "1", str(key), setup="s").ok is True


def test_redis_down_is_a_failure():
    settings = Settings(queue_backend="redis", redis_url="redis://127.0.0.1:1/0")
    check = asyncio.run(home.check_redis(settings, 0.5))
    assert check.ok is False and "failgate-redis" in check.hint


# ---------------------------------------------------------------- console 与 --help


def test_console_reports_when_server_is_down(cli: CliRunner, monkeypatch):
    def down(url, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", down)
    res = cli.invoke(app, ["console", "--no-open"])
    assert res.exit_code == 1 and "failgate serve" in res.output


def test_console_detects_old_server(cli: CliRunner, monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(
        404 if url.endswith("/console") else 200))
    res = cli.invoke(app, ["console", "--no-open"])
    assert res.exit_code == 1 and "旧版本" in res.output


def test_console_opens_page(cli: CliRunner, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(200))
    monkeypatch.setattr("webbrowser.open", opened.append)
    res = cli.invoke(app, ["console", "--port", "8091"])
    assert res.exit_code == 0, res.output
    assert opened == ["http://127.0.0.1:8091/console"]


def test_help_is_grouped(cli: CliRunner):
    res = cli.invoke(app, ["--help"])
    assert res.exit_code == 0
    for panel in ("出题 · 答题 · 阅卷", "服务与集成", "评测与回放", "运维"):
        assert panel in res.output
    assert "Commands" not in res.output  # 每个命令都有归属
    assert "修复谁都能写" in res.output


def test_mcp_in_a_terminal_explains_instead_of_hanging(cli: CliRunner, monkeypatch, tmp_path):
    env = write_env(tmp_path / "proj")
    monkeypatch.setattr("failgate.cli._stdin_is_terminal", lambda: True)
    res = cli.invoke(app, ["mcp", "--env-file", str(env)])
    assert res.exit_code == 1
    assert "claude mcp add failgate --" in res.output
    assert env.resolve().as_posix() in res.output


def test_console_old_server_says_how_to_restart(cli: CliRunner, monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(
        404 if url.endswith("/console") else 200))
    res = cli.invoke(app, ["console", "--no-open", "--port", "8080"])
    assert res.exit_code == 1
    assert "8080" in res.output and "failgate serve" in res.output and "--port 8081" in res.output


def test_trace_open_without_langfuse_points_to_local_sink(cli: CliRunner, monkeypatch):
    for name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "OTLP_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    res = cli.invoke(app, ["trace", "open", "--no-open"])
    assert res.exit_code == 1 and "trace sink" in res.output


def test_trace_open_local_otlp_endpoint(cli: CliRunner, monkeypatch):
    monkeypatch.setenv("OTLP_ENDPOINT", "http://127.0.0.1:4318/v1/traces")
    res = cli.invoke(app, ["trace", "open", "--no-open"])
    assert res.exit_code == 1 and "trace show" in res.output


def test_trace_open_finds_the_langfuse_project(cli: CliRunner, monkeypatch):
    monkeypatch.delenv("OTLP_ENDPOINT", raising=False)
    monkeypatch.setenv("LANGFUSE_HOST", "https://jp.cloud.langfuse.com")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    calls: list[tuple[str, object]] = []

    def projects(url, **kw):
        calls.append((url, kw.get("auth")))
        return httpx.Response(200, json={"data": [{"id": "p1", "name": "My Project"}]},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", projects)
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    res = cli.invoke(app, ["trace", "open"])
    assert res.exit_code == 0, res.output
    assert opened == ["https://jp.cloud.langfuse.com/project/p1/traces"]
    assert calls == [("https://jp.cloud.langfuse.com/api/public/projects",
                      ("pk-lf-test", "sk-lf-test"))]
    assert "sk-lf-test" not in res.output


def test_trace_open_falls_back_to_home_page(cli: CliRunner, monkeypatch):
    monkeypatch.delenv("OTLP_ENDPOINT", raising=False)
    monkeypatch.setenv("LANGFUSE_HOST", "https://jp.cloud.langfuse.com/")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")

    def down(url, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", down)
    res = cli.invoke(app, ["trace", "open", "--no-open"])
    assert res.exit_code == 0 and "链路：https://jp.cloud.langfuse.com\n" in res.output
