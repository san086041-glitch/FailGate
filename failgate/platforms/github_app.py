"""GitHub App 客户端：以 App 身份读写仓库（技术方案第 6 节）。

认证分两步：
1. 用 App 私钥签一个 JWT（RS256，最长 10 分钟），证明"我是这个 App"；
2. 拿 JWT 换某个安装（installation）的访问令牌，有效期 1 小时，缓存起来，快过期时再换。
之后所有仓库 API 都带安装令牌调用，评论会显示为 "xxx[bot]" 发的。

没有用 githubkit：只用到七八个接口，直接用 httpx 写更透明，
也和项目里其余的 HTTP 代码一致（ADR 0004）。
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx
import jwt

from .base import CaseRef, Comment, Label, PlatformError, RepoRef, User

log = logging.getLogger(__name__)

# 安装令牌剩余有效期低于这个值就提前刷新，避免请求发出去时刚好过期
TOKEN_REFRESH_MARGIN = 300
# 限流时最多等待多久；再长就放弃，交给执行器下次重试
MAX_RATE_LIMIT_WAIT = 60.0
# 本机时钟和 GitHub 相差超过这个值，JWT 就可能被拒，需要按服务器时间重签
CLOCK_SKEW_RETRY = 30.0
_KNOWN_ROLES = frozenset({"admin", "maintain", "write", "triage", "read"})

Sleep = Callable[[float], Awaitable[None]]


class GitHubApiError(PlatformError):
    def __init__(self, status: int, message: str, *, rate_limited: bool = False) -> None:
        # 5xx 和限流可以稍后重试；其余 4xx（权限不足、参数错误）重试也没用
        super().__init__(
            status, f"GitHub API {status}: {message}", retryable=status >= 500 or rate_limited
        )


@dataclass
class _Token:
    value: str
    expires_at: float


def app_jwt(app_id: str, private_key: str, now: float | None = None) -> str:
    """App 级 JWT。iat 往前拨 60 秒，容忍本机和 GitHub 之间的时钟偏差。"""
    now = time.time() if now is None else now
    payload = {"iat": int(now) - 60, "exp": int(now) + 540, "iss": app_id}
    return jwt.encode(payload, private_key, algorithm="RS256")


class GitHubApp:
    """一个 App 对应一个实例；按安装 ID 缓存令牌，按仓库缓存标签表。"""

    def __init__(
        self,
        app_id: str,
        private_key: str,
        *,
        base_url: str = "https://api.github.com",
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        label_ttl: float = 600.0,
    ) -> None:
        self.app_id = app_id
        self._key = private_key
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "FailGate",
            },
            timeout=30,
            transport=transport,
        )
        self._sleep = sleep
        # GitHub 服务器时间 - 本机时间（秒），从响应头 Date 学到；
        # 签 JWT、判断令牌过期都用校正后的时间
        self._skew = 0.0
        self._tokens: dict[int, _Token] = {}
        self._token_lock = asyncio.Lock()
        self._label_ttl = label_ttl
        self._labels: dict[tuple[int, str], tuple[float, list[Label]]] = {}

    @classmethod
    def from_key_file(cls, app_id: str, key_path: str, **kwargs: Any) -> GitHubApp:
        return cls(app_id, Path(key_path).read_text(encoding="utf-8"), **kwargs)

    async def aclose(self) -> None:
        await self._http.aclose()

    def installation(self, installation_id: int) -> InstallationClient:
        return InstallationClient(self, installation_id)

    # ---- App 级接口（用 JWT）----

    async def get_app(self) -> dict[str, Any]:
        return await self._app_request("GET", "/app")

    async def list_installations(self) -> list[dict[str, Any]]:
        return await self._app_request("GET", "/app/installations")

    def _now(self) -> float:
        return time.time() + self._skew

    async def _app_request(self, method: str, url: str) -> Any:
        """用 JWT 调 App 级接口。本机时钟偏差大时 GitHub 会返回 401：按服务器时间重签，重试一次。"""
        skew_before = self._skew
        try:
            return await self._request(
                method, url, auth=f"Bearer {app_jwt(self.app_id, self._key, self._now())}"
            )
        except GitHubApiError as e:
            if e.status != 401 or abs(self._skew - skew_before) < CLOCK_SKEW_RETRY:
                raise
            log.warning("local clock is off by %.0fs vs GitHub; re-signing JWT", -self._skew)
            return await self._request(
                method, url, auth=f"Bearer {app_jwt(self.app_id, self._key, self._now())}"
            )

    async def installation_token(self, installation_id: int) -> str:
        cached = self._tokens.get(installation_id)
        if cached and cached.expires_at - self._now() > TOKEN_REFRESH_MARGIN:
            return cached.value
        # 加锁：并发的多个请求同时发现令牌过期时，只换一次
        async with self._token_lock:
            cached = self._tokens.get(installation_id)
            if cached and cached.expires_at - self._now() > TOKEN_REFRESH_MARGIN:
                return cached.value
            data = await self._app_request(
                "POST", f"/app/installations/{installation_id}/access_tokens"
            )
            expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
            self._tokens[installation_id] = _Token(data["token"], expires.timestamp())
            return data["token"]

    # ---- 通用请求：限流退避 + 错误归一化 ----

    async def _request(
        self, method: str, url: str, *, auth: str, json: Any = None, retries: int = 2
    ) -> Any:
        for attempt in range(retries + 1):
            r = await self._http.request(method, url, json=json, headers={"Authorization": auth})
            self._observe_clock(r)
            wait = _rate_limit_wait(r, self._now())
            if wait is not None and attempt < retries and wait <= MAX_RATE_LIMIT_WAIT:
                log.warning("GitHub rate limited on %s %s, waiting %.0fs", method, url, wait)
                await self._sleep(wait)
                continue
            if r.status_code >= 400:
                raise GitHubApiError(
                    r.status_code, _error_message(r), rate_limited=wait is not None
                )
            return r.json() if r.content else None
        raise AssertionError("unreachable")

    def _observe_clock(self, r: httpx.Response) -> None:
        date = r.headers.get("date")
        if not date:
            return
        try:
            self._skew = parsedate_to_datetime(date).timestamp() - time.time()
        except (TypeError, ValueError):
            pass


def _rate_limit_wait(r: httpx.Response, now: float) -> float | None:
    """GitHub 的两种限流：主限额（剩余次数为 0，等到 reset）和二级限流（带 retry-after）。"""
    if r.status_code not in {403, 429}:
        return None
    if "retry-after" in r.headers:
        return float(r.headers["retry-after"])
    if r.headers.get("x-ratelimit-remaining") == "0" and "x-ratelimit-reset" in r.headers:
        # reset 是服务器时间戳，要和服务器时间比
        return max(0.0, float(r.headers["x-ratelimit-reset"]) - now) + 1
    # 403 但不是限流（例如 App 没有这个权限）
    return None if r.status_code == 403 else 60.0


def _error_message(r: httpx.Response) -> str:
    try:
        return str(r.json().get("message", r.text))[:300]
    except ValueError:
        return r.text[:300]


def comment_from_api(c: dict[str, Any]) -> Comment:
    """REST 返回的评论 → 领域模型（GitHubRest 和 InstallationClient 共用）。"""
    user = c.get("user") or {}
    login = user.get("login", "")
    return Comment(
        id=str(c["id"]),
        author=User(
            login=login,
            is_bot=user.get("type") == "Bot" or login.endswith("[bot]"),
            association=c.get("author_association") or "NONE",
        ),
        body=c.get("body") or "",
        created_at=datetime.fromisoformat(c["created_at"].replace("Z", "+00:00")),
    )


class InstallationClient:
    """某个安装下的读写客户端，实现 PlatformReader 的子集和 PlatformWriter。"""

    def __init__(self, app: GitHubApp, installation_id: int) -> None:
        self.app = app
        self.installation_id = installation_id

    async def _call(self, method: str, url: str, json: Any = None) -> Any:
        token = await self.app.installation_token(self.installation_id)
        return await self.app._request(method, url, auth=f"token {token}", json=json)

    # ---- 读 ----

    async def list_labels(self, repo: RepoRef) -> list[Label]:
        key = (self.installation_id, repo.full_name)
        hit = self.app._labels.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        labels: list[Label] = []
        page = 1
        while True:
            data = await self._call(
                "GET", f"/repos/{repo.full_name}/labels?per_page=100&page={page}"
            )
            labels += [Label(name=x["name"], description=x.get("description") or "") for x in data]
            if len(data) < 100:
                break
            page += 1
        self.app._labels[key] = (time.monotonic() + self.app._label_ttl, labels)
        return labels

    async def get_permission(self, repo: RepoRef, login: str) -> str:
        """实时查询某人对仓库的权限：admin / maintain / write / triage / read / none。"""
        try:
            data = await self._call(
                "GET", f"/repos/{repo.full_name}/collaborators/{login}/permission"
            )
        except GitHubApiError as e:
            if e.status == 404:  # 不是协作者
                return "none"
            raise
        # role_name 区分 maintain / triage；自定义角色名不认识时退回粗粒度的 permission 字段
        role = data.get("role_name")
        if role in _KNOWN_ROLES:
            return str(role)
        return str(data.get("permission") or "none")

    async def list_comments(self, ref: CaseRef) -> list[Comment]:
        comments: list[Comment] = []
        page = 1
        while True:
            data = await self._call(
                "GET",
                f"/repos/{ref.repo.full_name}/issues/{ref.number}/comments"
                f"?per_page=100&page={page}",
            )
            comments += [comment_from_api(c) for c in data]
            if len(data) < 100:
                return comments
            page += 1

    async def find_comment(self, ref: CaseRef, marker: str) -> str | None:
        """找机器人自己之前发的、带隐藏标记的评论（用于崩溃恢复后的去重）。"""
        page = 1
        while True:
            data = await self._call(
                "GET",
                f"/repos/{ref.repo.full_name}/issues/{ref.number}/comments"
                f"?per_page=100&page={page}",
            )
            for c in data:
                if (c.get("user") or {}).get("type") == "Bot" and marker in (c.get("body") or ""):
                    return str(c["id"])
            if len(data) < 100:
                return None
            page += 1

    # ---- 写（只有 EffectExecutor 调用）----

    async def create_comment(self, ref: CaseRef, body: str) -> str:
        data = await self._call(
            "POST", f"/repos/{ref.repo.full_name}/issues/{ref.number}/comments", {"body": body}
        )
        return str(data["id"])

    async def update_comment(self, ref: CaseRef, comment_id: str, body: str) -> None:
        await self._call(
            "PATCH", f"/repos/{ref.repo.full_name}/issues/comments/{comment_id}", {"body": body}
        )

    async def set_labels(self, ref: CaseRef, add: list[str], remove: list[str]) -> None:
        # POST 是"追加"而不是"覆盖"，不会冲掉维护者已经打上的标签
        if add:
            await self._call(
                "POST", f"/repos/{ref.repo.full_name}/issues/{ref.number}/labels", {"labels": add}
            )
        for name in remove:
            try:
                await self._call(
                    "DELETE", f"/repos/{ref.repo.full_name}/issues/{ref.number}/labels/"
                    + urllib.parse.quote(name, safe="")
                )
            except GitHubApiError as e:
                if e.status != 404:  # 标签本来就不在，视为成功
                    raise
