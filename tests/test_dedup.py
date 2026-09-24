from typing import Any

import pytest
from fake_llm import FakeLLM
from harness_utils import only_case

from warden.index.store import Recalled
from warden.llm import LLMClient
from warden.skills.base import IssueSnapshot, SkillContext
from warden.skills.dedup import DedupSkill, quote_found

NEW_BODY = "升级到 2.4.1 后 read_parquet 读取分区目录报 KeyError: 'a'"


class FakeRetriever:
    def __init__(self, results: list[Recalled]) -> None:
        self.results = results
        self.calls: list[dict[str, Any]] = []

    async def search(self, repo_id: int, **kwargs: Any) -> list[Recalled]:
        self.calls.append({"repo_id": repo_id, **kwargs})
        return self.results


def recalled(number: int, title: str, body: str) -> Recalled:
    return Recalled(
        number=number, title=title, body=body, state="open", state_reason=None,
        labels=[], url=None, rrf=0.03, ranks={"lexical": 1},
    )


def make_ctx(results: list[Recalled]) -> tuple[SkillContext, FakeLLM, FakeRetriever]:
    fake, retriever = FakeLLM(), FakeRetriever(results)
    ctx = SkillContext(
        issue=IssueSnapshot(repo="acme/w", number=10, title="读取分区 parquet 报错",
                            body=NEW_BODY, repo_id=1),
        llm=LLMClient("http://llm.test", "k", transport=fake.transport),
        model="deepseek-flash",
        prior={"intake": {"language": "zh", "traceback": None}, "triage": {"type": "bug"}},
        retriever=retriever,
    )
    return ctx, fake, retriever


def judge(cid: str, score: float, qn: str = "", qc: str = "") -> dict[str, Any]:
    return {"id": cid, "score": score, "reason": "r", "quote_new": qn, "quote_candidate": qc}


def test_quote_found_ignores_whitespace_case_and_wrapping_quotes():
    assert quote_found('"KeyError:   \'A\'"', "报 keyerror: 'a' 了")
    assert not quote_found("ValueError", "KeyError")
    assert quote_found("", "anything")


async def test_no_candidates_skips_llm():
    ctx, fake, _ = make_ctx([])
    result = await DedupSkill().run(ctx)
    assert result.output.verdict == "none" and result.cost_usd == 0.0
    assert fake.requests == [] and result.facts["dup_high"] is False


async def test_duplicate_with_verified_quotes():
    ctx, fake, retriever = make_ctx([
        recalled(3, "read_parquet KeyError on partitioned dir", "KeyError: 'a' since 2.4.1"),
        recalled(7, "CSV slow", "to_csv slow"),
    ])
    fake.queue("dedup", {"judgements": [
        judge("c1", 0.97, qn="KeyError: 'a'", qc="KeyError: 'a' since 2.4.1"),
        judge("c2", 0.05),
    ]})
    result = await DedupSkill().run(ctx)
    out = result.output
    assert out.verdict == "duplicate" and out.best == 3 and out.best_score == 0.97
    assert out.candidates[0].quotes_verified and out.candidates[0].level == "duplicate"
    assert out.candidates[1].level == "none"
    assert result.facts == {"type": "bug", "repro_enabled": False, "dup_high": True}
    # 召回只看创建时间之前的 issue，并排除自己
    assert retriever.calls[0]["exclude_number"] == 10
    # 候选内容作为 untrusted 数据发给模型
    user = fake.requests[0]["messages"][1]["content"]
    assert '<untrusted source="issue#3" id="c1">' in user


async def test_fabricated_quote_is_penalized_below_duplicate():
    ctx, fake, _ = make_ctx([recalled(3, "parquet bug", "reading fails")])
    fake.queue("dedup", {"judgements": [
        judge("c1", 0.95, qn="KeyError: 'a'", qc="this sentence is not in the candidate"),
    ]})
    out = (await DedupSkill().run(ctx)).output
    c = out.candidates[0]
    assert not c.quotes_verified and c.raw_score == 0.95
    assert c.score == pytest.approx(0.665) and out.verdict == "related"


