"""查重评委的思考模式（ADR 0026）：请求参数、推理 token、配置接线、对比报告。"""

from __future__ import annotations

import json

import httpx
import pytest
from test_dedup import judge, make_ctx, recalled

from failgate.llm import LLMClient, Usage, thinking_params
from failgate.replay.dataset import Labels
from failgate.replay.dedup import RunConfig, RunResult
from failgate.replay.dedup_compare import binom_two_sided, judge_stats, paired, render
from failgate.replay.metrics import JudgedCandidate, Record
from failgate.skills.dedup import DedupSkill


def test_thinking_params_map_to_deepseek_fields():
    assert thinking_params(None) == {} and thinking_params("") == {}
    assert thinking_params("disabled") == {"thinking": {"type": "disabled"}}
    assert thinking_params("low") == {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}
    with pytest.raises(ValueError):
        thinking_params("medium")


def test_reasoning_tokens_are_read_from_usage_details():
    u = Usage.from_api({"prompt_tokens": 400, "completion_tokens": 2500,
                        "completion_tokens_details": {"reasoning_tokens": 2400}})
    assert (u.completion_tokens, u.reasoning_tokens) == (2500, 2400)
    assert (u + u).reasoning_tokens == 4800
    assert Usage.from_api({"completion_tokens": 10}).reasoning_tokens == 0


async def test_chat_sends_thinking_only_when_asked():
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={
            "model": "deepseek-flash", "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    llm = LLMClient("http://llm.test", "k", transport=httpx.MockTransport(handler))
    await llm.chat([{"role": "user", "content": "hi"}], model="deepseek-flash")
    await llm.chat([{"role": "user", "content": "hi"}], model="deepseek-flash",
                   thinking="disabled")
    await llm.aclose()
    assert "thinking" not in bodies[0] and "reasoning_effort" not in bodies[0]
    assert bodies[1]["thinking"] == {"type": "disabled"}


async def test_dedup_judge_uses_the_configured_thinking_mode():
    ctx, fake, _ = make_ctx([recalled(3, "read_parquet KeyError", "KeyError: 'a'")])
    fake.queue("dedup", {"judgements": [judge("c1", 0.1)]})
    await DedupSkill(thinking="disabled").run(ctx)
    assert fake.requests[-1]["thinking"] == {"type": "disabled"}
    # 默认不传：用服务方的默认（DeepSeek 开思考、强度 high）
    ctx, fake, _ = make_ctx([recalled(3, "read_parquet KeyError", "KeyError: 'a'")])
    fake.queue("dedup", {"judgements": [judge("c1", 0.1)]})
    await DedupSkill().run(ctx)
    assert "thinking" not in fake.requests[-1]


async def test_setting_reaches_the_pipeline(tmp_path):
    from conftest import _harness, make_settings

    async for h in _harness(make_settings(tmp_path, dedup_thinking="low")):
        dedup = h.failgate.pipeline.skills[next(k for k in h.failgate.pipeline.skills
                                                if k.name == "DEDUPING")][0]
        assert dedup.thinking == "low"


# ---------------------------------------------------------------- 对比报告


def _rec(kind: str, issue: int, target: int | None, *, gold: tuple[int, ...] = (),
         out: int = 100, reasoning: int = 0, latency: float = 1.0) -> Record:
    cands = []
    if target is not None:
        cands = [JudgedCandidate(number=target, title="t", raw_score=0.99, has_quotes=True,
                                 quotes_found=True, same_root_cause=True, reason="r")]
    return Record(kind=kind, issue=issue, gold=list(gold), recall_rank=1, judged=True,
                  candidates=cands, out_tokens=out, reasoning_tokens=reasoning,
                  latency_s=latency, judge_cost_usd=out * 1e-6)


def _run(records: list[Record]) -> RunResult:
    from datetime import UTC, datetime

    from failgate.replay.dedup import RecallSummary

    return RunResult(config=RunConfig(repo="acme/w", high=0.95, low=0.5),
                     started_at=datetime.now(UTC), corpus_size=10, corpus_fingerprint="x",
                     gold_pairs=3, recall=RecallSummary(usable_pairs=3, hits={}, with_traceback=0),
                     records=records)


def test_paired_counts_lost_and_gained_samples():
    base = _run([_rec("pos", 1, 9, gold=(9,)), _rec("pos", 2, None, gold=(8,)),
                 _rec("neg", 3, None)])
    # 换模式后：#1 判错目标（判对 → 判错），#2 找到了（判错 → 判对），#3 不变
    other = _run([_rec("pos", 1, 5, gold=(9,)), _rec("pos", 2, 8, gold=(8,)),
                  _rec("neg", 3, None)])
    pr = paired(base, other, Labels())
    assert (pr.common, pr.same_decision, pr.lost, pr.gained) == (3, 1, 1, 1)
    assert pr.p_value == 1.0


def test_binomial_and_judge_stats():
    assert binom_two_sided(0, 10) == pytest.approx(2 / 1024)
    assert binom_two_sided(5, 10) == 1.0
    s = judge_stats([_rec("pos", 1, None, out=3000, reasoning=2900, latency=9.0),
                     _rec("pos", 2, None, out=200, reasoning=0, latency=1.0)])
    assert (s.n, s.out_tokens_mean, s.reasoning_mean) == (2, 1600, 1450)
    assert s.latency_p95 == 9.0


def test_render_has_quality_cost_and_paired_tables():
    base = _run([_rec("pos", 1, 9, gold=(9,), out=3000, reasoning=2900)])
    other = _run([_rec("pos", 1, 9, gold=(9,), out=150)])
    text = render([("high", base), ("disabled", other)], Labels())
    assert "## 质量" in text and "## 开销" in text and "以「high」为基准" in text
    assert "| disabled | 1 | 1 | 0 | 0 |" in text


def test_cascade_escalates_only_suspicious_samples():
    from failgate.replay.dedup_compare import cascade

    def low_score(rec: Record) -> Record:
        for c in rec.candidates:
            c.raw_score = 0.2
        return rec

    # 便宜的评委：#1 觉得可疑（0.99 → 升级），#2 觉得不像（0.2 → 直接用它的结论）
    cheap = _run([_rec("pos", 1, 5, gold=(9,), out=100),
                  low_score(_rec("pos", 2, 7, gold=(8,), out=100))])
    expensive = _run([_rec("pos", 1, 9, gold=(9,), out=3000),
                      _rec("pos", 2, 8, gold=(8,), out=3000)])
    text = cascade(cheap, expensive, Labels(), thresholds=(0.5,))
    assert "级联 t=0.5（升级 1）" in text
    # 升级的 #1 用贵评委的结论（判对），#2 用便宜的（判错）：召回 50%；花费 = 2×便宜 + 1×贵
    row = next(ln for ln in text.splitlines() if ln.startswith("| 级联"))
    assert "| 50.0% |" in row and "$0.00160" in row
