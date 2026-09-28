"""自动打标签的策略（技术方案第 11 节"标签属于可自动打的白名单"）。

两层规则，在 PolicyGate 里执行，不依赖模型听话：
1. 始终拦截"维护者的处理结论 / 进度"类标签（重复、不是 bug、已接受、需要讨论……）。
   这类标签是维护者看过之后的决定，机器人替他们打就是越权。按标签名里的关键词识别，
   对 GitHub 默认标签和常见的自定义命名（例如 psf/black 的 "R: not a bug"、
   "S: needs repro"）都有效。
2. 仓库级白名单（可选）：配置了通配符模式（如 "T: *"）后，只有匹配的标签才会自动打上。
   没配置时只执行第 1 层。

分诊提示词 v2 也要求模型不选这类标签（ADR 0006），这里是代码层面的兜底。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from fnmatch import fnmatchcase

# 有歧义的单词：只有出现在标签名末尾时才算（"R: invalid" 拦，"C: invalid code" 不拦——
# 后者在 psf/black 里的说明是 "Black destroyed a valid Python file"，是类别标签）
TAIL_WORDS: tuple[str, ...] = (
    "duplicate", "dupe", "invalid", "outdated", "obsolete", "resolved", "fixed", "released",
    "confirmed",
)

# 意思明确的短语：出现在标签名任何位置都算"结论 / 进度"类
DECISION_PHRASES: tuple[str, ...] = (
    # 处理结论
    "wontfix", "wont fix", "won t fix", "not a bug",
    "not planned", "rejected", "declined", "works as intended", "working as intended",
    "by design",
    # 处理进度 / 状态
    "accepted", "approved", "triaged", "needs triage", "needs discussion",
    "needs decision", "needs repro", "needs reproduction", "needs info", "needs more info",
    "more info needed", "awaiting", "waiting for", "blocked", "on hold", "stale",
    "in progress", "wip", "up for grabs",
    # 邀请贡献：维护者决定是否向外部贡献者开放
    "good first issue", "good second issue", "help wanted", "hacktoberfest",
    # 明显的管理动作
    "spam",
)

_NON_WORD = re.compile(r"[^0-9a-z]+")


def _normalize(name: str) -> str:
    return " " + _NON_WORD.sub(" ", name.lower()).strip() + " "


_PHRASES = tuple(_normalize(p) for p in DECISION_PHRASES)


def decision_phrase(name: str) -> str | None:
    """标签名像"结论 / 进度"类时，返回命中的短语；否则返回 None。

    按整词匹配（前后补空格），避免 "prefixed" 命中 "fixed"。
    """
    norm = _normalize(name)
    for raw, phrase in zip(DECISION_PHRASES, _PHRASES, strict=True):
        if phrase in norm:
            return raw
    for word in TAIL_WORDS:
        if norm.endswith(f" {word} "):
            return word
    return None


def filter_auto_labels(
    names: Sequence[str], allow: Sequence[str] | None = None
) -> tuple[list[str], dict[str, str]]:
    """返回 (可以自动打的标签, {被拦下的标签: 原因})。"""
    kept: list[str] = []
    blocked: dict[str, str] = {}
    for name in names:
        phrase = decision_phrase(name)
        if phrase is not None:
            blocked[name] = f"decision/status label ({phrase})"
        elif allow and not any(fnmatchcase(name, pattern) for pattern in allow):
            blocked[name] = "not in repo allowlist"
        elif name not in kept:
            kept.append(name)
    return kept, blocked
