"""Intake：从 issue 里抽取版本、环境、复现步骤、预期与实际行为，列出缺失的信息。

报错堆栈由程序用规则提取（原样保留，不让模型转述）；其余字段由小模型抽取。
可验证性分按缺失字段加权计算，保证同样的输入得到同样的分数。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, Field

from .base import SkillContext, SkillResult, load_prompt, priced, untrusted

MissingField = Literal[
    "version",
    "environment",
    "repro_steps",
    "expected_behavior",
    "actual_behavior",
    "error_output",
]
MISSING_WEIGHTS: dict[str, float] = {
    "version": 0.2,
    "environment": 0.1,
    "repro_steps": 0.3,
    "expected_behavior": 0.1,
    "actual_behavior": 0.1,
    "error_output": 0.2,
}

_PY_TRACEBACK = re.compile(
    r"Traceback \(most recent call last\):\n"
    r"(?:[ \t].*\n|\n)*?"
    r"[\w.]+(?:Error|Exception|Warning|Exit)\b.*",
)
_JS_STACK = re.compile(r"(?:^.*(?:Error|Exception).*\n)(?:^\s+at .+\n?)+", re.MULTILINE)
MAX_TRACEBACK_CHARS = 4000


class Environment(BaseModel):
    os: str | None = None
    python: str | None = None
    node: str | None = None
    other: dict[str, str] = Field(default_factory=dict)


class IntakeExtraction(BaseModel):
    """模型负责输出的部分。"""

    reported_version: str | None = None
    environment: Environment = Field(default_factory=Environment)
    repro_steps: list[str] = Field(default_factory=list)
    expected: str | None = None
    actual: str | None = None
    missing: list[MissingField] = Field(default_factory=list)
    language: Literal["zh", "en", "other"] = "en"


class IntakeOutput(IntakeExtraction):
    traceback: str | None = None
    verifiability: float = Field(ge=0, le=1)


def extract_traceback(text: str) -> str | None:
    normalized = text.replace("\r\n", "\n")
    m = _PY_TRACEBACK.search(normalized) or _JS_STACK.search(normalized)
    if m is None:
        return None
    return m.group(0).strip()[:MAX_TRACEBACK_CHARS]


def verifiability(missing: Iterable[str]) -> float:
    score = 1.0 - sum(MISSING_WEIGHTS.get(m, 0.0) for m in set(missing))
    return round(max(score, 0.0), 2)


class IntakeSkill:
    name = "intake"
    version = "1"

    async def run(self, ctx: SkillContext) -> SkillResult:
        issue = ctx.issue
        traceback = extract_traceback(issue.body)
        user_msg = "\n\n".join([
            untrusted(f"issue#{issue.number}", "u1", f"{issue.title}\n\n{issue.body}"),
            "程序已提取到报错堆栈，不要输出 traceback 字段。" if traceback else
            "程序没有在正文里找到报错堆栈。",
            "按 system 中的格式输出 JSON。",
        ])
        messages = [
            {"role": "system", "content": load_prompt(f"intake_v{self.version}")},
            {"role": "user", "content": user_msg},
        ]
        extraction, usage, resp = await ctx.llm.complete_json(
            messages, IntakeExtraction, model=ctx.model
        )
        missing = list(dict.fromkeys(extraction.missing))
        if traceback and "error_output" in missing:
            missing.remove("error_output")
        output = IntakeOutput(
            **extraction.model_dump(exclude={"missing"}),
            missing=missing,
            traceback=traceback,
            verifiability=verifiability(missing),
        )
        return SkillResult(
            output=output,
            confidence=1.0,
            model=resp.model,
            usage=usage,
            cost_usd=priced(resp.model, usage),
            facts={"verifiability": output.verifiability},
        )
