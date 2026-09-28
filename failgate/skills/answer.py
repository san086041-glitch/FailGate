"""Answer：用项目文档和历史 issue 里维护者的回答，带引用地回答提问；找不到依据就不答。

流程：检索资料（文档块 BM25 + 相似 issue 里维护者的评论）→ 资料编号 S1..Sn 交给模型 →
finalize_answer() 做三道程序化检查：
1. 引用的编号必须是这次给出的资料（模型不能编造出处）；
2. 每条引用都要附原文摘录，并且能在资料原文里逐字找到（和查重的引文核对是同一个函数）；
3. 答案里模型自己写的链接一律去掉，链接只由程序根据资料编号生成。
通过检查、且置信度 ≥ 0.75、至少有一条有效引用，才算"已回答"（技术方案第 11 节）。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from failgate.index.text import strip_boilerplate
from failgate.llm import Usage
from failgate.platforms.base import WRITE_ASSOCIATIONS, Comment

from .base import SkillContext, SkillResult, load_prompt, priced, render, untrusted
from .dedup import quote_found

MIN_CONFIDENCE = 0.75
DOC_K = 6
RECALL_K = 5
ISSUE_K = 3  # 最多读几个相似 issue 的评论
MAX_COMMENTS_PER_ISSUE = 2
MAX_SOURCE_CHARS = 1200
MAX_QUESTION_CHARS = 400

_MD_LINK = re.compile(r"\[([^\]]*)\]\((?:https?://|www\.)[^)\s]*\)")
_BARE_URL = re.compile(r"(?:https?://|www\.)[^\s)>\]]+")
# 代码块（```…```）和行内代码（`…`）；用捕获组 split，奇数下标就是代码
_CODE = re.compile(r"(```[\s\S]*?```|`[^`\n]+`)")
_MENTION = re.compile(r"(?<![\w`])@(?=[A-Za-z0-9])")
_MARKER = re.compile(r"\[(S\d+(?:\s*[,，、]\s*S\d+)*)\]")
# 机器人汇总评论的隐藏标记：自己以前的回答不能当作"维护者的依据"
# 改名前的评论带的是 repowarden 标记，两种都认
_BOT_MARKERS = ("<!-- failgate:", "<!-- repowarden:")


class Source(BaseModel):
    id: str
    kind: Literal["doc", "issue"]
    title: str
    url: str
    text: str


class RawCitation(BaseModel):
    id: str
    quote: str = ""


class RawAnswer(BaseModel):
    abstain: bool = False
    answer: str = ""
    citations: list[RawCitation] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0, le=1)
    missing: str = ""


class Reference(BaseModel):
    n: int
    id: str
    kind: Literal["doc", "issue"]
    title: str
    url: str


class CitationCheck(BaseModel):
    id: str
    quote: str
    known_source: bool
    quote_found: bool


class AnswerOutput(BaseModel):
    # answered：通过全部检查 · abstained：模型拒答或没有资料 · rejected：没通过检查
    status: Literal["answered", "abstained", "rejected"]
    reject_reason: str = ""
    # 编号已经换成带链接的 [1] [2]…（Markdown），和 references 对应
    answer_md: str = ""
    references: list[Reference] = Field(default_factory=list)
    confidence: float = 0.0
    missing: str = ""
    # 以下是审计和回放用的原始事实。
    # draft_md：没通过检查的草稿（经过同样的处理），永远不会发出，只用于人工复核和离线调阈值
    draft_md: str = ""
    citations: list[CitationCheck] = Field(default_factory=list)
    removed_links: int = 0
    dropped_markers: list[str] = Field(default_factory=list)
    sources: list[dict[str, str]] = Field(default_factory=list)


def strip_links(text: str) -> tuple[str, int]:
    """去掉模型自己写的链接：[文字](网址) 只留文字，裸网址直接删掉。

    代码块和行内代码里的网址保留：GitHub 不会把它们渲染成可点击的链接，
    删掉反而会破坏代码示例（实测踩过：只剩一个孤零零的反引号）。
    """
    n = 0

    def keep_text(m: re.Match[str]) -> str:
        nonlocal n
        n += 1
        return m.group(1)

    parts = _CODE.split(text)
    for i in range(0, len(parts), 2):  # 偶数下标是代码以外的部分
        parts[i] = _MD_LINK.sub(keep_text, parts[i])
        parts[i], bare = _BARE_URL.subn("", parts[i])
        n += bare
    return "".join(parts), n


def finalize_answer(
    raw: RawAnswer, sources: list[Source], *, min_confidence: float = MIN_CONFIDENCE
) -> AnswerOutput:
    """由模型的原始输出和资料推导最终结论。纯函数：线上和离线评测共用。"""
    by_id = {s.id: s for s in sources}
    meta = [{"id": s.id, "kind": s.kind, "title": s.title, "url": s.url} for s in sources]
    checks: list[CitationCheck] = []
    verified: set[str] = set()
    for c in raw.citations:
        src = by_id.get(c.id)
        found = src is not None and bool(c.quote.strip()) and quote_found(c.quote, src.text)
        checks.append(
            CitationCheck(id=c.id, quote=c.quote, known_source=src is not None, quote_found=found)
        )
        if found:
            verified.add(c.id)

    base: dict[str, Any] = {
        "confidence": raw.confidence,
        "missing": raw.missing,
        "citations": checks,
        "sources": meta,
    }
    if raw.abstain or not sources:
        return AnswerOutput(status="abstained", **base)

    text, removed = strip_links(raw.answer)
    numbering: dict[str, int] = {}
    dropped: list[str] = []

    def renumber(m: re.Match[str]) -> str:
        ids = [x.strip() for x in re.split(r"[,，、]", m.group(1))]
        out = []
        for sid in ids:
            if sid in verified:
                numbering.setdefault(sid, len(numbering) + 1)
                out.append(f"[[{numbering[sid]}]]({by_id[sid].url})")
            else:
                dropped.append(sid)
        return "".join(out)

    text = re.sub(r"[ \t]+\n", "\n", _MARKER.sub(renumber, text)).strip()
    # "@某人" 会给那个人发通知：机器人不该替模型去 @ 任何人，插一个零宽空格让它失效
    text = _MENTION.sub("@\u200b", text)
    refs = [
        Reference(n=n, id=sid, kind=by_id[sid].kind, title=by_id[sid].title, url=by_id[sid].url)
        for sid, n in sorted(numbering.items(), key=lambda kv: kv[1])
    ]
    base.update(removed_links=removed, dropped_markers=dropped, draft_md=text)
    if not refs:
        return AnswerOutput(status="rejected", reject_reason="no_verified_citation", **base)
    if raw.confidence < min_confidence:
        return AnswerOutput(status="rejected", reject_reason="low_confidence", **base)
    return AnswerOutput(status="answered", answer_md=text, references=refs, **base)


def _before(comment: Comment, cutoff: datetime | None) -> bool:
    if cutoff is None:
        return True
    c = cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=UTC)
    return comment.created_at < c


def maintainer_answers(comments: list[Comment], cutoff: datetime | None) -> list[str]:
    """只取有仓库权限的人（OWNER / MEMBER / COLLABORATOR）在 cutoff 之前写的评论。

    普通用户的评论不能当依据：任何人都能在 issue 下面写"这样配置就行"，那等于让路人给机器人喂答案。
    """
    return [
        c.body.strip()
        for c in comments
        if not c.author.is_bot
        and c.author.association in WRITE_ASSOCIATIONS
        and not any(m in c.body for m in _BOT_MARKERS)
        and c.body.strip()
        and _before(c, cutoff)
    ][:MAX_COMMENTS_PER_ISSUE]


class AnswerSkill:
    name = "answer"

    def __init__(
        self, *, min_confidence: float = MIN_CONFIDENCE, prompt_version: str = "1"
    ) -> None:
        self.min_confidence = min_confidence
        self.version = prompt_version

    async def gather(self, ctx: SkillContext) -> list[Source]:
        issue = ctx.issue
        # 去掉 issue 模板里的 HTML 注释（"Please make sure that the bug is not already fixed…"），
        # 它们和问题无关却会主导 BM25；标题最能概括问题，重复一次加权
        body = strip_boilerplate(issue.body, frozenset())
        query = f"{issue.title}\n{issue.title}\n{body[:2000]}"
        sources: list[Source] = []
        if ctx.docs is not None and issue.repo_id is not None:
            for hit in await ctx.docs.search(issue.repo_id, query, k=DOC_K):
                title = f"{hit.path} › {hit.heading}" if hit.heading else hit.path
                sources.append(
                    Source(id="", kind="doc", title=title, url=hit.url,
                           text=hit.text[:MAX_SOURCE_CHARS])
                )
        if ctx.retriever is not None and ctx.comments is not None and issue.repo_id is not None:
            recalled = await ctx.retriever.search(
                issue.repo_id,
                title=issue.title,
                body=issue.body,
                trace=None,
                exclude_number=issue.number,
                before=issue.created_at,
                k=RECALL_K,
            )
            used = 0
            for r in recalled:
                if used >= ISSUE_K:
                    break
                answers = maintainer_answers(
                    await ctx.comments(issue.repo, r.number), issue.created_at
                )
                if not answers:
                    continue  # 没有维护者回答的 issue 不是依据
                used += 1
                body = "\n\n".join(answers)
                text = (
                    f"#{r.number} {r.title}\n问：{r.body[:MAX_QUESTION_CHARS]}\n\n"
                    f"维护者回答：{body}"
                )[:MAX_SOURCE_CHARS]
                url = r.url or f"https://github.com/{issue.repo}/issues/{r.number}"
                sources.append(
                    Source(id="", kind="issue", title=f"#{r.number} {r.title}", url=url, text=text)
                )
        return [s.model_copy(update={"id": f"S{i}"}) for i, s in enumerate(sources, start=1)]

    async def run(self, ctx: SkillContext) -> SkillResult:
        issue = ctx.issue
        sources = await self.gather(ctx)
        if not sources:
            # 没有任何资料：不调用模型，直接拒答，零花费
            out = finalize_answer(RawAnswer(abstain=True, missing="no sources"), [])
            return SkillResult(
                output=out, confidence=0.0, model=ctx.model, usage=Usage(), cost_usd=0.0,
                facts={"answered": False},
            )

        intake = ctx.prior.get("intake", {})
        lang = "中文" if intake.get("language") == "zh" else "English"
        blocks = [untrusted(f"issue#{issue.number}", "question", f"{issue.title}\n\n{issue.body}")]
        # 资料里也有外部用户写的内容（相似 issue 的标题和提问），同样按不可信数据包装；
        # 标题放进正文而不是标签属性，避免属性注入
        blocks += [untrusted(s.kind, s.id, f"【{s.title}】\n{s.text}") for s in sources]
        messages = [
            {"role": "system", "content": render(load_prompt(f"answer_v{self.version}"),
                                                 language=lang)},
            {"role": "user", "content": "\n\n".join([*blocks, "按 system 中的格式输出 JSON。"])},
        ]
        raw, usage, resp = await ctx.llm.complete_json(messages, RawAnswer, model=ctx.model)
        out = finalize_answer(raw, sources, min_confidence=self.min_confidence)
        return SkillResult(
            output=out,
            confidence=out.confidence if out.status == "answered" else 0.0,
            model=resp.model,
            usage=usage,
            cost_usd=priced(resp.model, usage),
            facts={"answered": out.status == "answered"},
        )
