import re
from dataclasses import dataclass

VERBS = frozenset({"fix", "retry", "ignore", "learn", "budget", "verify", "reseal"})
# /warden 是改名前的写法，维护者可能还习惯用，照样认
_COMMAND = re.compile(r"^/(?:failgate|warden)\s+(\w+)(?:\s+(.*))?$")


@dataclass(frozen=True)
class Command:
    verb: str
    arg: str = ""


def parse_command(body: str) -> Command | None:
    """在评论里找第一条 /failgate 命令；未知命令视为普通评论。"""
    for line in body.splitlines():
        m = _COMMAND.match(line.strip())
        if m and m.group(1) in VERBS:
            return Command(m.group(1), (m.group(2) or "").strip())
    return None
