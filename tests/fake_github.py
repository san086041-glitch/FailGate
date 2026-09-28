"""假的 GitHub REST API：只实现 FailGate 用到的接口，状态存在内存里，便于断言。"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from email.utils import formatdate
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from failgate.platforms.github_app import GitHubApp

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_KEY = _KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
PUBLIC_KEY = _KEY.public_key()

BOT = {"login": "failgate-test[bot]", "type": "Bot"}


@dataclass
class FakeGitHub:
    labels: list[str] = field(default_factory=lambda: ["bug", "area:io", "question"])
    # login → role_name
    permissions: dict[str, str] = field(default_factory=dict)
    # (repo, number) → 评论列表
    comments: dict[tuple[str, int], list[dict[str, Any]]] = field(default_factory=dict)
    issue_labels: dict[tuple[str, int], list[str]] = field(default_factory=dict)
    requests: list[tuple[str, str]] = field(default_factory=list)
    tokens_issued: int = 0
    # 预设的失败：按顺序匹配 (方法, 路径正则)，命中一次就消耗掉
    failures: list[tuple[str, str, httpx.Response]] = field(default_factory=list)
    token_ttl: str = "2099-01-01T00:00:00Z"
    # 服务器时间 - 真实时间（秒）；用来模拟"本机时钟不准"
    server_offset: float = 0.0
    jwt_rejections: int = 0
    _next_id: int = 1000

    def fail(self, method: str, path_regex: str, response: httpx.Response) -> None:
        self.failures.append((method, path_regex, response))

    @property
    def writes(self) -> list[tuple[str, str]]:
        return [r for r in self.requests if r[0] in {"POST", "PATCH", "DELETE"}
                and "/access_tokens" not in r[1]]

    def app(self, **kwargs: Any) -> GitHubApp:
        async def no_sleep(_: float) -> None:
            return None

        return GitHubApp(
            "12345", PRIVATE_KEY, base_url="https://gh.test", transport=self.transport,
            sleep=no_sleep, **kwargs,
        )

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        resp = self._dispatch(request)
        # 真实的 GitHub 每个响应都带 Date 头
        resp.headers["date"] = formatdate(time.time() + self.server_offset, usegmt=True)
        return resp

    def _jwt_ok(self, request: httpx.Request) -> bool:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return True
        claims = jwt.decode(
            auth[7:], PUBLIC_KEY, algorithms=["RS256"],
            options={"verify_exp": False, "verify_iat": False},
        )
        now = time.time() + self.server_offset
        # GitHub 拒绝"签发时间在未来"或已过期的 JWT
        return claims["iat"] <= now < claims["exp"]

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, urllib.parse.unquote(request.url.path)
        self.requests.append((method, path))
        if not self._jwt_ok(request):
            self.jwt_rejections += 1
            return httpx.Response(401, json={"message": "Bad credentials"})
        for i, (m, pattern, resp) in enumerate(self.failures):
            if m == method and re.search(pattern, path):
                del self.failures[i]
                return resp
        body = json.loads(request.content) if request.content else {}
        for route_method, pattern, handler in self._routes():
            match = re.fullmatch(pattern, path)
            if route_method == method and match:
                return handler(request, body, *match.groups())
        return httpx.Response(404, json={"message": "Not Found"})

    def _routes(self) -> list[tuple[str, str, Callable[..., httpx.Response]]]:
        repo = r"/repos/([^/]+/[^/]+)"
        return [
            ("GET", r"/app", self._get_app),
            ("GET", r"/app/installations", self._installations),
            ("POST", r"/app/installations/(\d+)/access_tokens", self._token),
            ("GET", repo + r"/labels", self._list_labels),
            ("GET", repo + r"/collaborators/([^/]+)/permission", self._permission),
            ("GET", repo + r"/issues/(\d+)/comments", self._list_comments),
            ("POST", repo + r"/issues/(\d+)/comments", self._create_comment),
            ("PATCH", repo + r"/issues/comments/(\d+)", self._update_comment),
            ("POST", repo + r"/issues/(\d+)/labels", self._add_labels),
            ("DELETE", repo + r"/issues/(\d+)/labels/(.+)", self._remove_label),
        ]

    def _get_app(self, req: httpx.Request, body: Any) -> httpx.Response:
        assert req.headers["authorization"].startswith("Bearer ")
        return httpx.Response(200, json={"id": 12345, "slug": "failgate-test", "name": "RW"})

    def _installations(self, req: httpx.Request, body: Any) -> httpx.Response:
        return httpx.Response(200, json=[{"id": 42, "account": {"login": "acme"}}])

    def _token(self, req: httpx.Request, body: Any, inst: str) -> httpx.Response:
        assert req.headers["authorization"].startswith("Bearer ")
        self.tokens_issued += 1
        return httpx.Response(
            201, json={"token": f"ghs_fake{self.tokens_issued}", "expires_at": self.token_ttl}
        )

    def _list_labels(self, req: httpx.Request, body: Any, repo: str) -> httpx.Response:
        page = int(req.url.params.get("page", "1"))
        per = int(req.url.params.get("per_page", "30"))
        chunk = self.labels[(page - 1) * per : page * per]
        desc = {"area:io": "Reading and writing files"}
        return httpx.Response(
            200, json=[{"name": n, "description": desc.get(n, "")} for n in chunk]
        )

    def _permission(self, req: httpx.Request, body: Any, repo: str, login: str) -> httpx.Response:
        role = self.permissions.get(login)
        if role is None:
            return httpx.Response(404, json={"message": "Not a collaborator"})
        # 粗粒度字段只有 admin / write / read / none
        coarse = {"admin": "admin", "maintain": "write", "write": "write"}.get(role, "read")
        return httpx.Response(200, json={"permission": coarse, "role_name": role})

    def _list_comments(self, req: httpx.Request, body: Any, repo: str, n: str) -> httpx.Response:
        return httpx.Response(200, json=self.comments.get((repo, int(n)), []))

    def _create_comment(self, req: httpx.Request, body: Any, repo: str, n: str) -> httpx.Response:
        assert req.headers["authorization"].startswith("token ghs_")
        self._next_id += 1
        comment = {"id": self._next_id, "body": body["body"], "user": BOT}
        self.comments.setdefault((repo, int(n)), []).append(comment)
        return httpx.Response(201, json=comment)

    def _update_comment(self, req: httpx.Request, body: Any, repo: str, cid: str) -> httpx.Response:
        for comments in self.comments.values():
            for c in comments:
                if c["id"] == int(cid):
                    c["body"] = body["body"]
                    return httpx.Response(200, json=c)
        return httpx.Response(404, json={"message": "Not Found"})

    def _add_labels(self, req: httpx.Request, body: Any, repo: str, n: str) -> httpx.Response:
        current = self.issue_labels.setdefault((repo, int(n)), [])
        current += [x for x in body["labels"] if x not in current]
        return httpx.Response(200, json=[{"name": x} for x in current])

    def _remove_label(
        self, req: httpx.Request, body: Any, repo: str, n: str, name: str
    ) -> httpx.Response:
        current = self.issue_labels.get((repo, int(n)), [])
        if name not in current:
            return httpx.Response(404, json={"message": "Label does not exist"})
        current.remove(name)
        return httpx.Response(200, json=[])
