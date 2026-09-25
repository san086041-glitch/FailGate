"""报告里没有堆栈时的复现判定：由独立的 LLM 评委对照 issue 描述和实际输出打分（技术方案 8.6 节）。

评委和写脚本的复现 Agent 是两次独立的调用：让 Agent 自己判断自己成没成功，等于让考生
给自己判卷。评委必须从实际输出里逐字引用证据，引文核对不上时按 0 分处理，
防止它凭空"看到"输出里没有的东西。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from warden.llm import LLMClient, Usage
from warden.skills.base import load_prompt, priced, untrusted

PROMPT_VERSION = "1"
_WS = re.compile(r"\s+")


class JudgeOutput(BaseModel):
    match: float = Field(ge=0, le=1)
    quote: str = ""
    reason: str = ""


class SemanticVerdict(BaseModel):
    match: float
    reason: str
    quote: str
    quote_found: bool
    cost_usd: float


def _norm(text: str) -> str:
    return _WS.sub(" ", text).strip()


def quote_in_output(quote: str, output: str) -> bool:
    q = _norm(quote)
    return bool(q) and q in _norm(output)


class SemanticJudge:
    def __init__(self, llm: LLMClient, model: str) -> None:
        self.llm = llm
        self.model = model
        self.usage = Usage()
        self.cost_usd = 0.0

    async def score(
        self, *, issue_title: str, issue_body: str, expected: str | None, actual: str | None,
        script: str, output: str,
    ) -> SemanticVerdict:
        user = "\n\n".join([
            untrusted("issue", "issue", f"{issue_title}\n\n{issue_body[:6000]}"),
            f"Intake 提取的预期行为：{expected or '（未提取到）'}\n"
            f"Intake 提取的实际行为：{actual or '（未提取到）'}",
            untrusted("repro-script", "script", script[:6000]),
            untrusted("sandbox-output", "output", output[-6000:]),
            "按 system 中的格式输出 JSON。",
        ])
        parsed, usage, resp = await self.llm.complete_json(
            [
                {"role": "system", "content": load_prompt(f"repro_judge_v{PROMPT_VERSION}")},
                {"role": "user", "content": user},
            ],
            JudgeOutput,
            model=self.model,
        )
        cost = priced(resp.model, usage)
        self.usage = self.usage + usage
        self.cost_usd += cost
        found = quote_in_output(parsed.quote, output)
        return SemanticVerdict(
            match=parsed.match if found else 0.0,
            reason=parsed.reason if found else f"引文在输出里找不到，按 0 分（{parsed.reason}）",
            quote=parsed.quote,
            quote_found=found,
            cost_usd=cost,
        )
