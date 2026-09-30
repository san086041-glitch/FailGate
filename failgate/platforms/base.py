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
WRITE_PERMISSIONS = frozenset({"admin", "maintain", "write"})


class PlatformError(Exception):
    """平台 API 出错。执行器只看 status 和 retryable，不关心具体是哪个平台。"""

    def __init__(self, status: int, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


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
    # 实时查询到的仓库权限（admin / maintain / write / triage / read / none）；None = 没查过
    permission: str | None = None

    @property
    def can_write(self) -> bool:
        # 优先用实时权限。webhook 里的 author_association 只是近似：
        # 只读协作者也是 COLLABORATOR，组织成员（MEMBER）也不一定能写这个仓库
        if self.permission is not None:
            return self.permission in WRITE_PERMISSIONS
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
    # W3C trace context（traceparent）：webhook 入口注入，跟着事件进队列（ADR 0025）
    trace: dict[str, str] = {}


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
    """对外写操作。只有 EffectExecutor 持有；开 PR、推分支等接口在 M3 加入。"""

    async def create_comment(self, ref: CaseRef, body: str) -> str: ...
    async def update_comment(self, ref: CaseRef, comment_id: str, body: str) -> None: ...
    # 找机器人自己发过的、带隐藏标记的评论；严格说是读操作，放在这里是因为只有执行器需要它
    async def find_comment(self, ref: CaseRef, marker: str) -> str | None: ...
    async def set_labels(self, ref: CaseRef, add: list[str], remove: list[str]) -> None: ...


class Platform(Protocol):
    name: str

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool: ...
    def parse_event(self, headers: Mapping[str, str], body: bytes) -> DomainEvent | None: ...
