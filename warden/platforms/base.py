"""平台无关的领域模型与接口。

读写分离：PlatformReader 可以交给模型工具使用；PlatformWriter 只由 PolicyGate 持有，
从类型层面保证模型碰不到写操作（技术方案第 6 节）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel

WRITE_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


class CaseKind(StrEnum):
    ISSUE = "issue"
    PULL = "pull"


class RepoRef(BaseModel, frozen=True):
    platform: str
    full_name: str


class CaseRef(BaseModel, frozen=True):
    repo: RepoRef
    kind: CaseKind
    number: int


class User(BaseModel, frozen=True):
    login: str
    is_bot: bool = False
    # OWNER / MEMBER / COLLABORATOR / CONTRIBUTOR / NONE …
    association: str = "NONE"

    @property
    def can_write(self) -> bool:
        # M0 用 webhook 里的 author_association 近似；M1 改为实时查询仓库权限 API
        return self.association in WRITE_ASSOCIATIONS


class DomainEvent(BaseModel, frozen=True):
    """归一化后的平台事件，编排层只认这个模型。"""

    platform: str
    delivery_id: str
    # issue.opened / issue.closed / comment.created / pull.opened …
    name: str
    repo: RepoRef
    case: CaseRef | None = None
    actor: User
    title: str = ""
    body: str = ""
    installation_id: int | None = None


class Label(BaseModel):
    name: str
    description: str = ""


class Issue(BaseModel):
    ref: CaseRef
    title: str
    body: str
    author: User
    labels: list[str] = []
    state: str
    created_at: datetime


class Comment(BaseModel):
    id: str
    author: User
    body: str
    created_at: datetime


class PlatformReader(Protocol):
    async def get_issue(self, ref: CaseRef) -> Issue: ...
    async def list_comments(self, ref: CaseRef) -> list[Comment]: ...
    async def list_labels(self, repo: RepoRef) -> list[Label]: ...
    def search_issues(self, repo: RepoRef, since: datetime) -> AsyncIterator[Issue]: ...


class PlatformWriter(Protocol):
    async def comment(self, ref: CaseRef, body: str) -> None: ...
    async def set_labels(self, ref: CaseRef, add: list[str], remove: list[str]) -> None: ...
    async def open_pull(
        self, repo: RepoRef, branch: str, title: str, body: str, draft: bool = True
    ) -> str: ...


class Platform(Protocol):
    name: str

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool: ...
    def parse_event(self, headers: Mapping[str, str], body: bytes) -> DomainEvent | None: ...
