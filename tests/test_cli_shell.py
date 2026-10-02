"""交互模式（ADR 0036）：分发到同一个命令树，出错 / 中断都回到提示符。"""

from __future__ import annotations

import httpx
import pytest
import typer
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from failgate import home, shell
from failgate.cli import app


@pytest.fixture
def command(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in ("FAILGATE_DB_URL", "WARDEN_DB_URL", "LLM_API_KEY", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(home, "check_docker",
                        lambda settings, wait=3.0: home.Check("Docker", True, "29.0.0"))
    return typer.main.get_command(app)


def make(command, **kw) -> tuple[shell.Shell, list[str]]:
    said: list[str] = []
    return shell.Shell(command, echo=said.append, **kw), said


def test_split_keeps_windows_paths_and_quotes():
    assert shell.split(r'verify --from D:\eval\runs\x.json "a b"') == [
        "verify", "--from", r"D:\eval\runs\x.json", "a b"]
    with pytest.raises(ValueError):
        shell.split('fix run "unterminated')


def test_split_keeps_issue_and_pr_numbers():
    # 用户实测踩到：shlex 默认把 # 后面当注释，owner/name#20 变成 owner/name
    assert shell.split("verify san086041-glitch/failgate-demo#20") == [
        "verify", "san086041-glitch/failgate-demo#20"]
    assert shell.split("hidden show a/b#2 --x") == ["hidden", "show", "a/b#2", "--x"]


def test_completion_tree(command):
    tree = shell.nested(command)
    assert tree is not None
    assert {"doctor", "verify", "replay", "console"} <= tree.keys()
    assert "fix" in tree["replay"] and "run" in tree["fix"]  # 子命令
    assert {"--port", "--open", "--no-open", "--help"} <= tree["console"].keys()


def test_builtins(command):
    sh, said = make(command)
    assert sh.handle("") and sh.handle("   ")
    assert sh.handle("help")
    assert "home" in said[-1]
    for word in ("exit", "quit", "q", "EXIT"):
        assert sh.handle(word) is False


def test_errors_stay_in_the_shell(command, capsys):
    sh, said = make(command)
    assert sh.handle("no-such-command")
    assert sh.run(["console", "--port", "not-a-number"]) == 2  # 参数错误：报出来、不退出
    assert sh.handle('fix run "oops') and "没法解析" in said[-1]
    err = capsys.readouterr()
    assert "no-such-command" in err.out + err.err


def test_failing_command_returns_its_exit_code(command, monkeypatch):
    def down(url, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", down)
    sh, _ = make(command)
    assert sh.run(["console", "--no-open"]) == 1
    assert sh.handle("failgate console --no-open")  # 习惯性带前缀也行


def test_ctrl_c_and_crashes_in_a_command(command, monkeypatch):
    def interrupted(url, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(httpx, "get", interrupted)
    sh, said = make(command)
    assert sh.run(["console", "--no-open"]) == 130 and said[-1] == "已中断"

    def boom(url, **kw):
        raise RuntimeError("坏了")

    monkeypatch.setattr(httpx, "get", boom)
    assert sh.run(["console", "--no-open"]) == 1 and "RuntimeError: 坏了" in said[-1]


def test_base_args_pin_the_env_file(command, tmp_path, monkeypatch, capsys):
    env = tmp_path / "proj" / ".env"
    env.parent.mkdir()
    env.write_text(f"FAILGATE_DB_URL=sqlite+aiosqlite:///{(tmp_path / 'p.db').as_posix()}\n",
                   encoding="utf-8")
    sh, _ = make(command, base_args=["--env-file", str(env)])
    sh.run(["doctor"])
    assert "参数" in capsys.readouterr().out  # doctor 看到的是启动时那个 .env


def test_loop_reads_until_exit(command, monkeypatch):
    homes: list[int] = []
    sh, said = make(command, show_home=lambda: homes.append(1))
    with create_pipe_input() as inp:
        session = shell.make_session(command, input=inp, output=DummyOutput(),
                                     history=InMemoryHistory())
        inp.send_text("home\nno-such-command\nexit\nhome\n")
        shell.loop(sh, session)
    assert homes == [1]  # exit 之后的 home 没执行
    assert said[-1] == "再见。"


def test_loop_ends_on_ctrl_d(command):
    sh, said = make(command)
    with create_pipe_input() as inp:
        session = shell.make_session(command, input=inp, output=DummyOutput(),
                                     history=InMemoryHistory())
        inp.send_text("\x04")  # Ctrl+D
        shell.loop(sh, session)
    assert said == ["再见。"]


def run_keys(command, text: str) -> list[str]:
    sh, said = make(command)
    with create_pipe_input() as inp:
        session = shell.make_session(command, input=inp, output=DummyOutput(),
                                     history=InMemoryHistory())
        inp.send_text(text)
        shell.loop(sh, session)
    return said


CTRL_C = "\x03"
ARMED = "再按一次 Ctrl+C 退出（或输入 exit）"


def test_ctrl_c_twice_on_empty_line_exits(command):
    assert run_keys(command, CTRL_C * 2) == [ARMED, "再见。"]


def test_ctrl_c_clears_typed_text_first(command):
    # 有内容时 Ctrl+C 只清空这一行，不算"想退出"；之后空行上按两次才退出
    assert run_keys(command, "verify abc" + CTRL_C * 3) == [ARMED, "再见。"]


def test_ctrl_c_counter_resets_after_a_command(command):
    # 按一次 → 敲了别的命令 → 再按一次：不退出，要重新确认
    said = run_keys(command, CTRL_C + "home\n" + CTRL_C + "exit\n")
    assert said.count(ARMED) == 2 and said[-1] == "再见。"


