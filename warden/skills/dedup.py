"""Dedup：多路召回（见 warden/index/store.py）→ LLM 逐个判断是否同一根因 → 引文核对 → 分级。

两阶段的原因：LLM 判断准但贵，不能拿每个新 issue 和全部历史 issue 两两比较；
召回便宜但只看字面，分数不代表"是不是重复"。所以召回只负责"找出值得判断的 8 个"，
最终分数完全由 LLM 给出，再经过引文核对这道程序化的防幻觉检查。
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from warden.index.trace import signature
from warden.llm import Usage

from .base import SkillContext, SkillResult, load_prompt, priced, render, untrusted

DEFAULT_RECALL_K = 8
DEFAULT_HIGH = 0.85
DEFAULT_LOW = 0.5
MAX_CANDIDATE_BODY = 800
# 引文在原文中找不到时的惩罚系数：模型可能在"编理由"
UNVERIFIED_QUOTE_PENALTY = 0.7
MAX_OUTPUT_CANDIDATES = 5


class Judgement(BaseModel):
    id: str
    score: float = Field(ge=0, le=1)
    # v2 提示词新增：先写差异、再单独给出"是否同一根因"的布尔判断
    differences: str = ""
    same_root_cause: bool | None = None
    reason: str = ""
    quote_new: str = ""
    quote_candidate: str = ""


class Judgements(BaseModel):
    judgements: list[Judgement]


class DedupCandidate(BaseModel):
    number: int
    title: str
    state: str
    state_reason: str | None = None
    url: str | None = None
    score: float
    raw_score: float
    level: Literal["duplicate", "related", "none"]
    quotes_verified: bool
    same_root_cause: bool | None = None
    differences: str = ""
    reason: str
    recall: dict[str, Any]


class DedupOutput(BaseModel):
    verdict: Literal["duplicate", "related", "none"]
    best: int | None = None
    best_score: float = 0.0
    candidates: list[DedupCandidate] = Field(default_factory=list)
    recalled: int = 0


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def quote_found(quote: str, source: str) -> bool:
    """非空引文必须逐字出现在原文里（忽略空白和大小写）；空引文由调用方决定是否允许。"""
    q = _norm(quote).strip("\"'“”‘’…. ")
    return not q or q in _norm(source)


def classify(score: float, high: float, low: float) -> Literal["duplicate", "related", "none"]:
    if score >= high:
        return "duplicate"
    if score >= low:
        return "related"
    return "none"


class DedupSkill:
    name = "dedup"

    def __init__(
        self,
        *,
        recall_k: int = DEFAULT_RECALL_K,
        high: float = DEFAULT_HIGH,
        low: float = DEFAULT_LOW,
        prompt_version: str = "2",
    ) -> None:
        self.recall_k, self.high, self.low = recall_k, high, low
        # 版本号写进 runs 表，回放评测时可以按提示词版本对比
        self.version = prompt_version

    async def run(self, ctx: SkillContext) -> SkillResult:
        issue = ctx.issue
        intake = ctx.prior.get("intake", {})
        triage_type = ctx.prior.get("triage", {}).get("type")
        facts: dict[str, Any] = {"type": triage_type, "repro_enabled": False}

        recalled = []
        if ctx.retriever is not None and issue.repo_id is not None:
            recalled = await ctx.retriever.search(
                issue.repo_id,
                title=issue.title,
                body=issue.body,
                trace=signature(intake.get("traceback")),
                exclude_number=issue.number,
                before=issue.created_at,
                k=self.recall_k,
            )
        if not recalled:
            # 没有可比较的历史 issue：不调用模型，零花费
            return SkillResult(
                output=DedupOutput(verdict="none"),
                confidence=1.0,
                model=ctx.model,
                usage=Usage(),
                cost_usd=0.0,
                facts={**facts, "dup_high": False},
            )

        by_id = {f"c{i}": r for i, r in enumerate(recalled, start=1)}
        lang = "中文" if intake.get("language") == "zh" else "English"
        blocks = [untrusted(f"issue#{issue.number}", "new", f"{issue.title}\n\n{issue.body}")]
        for cid, r in by_id.items():
            meta = json.dumps(
                {"state": r.state, "state_reason": r.state_reason, "labels": r.labels},
                ensure_ascii=False,
            )
            text = f"#{r.number} {r.title}\n{meta}\n\n{r.body[:MAX_CANDIDATE_BODY]}"
            blocks.append(untrusted(f"issue#{r.number}", cid, text))
        messages = [
            {
                "role": "system",
                "content": render(load_prompt(f"dedup_v{self.version}"), language=lang),
            },
            {"role": "user", "content": "\n\n".join([*blocks, "按 system 中的格式输出 JSON。"])},
        ]
        parsed, usage, resp = await ctx.llm.complete_json(messages, Judgements, model=ctx.model)

        new_text = f"{issue.title}\n{issue.body}"
        candidates: list[DedupCandidate] = []
        for j in parsed.judgements:
            r = by_id.get(j.id)
            if r is None:  # 模型编造了不存在的候选 id
                continue
            # 分数达到"相关"及以上时必须给出两段引文；引文还必须能在原文里找到
            has_quotes = bool(j.quote_new.strip() and j.quote_candidate.strip())
            verified = (
                (has_quotes or j.score < self.low)
                and quote_found(j.quote_new, new_text)
                and quote_found(j.quote_candidate, f"{r.title}\n{r.body}")
            )
            score = j.score if verified else j.score * UNVERIFIED_QUOTE_PENALTY
            level = classify(score, self.high, self.low)
            if level == "duplicate" and j.same_root_cause is False:
                # 模型自己说"不是同一根因"时，分数再高也最多算"相关"
                level = "related"
            candidates.append(
                DedupCandidate(
                    number=r.number,
                    title=r.title,
                    state=r.state,
                    state_reason=r.state_reason,
                    url=r.url,
                    score=round(score, 3),
                    raw_score=j.score,
                    level=level,
                    quotes_verified=verified,
                    same_root_cause=j.same_root_cause,
                    differences=j.differences,
                    reason=j.reason,
                    recall={"rrf": r.rrf, "ranks": r.ranks, "scores": r.channel_scores},
                )
            )
        rank = {"duplicate": 2, "related": 1, "none": 0}
        candidates.sort(key=lambda c: (rank[c.level], c.score), reverse=True)
        candidates = candidates[:MAX_OUTPUT_CANDIDATES]
        best = candidates[0] if candidates else None
        verdict = best.level if best else "none"
        out = DedupOutput(
            verdict=verdict,
            best=best.number if best and verdict != "none" else None,
            best_score=best.score if best else 0.0,
            candidates=candidates,
            recalled=len(recalled),
        )
        return SkillResult(
            output=out,
            confidence=out.best_score,
            model=resp.model,
            usage=usage,
            cost_usd=priced(resp.model, usage),
            facts={**facts, "dup_high": verdict == "duplicate"},
        )
