"""发出去之前的密钥扫描（技术方案第 12 节"通过评论内容外泄数据"）。

模型的上下文里本来就没有我们自己的密钥；这一层防的是另一种情况：
issue 里用户误贴了令牌，模型在理由或引用里原样复述，机器人再把它公开发一遍。
这里只做高置信度的正则匹配；熵检测误报太多，暂不启用。
"""

from __future__ import annotations

import re

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    ("openai_style_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("private_key", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
)


def find_secrets(text: str) -> list[str]:
    """返回命中的规则名（不返回命中的内容本身，避免把密钥写进日志）。"""
    return [name for name, pattern in _PATTERNS if pattern.search(text)]
