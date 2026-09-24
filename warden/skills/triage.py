"""Triage：判断 issue 类型、选择标签（只能从仓库已有标签中选）、定优先级，并给出依据。"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from .base import SkillContext, SkillResult, load_prompt, priced, render, untrusted

IssueType = Literal["bug", "feature", "question", "docs", "other"]


class TriageOutput(BaseModel):
    type: IssueType
    labels: list[str] = Field(default_factory=list)
    priority: Literal["P0", "P1", "P2", "P3"] = "P2"
    rationale: str
    evidence_quotes: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    # 低质量 / 疑似批量生成内容的程度，0 = 正常
    slop_score: float = Field(default=0.0, ge=0, le=1)
    dropped_labels: list[str] = Field(default_factory=list)


def constrain_labels(proposed: list[str], allowed: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """按仓库标签表过滤（忽略大小写），返回 (保留, 丢弃)。"""
    by_lower = {label.lower(): label for label in allowed}
    kept: list[str] = []
    dropped: list[str] = []
    for label in proposed:
        match = by_lower.get(label.strip().lower())
        if match is None:
            dropped.append(label)
        elif match not in kept:
            kept.append(match)
    return kept, dropped


class TriageSkill:
    name = "triage"
    version = "1"

    async def run(self, ctx: SkillContext) -> SkillResult:
        issue = ctx.issue
        system = render(
            load_prompt(f"triage_v{self.version}"),
            labels=json.dumps(list(ctx.labels), ensure_ascii=False),
        )
        intake = ctx.prior.get("intake")
        parts = [untrusted(f"issue#{issue.number}", "u1", f"{issue.title}\n\n{issue.body}")]
        if intake:
            summary = {k: intake.get(k) for k in ("reported_version", "missing", "verifiability")}
            summary["has_traceback"] = bool(intake.get("traceback"))
            trusted = json.dumps(summary, ensure_ascii=False)
            parts.append(f"Intake 结果（程序生成，可信）：{trusted}")
        # system 提示词是中文，不显式指定时模型倾向于用中文写 rationale
        if (intake or {}).get("language") == "zh":
            parts.append("rationale 用中文书写。")
        else:
            parts.append("Write `rationale` in English.")
        parts.append("按 system 中的格式输出 JSON。")
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": "\n\n".join(parts)},
        ]
        output, usage, resp = await ctx.llm.complete_json(messages, TriageOutput, model=ctx.model)
        output.labels, output.dropped_labels = constrain_labels(output.labels, ctx.labels)
        return SkillResult(
            output=output,
            confidence=output.confidence,
            model=resp.model,
            usage=usage,
            cost_usd=priced(resp.model, usage),
            facts={"type": output.type, "slop_score": output.slop_score},
        )
