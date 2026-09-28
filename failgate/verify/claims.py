"""从 PR 的标题和正文里找"修复了哪个 issue"的声明（技术方案 9.1 节）。

按 GitHub 的关闭关键字：close / closes / closed / fix / fixes / fixed / resolve / resolves /
resolved，后面跟 #N 或 owner/name#N，大小写不敏感。只接受同一个仓库的 issue：
别的仓库的 issue 没有我们封存的考卷，核验不了。
"""

from __future__ import annotations

import re

_KEYWORDS = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
_CLAIM = re.compile(
    rf"(?<![\w/]){_KEYWORDS}:?\s+(?:([\w.-]+/[\w.-]+))?#(\d+)\b", re.IGNORECASE
)


def parse_claims(title: str, body: str | None, repo: str) -> list[int]:
    """声明修复的 issue 编号，按出现顺序去重。"""
    found: list[int] = []
    for m in _CLAIM.finditer(f"{title}\n{body or ''}"):
        other, number = m.group(1), int(m.group(2))
        if other and other.lower() != repo.lower():
            continue
        if number not in found:
            found.append(number)
    return found
