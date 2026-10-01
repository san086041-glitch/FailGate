"""交互模式（ADR 0036）：在终端里直接输入 `failgate`，显示首页后停在提示符上。

提示符后面敲的就是现有的子命令（不用再写 `failgate`），由同一个 click 命令树分发——
没有第二套逻辑。一条命令出错、被 Ctrl+C 中断，都只打印出来、回到提示符；
exit / quit / Ctrl+D 退出，空行上连按两次 Ctrl+C 也退出（和 Claude Code 一样）。
Tab 补全命令和选项名，↑↓ 翻历史（存在 ~/.failgate/history）。

只在 stdin 和 stdout 都是终端时进入；管道、重定向、CI 里还是显示完首页就退出。
"""

from __future__ import annotations

import shlex
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

# typer 0.27 起自带一份改过的 click（typer._click），命令树、异常类都是它的；
# 单独安装的 click 是另一套类，用它去 except 会接不住（参数错误会让交互模式直接崩掉）
from typer import echo
from typer._click.core import Command
from typer._click.exceptions import ClickException
from typer.core import TyperGroup, TyperOption
from typer.exceptions import Abort

if TYPE_CHECKING:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import FormattedText

EXIT = frozenset({"exit", "quit", "q", ":q"})
HELP = frozenset({"help", "?", "h"})
CLEAR = frozenset({"clear", "cls"})
HOME = frozenset({"home"})
BUILTINS = sorted(EXIT | HELP | CLEAR | HOME)
TOOLBAR = " Tab 补全 · ↑↓ 历史 · help 全部命令 · home 首页 · exit 或连按两次 Ctrl+C 退出 "
HISTORY = Path.home() / ".failgate" / "history"


def split(line: str) -> list[str]:
    """按 shell 规则切参数，但**不把反斜杠当转义**：Windows 路径 D:\\x\\y 要原样保留。"""
    lexer = shlex.shlex(line, posix=True)
    lexer.whitespace_split = True
    lexer.escape = ""
    return list(lexer)


def nested(command: Command) -> dict[str, Any] | None:
    """命令树 → prompt_toolkit NestedCompleter 用的嵌套字典（命令名 + 选项名）。"""
    if isinstance(command, TyperGroup):
        return {name: nested(sub) for name, sub in command.commands.items() if not sub.hidden}
    options = [o for p in command.params if isinstance(p, TyperOption)
               for o in (*p.opts, *p.secondary_opts)]
    return {o: None for o in [*options, "--help"]}


class Shell:
    """一行一行地处理输入；handle 返回 False 表示退出。"""

    def __init__(self, command: Command, *, base_args: Iterable[str] = (),
                 show_home: Callable[[], None] | None = None,
                 echo: Callable[[str], None] = echo) -> None:
        self.command = command
        self.base_args = list(base_args)  # 例如启动时的 --env-file：每条命令都带上
        self.show_home = show_home
        self.echo = echo

    def handle(self, line: str) -> bool:
        try:
            args = split(line)
        except ValueError as exc:  # 引号没闭合
            self.echo(f"没法解析这一行：{exc}")
            return True
        if args and args[0] == "failgate":  # 习惯性带上前缀也行
            args = args[1:]
        if not args:
            return True
        word = args[0].lower()
        if word in EXIT and len(args) == 1:
            return False
        if word in HELP and len(args) == 1:
            self.run(["--help"])
            self.echo("交互模式里还可以用：home（首页）、clear（清屏）、exit（退出）")
            return True
        if word in CLEAR and len(args) == 1:
            from rich.console import Console

            Console().clear()
            return True
        if word in HOME and len(args) == 1:
            if self.show_home is not None:
                self.show_home()
            return True
        self.run(args)
        return True

    def run(self, args: list[str]) -> int:
        """跑一条子命令。返回退出码；任何异常都不让交互模式退出。"""
        from typer import rich_utils

        try:
            rv = self.command.main(args=[*self.base_args, *args], prog_name="failgate",
                                   standalone_mode=False)
        except ClickException as exc:  # 未知命令、参数不对
            rich_utils.rich_format_error(exc)
            return exc.exit_code
        except Abort:
            self.echo("已中断")
            return 130
        except KeyboardInterrupt:
            self.echo("已中断")
            return 130
        except SystemExit as exc:  # 有的命令直接 sys.exit
            return exc.code if isinstance(exc.code, int) else 1
        except Exception as exc:  # noqa: BLE001 —— 命令自己的错误：报出来，回到提示符
            self.echo(f"出错了：{type(exc).__name__}: {exc}")
            return 1
        code = rv if isinstance(rv, int) else 0
        if code == 130:  # typer 把命令执行中的 Ctrl+C 转成 Exit(130)
            self.echo("已中断")
        return code


def make_session(command: Command, **kwargs: Any) -> PromptSession[str]:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.completion import NestedCompleter
    from prompt_toolkit.history import FileHistory, InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent

    keys = KeyBindings()

    @keys.add("c-c")
    def _ctrl_c(event: KeyPressEvent) -> None:
        """有内容时清掉这一行；空行上才当作"想退出"交给 loop 计数。"""
        buffer = event.current_buffer
        if buffer.text:
            buffer.reset()
        else:
            event.app.exit(exception=KeyboardInterrupt, style="class:aborting")

    tree = nested(command) or {}
    tree.update({b: None for b in BUILTINS})
    try:
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        history: Any = FileHistory(str(HISTORY))
    except OSError:
        history = InMemoryHistory()
    kwargs.setdefault("history", history)
    return PromptSession(completer=NestedCompleter.from_nested_dict(tree),
                         auto_suggest=AutoSuggestFromHistory(), complete_while_typing=False,
                         bottom_toolbar=TOOLBAR, key_bindings=keys, **kwargs)


def prompt_text() -> FormattedText:
    from prompt_toolkit.formatted_text import FormattedText

    return FormattedText([("class:name", "failgate"), ("class:arrow", " ❯ ")])


def loop(shell: Shell, session: PromptSession[str]) -> None:
    from prompt_toolkit.styles import Style

    style = Style.from_dict({"name": "bold #22d3ee", "arrow": "#6366f1",
                             "bottom-toolbar": "noreverse #94a3b8"})
    armed = False  # 空行上刚按过一次 Ctrl+C
    while True:
        try:
            line = session.prompt(prompt_text(), style=style)
        except KeyboardInterrupt:
            if armed:
                break
            armed = True
            shell.echo("再按一次 Ctrl+C 退出（或输入 exit）")
            continue
        except EOFError:  # Ctrl+D
            break
        armed = False
        if not shell.handle(line):
            break
    shell.echo("再见。")
