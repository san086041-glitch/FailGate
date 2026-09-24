import re
from dataclasses import dataclass

VERBS = frozenset({"fix", "retry", "ignore", "learn", "budget"})
_COMMAND = re.compile(r"^/warden\s+(\w+)(?:\s+(.*))?$")


@dataclass(frozen=True)
class Command:
    verb: str
    arg: str = ""


def parse_command(body: str) -> Command | None:
    """在评论里找第一条 /warden 命令；未知命令视为普通评论。"""
    for line in body.splitlines():
        m = _COMMAND.match(line.strip())
        if m and m.group(1) in VERBS:
            return Command(m.group(1), (m.group(2) or "").strip())
    return None