async def test_high_score_without_quotes_is_penalized():
    ctx, fake, _ = make_ctx([recalled(3, "parquet bug", "reading fails")])
    fake.queue("dedup", {"judgements": [judge("c1", 0.9)]})
    c = (await DedupSkill().run(ctx)).output.candidates[0]
    assert not c.quotes_verified and c.score == pytest.approx(0.63)


async def test_unknown_candidate_ids_are_ignored():
    ctx, fake, _ = make_ctx([recalled(3, "parquet bug", "reading fails")])
    fake.queue("dedup", {"judgements": [judge("c9", 0.99, "x", "y")]})
    out = (await DedupSkill().run(ctx)).output
    assert out.candidates == [] and out.verdict == "none" and out.best is None


async def test_end_to_end_second_issue_finds_first(harness):
    from conftest import issue_event

    first = issue_event("opened", 1)
    first["issue"]["title"] = "read_parquet 读取分区目录报 KeyError"
    first["issue"]["body"] = "升级到 2.4.1 后读取分区目录报 KeyError: 'a'"
    await harness.send("issues", first, "d-1")
    await harness.warden.worker.drain()

    second = issue_event("opened", 2, author="carol")
    second["issue"]["title"] = "分区 parquet 读取报 KeyError"
    second["issue"]["body"] = "2.4.1 读取分区的 parquet 目录时抛出 KeyError"
    harness.llm.queue("dedup", {"judgements": [
        judge("c1", 0.97, qn="读取分区的 parquet 目录时抛出 KeyError",
              qc="读取分区目录报 KeyError: 'a'"),
    ]})
    await harness.send("issues", second, "d-2")
    await harness.warden.worker.drain()

    case = await only_case(harness, number=2)
    dedup = case["runs"][2]["output"]
    assert dedup["verdict"] == "duplicate" and dedup["best"] == 1
    assert case["state"] == "DUP_SUSPECTED"
    summary = next(e for e in case["effects"] if e["action"] == "upsert_summary")
    assert "可能与 #1 重复" in summary["payload"]["body"]


async def test_same_root_cause_false_caps_at_related():
    ctx, fake, _ = make_ctx([recalled(3, "fmt skip bug", "fmt: skip ignored on decorators")])
    j = judge("c1", 0.95, qn="读取分区", qc="fmt: skip ignored")
    j["same_root_cause"] = False
    fake.queue("dedup", {"judgements": [j]})
    out = (await DedupSkill().run(ctx)).output
    assert out.candidates[0].score == 0.95 and out.candidates[0].level == "related"
    assert out.verdict == "related"


async def test_duplicate_level_outranks_higher_scored_related():
    ctx, fake, _ = make_ctx([
        recalled(3, "a", "KeyError: 'a' here"),
        recalled(4, "b", "KeyError: 'a' there"),
    ])
    capped = judge("c1", 0.99, qn="KeyError: 'a'", qc="KeyError: 'a' here")
    capped["same_root_cause"] = False
    real = judge("c2", 0.96, qn="KeyError: 'a'", qc="KeyError: 'a' there")
    real["same_root_cause"] = True
    fake.queue("dedup", {"judgements": [capped, real]})
    out = (await DedupSkill().run(ctx)).output
    assert out.verdict == "duplicate" and out.best == 4


async def test_prompt_version_selects_template():
    ctx, fake, _ = make_ctx([recalled(3, "x", "y")])
    await DedupSkill(prompt_version="1").run(ctx)
    await DedupSkill(prompt_version="2").run(ctx)
    systems = [r["messages"][0]["content"] for r in fake.requests]
    assert "same_root_cause" not in systems[0] and "same_root_cause" in systems[1]
