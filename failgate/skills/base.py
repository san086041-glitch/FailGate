"""能力模块的公共契约（技术方案第 7 节）。

能力模块是纯函数式单元：输入 SkillContext，输出 SkillResult，内部不产生任何外部副作用。
写操作（打标签、发评论）由编排层根据结果经 PolicyGate 执行。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel

from failgate.llm import LLMClient, Usage
from failgate.llm.pricing import cost_usd
from failgate.platforms.base import Comment

if TYPE_CHECKING:
    from failgate.verify.engine import Verification
    from failgate.verify.receipt import SealedTest

# GitHub 新仓库的默认标签；拿不到仓库真实标签表时使用（M1 后半段改为从平台 API 读取）
DEFAULT_LABELS = (
    "bug", "documentation", "duplicate", "enhancement", "good first issue",
    "help wanted", "invalid", "question", "wontfix",
)


@dataclass(frozen=True)
class IssueSnapshot:
    repo: str
    number: int
    title: str
    body: str
    author: str | None = None
    repo_id: int | None = None
    created_at: datetime | None = None


class Retriever(Protocol):
    """查重召回接口（实现见 failgate/index/store.py 的 IssueIndex），对能力模块只读。"""

    async def search(
        self,
        repo_id: int,
        *,
        title: str,
        body: str,
        trace: Any,
        exclude_number: int,
        before: datetime | None,
        k: int,
    ) -> list[Any]: ...


class DocRetriever(Protocol):
    """文档检索接口（实现见 failgate/index/docs.py 的 DocIndex），对能力模块只读。"""

    async def search(self, repo_id: int, query: str, *, k: int) -> list[Any]: ...


# 读取某个 issue 的评论：(仓库全名, issue 编号) → 评论列表。只读
CommentSource = Callable[[str, int], Awaitable[list[Comment]]]


@dataclass
class SkillContext:
    issue: IssueSnapshot
    llm: LLMClient
    model: str
    labels: tuple[str, ...] = DEFAULT_LABELS
    # 标签名 → 仓库里写的标签说明（可能为空）；分诊提示词 v2 起会展示给模型
    label_descriptions: dict[str, str] = field(default_factory=dict)
    # 此前各模块的输出，按模块名索引
    prior: dict[str, dict[str, Any]] = field(default_factory=dict)
    retriever: Retriever | None = None
    docs: DocRetriever | None = None
    comments: CommentSource | None = None
    # 仓库级配置（如复现用的包名），由流水线从 repos 表填入
    repo_config: dict[str, Any] = field(default_factory=dict)
    # 这个 Case 还剩多少模型预算（美元）；None = 不限。长流程（复现）用它给自己设上限
    budget_left_usd: float | None = None
    # 触发进入当前阶段的人（比如发 /failgate reseal 的维护者）；内部事件触发时为 None
    actor: str | None = None


@dataclass
class SkillResult:
    output: BaseModel
    confidence: float
    model: str
    usage: Usage
    cost_usd: float
    # 提供给状态机守卫的事实，例如 {"type": "bug"}
    facts: dict[str, Any] = field(default_factory=dict)
    # 要封存的证据（收据 + 完整代码）：复现成功、或维护者重新封存时产生。
    # 由流水线写进 evidence 表（挂在对应 issue 的 Case 下），模块自己不落库
    evidence: list[SealedTest] = field(default_factory=list)
    # PR 核验的结果；由流水线写进 verifications 表
    verification: Verification | None = None


class Skill(Protocol):
    name: str
    version: str

    async def run(self, ctx: SkillContext) -> SkillResult: ...


def load_prompt(name: str) -> str:
    return resources.files("failgate.prompts").joinpath(f"{name}.md").read_text(encoding="utf-8")


def render(template: str, **values: str) -> str:
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    return template


def untrusted(source: str, ref: str, text: str) -> str:
    """把外部用户提供的内容包进 <untrusted> 标签，并转义其中伪造的同名标签。"""
    safe = text.replace("<untrusted", "&lt;untrusted").replace("</untrusted", "&lt;/untrusted")
    return f'<untrusted source="{source}" id="{ref}">\n{safe}\n</untrusted>'


def priced(model: str, usage: Usage) -> float:
    return round(cost_usd(model, usage, datetime.now(UTC)), 6)
