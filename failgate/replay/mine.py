"""从维护者评论中挖掘查重的标准答案。

为什么不用 GitHub 的"关闭原因 = duplicate"：很多项目的维护者习惯把重复关闭为 completed，
再留一条 "Duplicate of #N" 的评论（psf/black 的 2822 个 issue 里只有 12 个用了关闭原因）。

为什么只认 OWNER / MEMBER / COLLABORATOR：普通用户的"我觉得是 #N 的重复"只是猜测，
标准答案必须来自有权限做这个判断的人。
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime

from failgate.platforms.github_rest import GitHubRest

from .dataset import GoldPair, GoldSet

TRUSTED = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


def duplicate_pattern(repo: str) -> re.Pattern[str]:
    return re.compile(
        rf"duplicate\s+of\s+(?:#|https://github\.com/{re.escape(repo)}/issues/)(\d+)", re.I
    )


def find_original(comments: list[dict], number: int, pattern: re.Pattern[str]) -> GoldPair | None:
    """按时间顺序找第一条"维护者写的 Duplicate of #N"。"""
    for c in comments:
        if c.get("author_association") not in TRUSTED:
            continue
        m = pattern.search(c.get("body") or "")
        if m and int(m.group(1)) != number:
            return GoldPair(
                duplicate=number,
                original=int(m.group(1)),
                evidence_url=c.get("html_url"),
                association=c.get("author_association", ""),
            )
    return None


async def mine_gold(gh: GitHubRest, repo: str, *, concurrency: int = 8) -> GoldSet:
    numbers = await gh.search_issue_numbers(
        f'repo:{repo} is:issue "duplicate of" in:comments'
    )
    pattern = duplicate_pattern(repo)
    sem = asyncio.Semaphore(concurrency)

    async def one(n: int) -> GoldPair | None:
        async with sem:
            comments = await gh.list_comments(repo, n)
        return find_original(comments, n, pattern)

    found = await asyncio.gather(*(one(n) for n in numbers))
    pairs = sorted((p for p in found if p is not None), key=lambda p: p.duplicate)
    return GoldSet(repo=repo, mined_at=datetime.now(UTC), search_hits=len(numbers), pairs=pairs)
