"""GitHub 适配器。M0 实现 webhook 校验与事件解析；读写 API（githubkit）在 M1 接入。"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any

from .base import CaseKind, CaseRef, DomainEvent, RepoRef, User

_ISSUE_ACTIONS = frozenset({"opened", "reopened", "closed", "edited"})
_PULL_ACTIONS = frozenset({"opened", "reopened", "synchronize", "closed", "edited"})


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """校验 X-Hub-Signature-256 = "sha256=" + HMAC-SHA256(secret, body)。"""
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _user(data: Mapping[str, Any], association: str | None = None) -> User:
    login = data.get("login", "")
    return User(
        login=login,
        is_bot=data.get("type") == "Bot" or login.endswith("[bot]"),
        association=association or "NONE",
    )


class GitHubPlatform:
    name = "github"

    def __init__(self, webhook_secret: str) -> None:
        self._secret = webhook_secret

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        h = {k.lower(): v for k, v in headers.items()}
        return verify_signature(self._secret, body, h.get("x-hub-signature-256"))

    def parse_event(self, headers: Mapping[str, str], body: bytes) -> DomainEvent | None:
        h = {k.lower(): v for k, v in headers.items()}
        kind, delivery = h.get("x-github-event"), h.get("x-github-delivery")
        if not kind or not delivery:
            return None
        data = json.loads(body)
        repo_data = data.get("repository")
        # installation 等不带 repository 的事件在 M1 处理（用于登记仓库）
        if not repo_data:
            return None

        action = data.get("action")
        repo = RepoRef(platform=self.name, full_name=repo_data["full_name"])
        sender = data.get("sender") or {}
        common: dict[str, Any] = {
            "platform": self.name,
            "delivery_id": delivery,
            "repo": repo,
            "installation_id": (data.get("installation") or {}).get("id"),
        }

        if kind == "issues" and action in _ISSUE_ACTIONS:
            issue = data["issue"]
            is_author = sender.get("login") == (issue.get("user") or {}).get("login")
            return DomainEvent(
                name=f"issue.{action}",
                case=CaseRef(repo=repo, kind=CaseKind.ISSUE, number=issue["number"]),
                actor=_user(sender, issue.get("author_association") if is_author else None),
                title=issue.get("title") or "",
                body=issue.get("body") or "",
                **common,
            )

        if kind == "issue_comment" and action == "created":
            issue, comment = data["issue"], data["comment"]
            # GitHub 把 PR 也当作 issue；带 pull_request 字段的是 PR 上的评论
            case_kind = CaseKind.PULL if "pull_request" in issue else CaseKind.ISSUE
            return DomainEvent(
                name="comment.created",
                case=CaseRef(repo=repo, kind=case_kind, number=issue["number"]),
                actor=_user(comment.get("user") or sender, comment.get("author_association")),
                body=comment.get("body") or "",
                **common,
            )

        if kind == "pull_request" and action in _PULL_ACTIONS:
            pull = data["pull_request"]
            is_author = sender.get("login") == (pull.get("user") or {}).get("login")
            return DomainEvent(
                name=f"pull.{action}",
                case=CaseRef(repo=repo, kind=CaseKind.PULL, number=data["number"]),
                actor=_user(sender, pull.get("author_association") if is_author else None),
                title=pull.get("title") or "",
                body=pull.get("body") or "",
                **common,
            )

        return None
