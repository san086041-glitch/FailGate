"""命令行界面的英文版（ADR 0039）：--lang / FAILGATE_LANG 切换，默认中文。"""

from __future__ import annotations

import io
import re
from datetime import UTC, datetime

import pytest
from opentelemetry import trace
from rich.console import Console
from typer.testing import CliRunner

from failgate import home, i18n, progress, shell, views
from failgate import up as u
from failgate.cli import app
from failgate.verify.engine import ClaimResult, ClaimVerdict, Layer1, Layer2, Layer3, Verification
from failgate.verify.hidden import HiddenResult
from failgate.verify.strength import StrengthReport

CJK = re.compile(r"[一-鿿]")


def text_of(renderable, width: int = 160) -> str:
    con = Console(record=True, width=width, file=io.StringIO())
    con.print(renderable)
    return con.export_text()


@pytest.fixture
def cli(monkeypatch, tmp_path) -> CliRunner:
    monkeypatch.chdir(tmp_path)
    for name in ("FAILGATE_DB_URL", "WARDEN_DB_URL", "GITHUB_TOKEN", "QUEUE_BACKEND",
                 "GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY_PATH", "FIXER_APP_ID",
                 "FIXER_APP_PRIVATE_KEY_PATH", "EMBED_BASE_URL", "EMBED_API_KEY", "EMBED_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(home, "check_docker",
                        lambda settings, wait=3.0: home.Check("Docker", True, "29.0.0"))
    return CliRunner()


def test_set_lang_normalizes():
    assert i18n.set_lang("EN") == "en" and i18n.t("中", "en") == "en"
    assert i18n.set_lang("en_US") == "en"
    assert i18n.set_lang("zh-CN") == "zh" and i18n.t("中", "en") == "中"
    assert i18n.set_lang("fr") == "zh"  # 认不出来用中文
    assert i18n.set_lang(None) == "zh"


def test_home_and_doctor_in_english(cli: CliRunner, tmp_path):
    (tmp_path / ".env").write_text("FAILGATE_LANG=en\n", encoding="utf-8")
    res = cli.invoke(app, [])
    assert res.exit_code == 0, res.output
    for part in ("Environment", "Config", "current dir", "Database", "Common commands",
                 "verify a PR against the sealed exam", "All commands"):
        assert part in res.output, part
    assert not CJK.search(res.output), res.output  # 一个汉字都不剩
    res = cli.invoke(app, ["doctor"])
    assert "FailGate environment check" in res.output and "Pending writes" in res.output
    assert not CJK.search(res.output), res.output


def test_lang_flag_beats_setting(cli: CliRunner, tmp_path):
    (tmp_path / ".env").write_text("FAILGATE_LANG=en\n", encoding="utf-8")
    res = cli.invoke(app, ["--lang", "zh", "doctor"])
    assert "FailGate 环境检查" in res.output
    assert cli.invoke(app, ["db-init"]).exit_code == 0
    res = cli.invoke(app, ["--lang", "en", "cases"])
    assert "no matching cases" in res.output


def test_default_stays_chinese(cli: CliRunner):
    res = cli.invoke(app, ["doctor"])
    assert "FailGate 环境检查" in res.output


def test_progress_in_english():
    i18n.set_lang("en")
    tracker = progress.Tracker("Verify acme/app#7")
    hub = progress.relay()
    hub.listeners.append(tracker)
    tracer = trace.get_tracer("test")
    try:
        with tracer.start_as_current_span("verify layer1"):
            pass
        with tracer.start_as_current_span("fix edit", attributes={"failgate.fix.round": 2}):
            with tracer.start_as_current_span("chat x") as span:
                span.set_attribute("failgate.cost_usd", 0.001)
    finally:
        hub.listeners.remove(tracker)
    out = text_of(tracker)
    assert "① exam: fails before, passes after" in out and "edit (round 2)" in out
    assert "LLM 1 calls" in out and "elapsed" in out
    assert not CJK.search(out), out


def test_verification_panel_in_english():
    i18n.set_lang("en")
    c = ClaimResult(issue=19, verdict=ClaimVerdict.VERIFIED, reasons=[],
                    test_path="tests/t.py", test_sha256="ab" * 32,
                    layer1=Layer1(status="pass", reason="pass"), layer2=Layer2(),
                    layer3=Layer3(status="pass", reason="pass", files=["tests/a.py"]),
                    strength=StrengthReport(status="ok", reason="ok", killed=7, survived=3,
                                            kill_rate=0.7, grade="medium"),
                    hidden=HiddenResult(status="ok", total=5, passed=5))
    v = Verification(repo="acme/app", pr=20, base_sha="1234567", head_sha="89abcde",
                     head_repo="acme/app", claims=[c], verdict=ClaimVerdict.VERIFIED,
                     created_at="x")
    out = text_of(views.verification_panel(v))
    for part in ("accepted", "Claims to fix #19", "① exam", "② tamper", "③ regress",
                 "(kill rate 70%)", "all 5 passed", "base 1234567"):
        assert part in out, part
    assert not CJK.search(out), out


def test_board_and_shell_in_english():
    i18n.set_lang("en")
    board = u.Board([u.Proc("serve", ["x"])], "http://127.0.0.1:8080", datetime.now(UTC))
    out = text_of(board)
    assert "FailGate live" in out and "This run" in out and "no events yet" in out
    assert not CJK.search(out), out
    assert "Ctrl+C twice" in shell.toolbar()
    said: list[str] = []
    sh = shell.Shell(object(), echo=said.append)  # type: ignore[arg-type]
    sh.handle('fix "oops')
    assert said[-1].startswith("cannot parse this line")
