"""合成负载（W6 排队延迟测量）：往本地服务发签名 webhook。

线上一轮只有几个快事件，只够看中位数。这里按固定剧本发几十个事件，给 p50 / p95：

    t=0      在 PR 上发 `/failgate verify`（慢：核验 + 强度 + 隐藏考卷，每个约 1 分钟）
    t=a, a+Δ …  开"提问 / 功能请求"类 issue（快：分诊 → 查重 → 答疑 / 只分诊）

**只对影子模式的仓库用**：webhook 里的仓库名是真的（核验要从 GitHub 读 PR 和源码），
写操作只记录不执行。快事件用不存在的 issue 编号（从 base 开始），不会碰到真实 issue。
改造前后跑同一个剧本，对比 `replay latency` 的数字。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

# 快事件的题目：提问和功能请求（不含 bug 报告，免得进复现变成慢事件）
FAST_ISSUES: tuple[tuple[str, str], ...] = (
    ("How do I read a config value with a fallback when the key is missing?",
     "Some keys in my settings file are optional. Is there a built-in way to pass a default "
     "instead of getting None?"),
    ("Feature request: add a `max_length` option to `slugify()`",
     "It would be nice if slugify could cut long titles without breaking a word. "
     "Would you accept a PR?"),
    ("配置文件里能写行内注释吗？",
     "比如 `port = 8080  # http` 这样写是支持的吗？还是注释只能单独占一行？"),
    ("What is the minimum supported Python version?",
     "I'd like to use this package on Python 3.9 but couldn't find it in the README."),
    ("Is there a changelog?", "Where can I see what changed between 0.2 and 0.3?"),
    ("Feature request: support `:` as a key/value separator in config files",
     "Some of our files use `key: value`. Could parse() accept both?"),
    ("slugify 能保留中文吗？", "我想生成带中文的 URL，有没有选项不把非 ASCII 字符去掉？"),
    ("How do I run the tests locally?", "Which command should I use, and do I need extra deps?"),
    ("Feature request: `unique()` with a key function",
     "Like `unique(users, key=lambda u: u.email)`. Would that fit the API?"),
    ("Is this package thread-safe?", "Can I call parse() from several threads at once?"),
)


@dataclass(frozen=True)
class Planned:
    at: float          # 相对开始的秒数
    event: str         # X-GitHub-Event
    payload: dict[str, Any]
    label: str


def _user(login: str) -> dict[str, Any]:
    return {"login": login, "id": abs(hash(login)) % 10**8, "type": "User"}


def issue_opened(repo: str, number: int, title: str, body: str, *, author: str,
                 installation: int | None) -> dict[str, Any]:
    return {
        "action": "opened",
        "issue": {"number": number, "title": title, "body": body, "user": _user(author),
                  "author_association": "NONE"},
        "repository": {"full_name": repo},
        "installation": {"id": installation} if installation else None,
        "sender": _user(author),
    }


def pull_command(repo: str, number: int, body: str, *, login: str,
                 installation: int | None) -> dict[str, Any]:
    return {
        "action": "created",
        "issue": {"number": number, "user": _user(login),
                  "pull_request": {"url": f"https://api.github.com/repos/{repo}/pulls/{number}"}},
        "comment": {"body": body, "user": _user(login), "author_association": "OWNER"},
        "repository": {"full_name": repo},
        "installation": {"id": installation} if installation else None,
        "sender": _user(login),
    }


def mixed_scenario(repo: str, *, pulls: Sequence[int], fast: int, first_fast_at: float,
                   interval: float, base: int, maintainer: str,
                   installation: int | None = None) -> list[Planned]:
    """剧本：t=0 对每个 PR 发一次 verify，之后每 interval 秒开一个快 issue。"""
    plan = [Planned(0.0, "issue_comment",
                    pull_command(repo, n, "/failgate verify", login=maintainer,
                                 installation=installation), f"verify PR #{n}")
            for n in pulls]
    for i in range(fast):
        title, body = FAST_ISSUES[i % len(FAST_ISSUES)]
        plan.append(Planned(first_fast_at + i * interval, "issues",
                            issue_opened(repo, base + i, title, body, author="load-tester",
                                         installation=installation),
                            f"issue #{base + i}"))
    return sorted(plan, key=lambda p: p.at)


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def send_plan(plan: Sequence[Planned], url: str, secret: str, *,
                    client: httpx.AsyncClient | None = None,
                    echo: Callable[[str], None] = print) -> list[int]:
    """按剧本的时间点发出，返回每个请求的状态码。"""
    own = client is None
    http = client or httpx.AsyncClient(timeout=30)
    codes: list[int] = []
    t0 = time.monotonic()
    try:
        for p in plan:
            delay = p.at - (time.monotonic() - t0)
            if delay > 0:
                await asyncio.sleep(delay)
            body = json.dumps(p.payload).encode()
            r = await http.post(url, content=body, headers={
                "X-GitHub-Event": p.event,
                "X-GitHub-Delivery": f"load-{uuid.uuid4()}",
                "X-Hub-Signature-256": sign(secret, body),
                "Content-Type": "application/json",
            })
            codes.append(r.status_code)
            echo(f"{time.monotonic() - t0:6.1f}s  {p.label}  → {r.status_code}")
    finally:
        if own:
            await http.aclose()
    return codes
