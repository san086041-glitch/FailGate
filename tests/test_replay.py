import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fake_llm import FakeLLM

from warden.db import Database, Repo
from warden.index.store import IssueIndex
from warden.llm import LLMClient
from warden.platforms.github_rest import GitHubRest
from warden.replay.dataset import (
    Clusters,
    GoldPair,
    GoldSet,
    Labels,
    PairLabel,
    load_gold,
    save_gold,
)
from warden.replay.dedup import RunConfig, run_dedup_replay
from warden.replay.metrics import JudgedCandidate, Record, evaluate, recommend, sweep, wilson
from warden.replay.mine import duplicate_pattern, find_original, mine_gold
from warden.replay.report import render_report

# ---------- 数据集 ----------


def test_clusters_are_transitive():
    c = Clusters()
    c.union(3, 2)
    c.union(2, 1)
    c.union(10, 11)
    assert c.same(3, 1) and not c.same(3, 10)
    assert c.members(1) == {1, 2, 3}
    assert 99 not in c and c.members(99) == set()


def test_gold_roundtrip(tmp_path):
    gold = GoldSet(repo="a/b", mined_at=datetime(2026, 9, 24, tzinfo=UTC),
                   pairs=[GoldPair(duplicate=5, original=2, association="MEMBER")])
    path = save_gold(gold, root=tmp_path)
    assert path.name == "dedup_gold.json" and "a__b" in str(path)
    assert load_gold("a/b", root=tmp_path) == gold


# ---------- 挖掘标准答案 ----------

def test_find_original_only_trusts_maintainers():
    pat = duplicate_pattern("psf/black")
    comments = [
        {"author_association": "NONE", "body": "Duplicate of #1"},  # 普通用户的猜测不算
        {"author_association": "MEMBER", "body": "Duplicate of #7"},  # 自己引用自己
        {"author_association": "COLLABORATOR",
         "body": "duplicate of https://github.com/psf/black/issues/3", "html_url": "u"},
    ]
    pair = find_original(comments, 7, pat)
    assert pair is not None and pair.original == 3 and pair.association == "COLLABORATOR"
    assert find_original(comments[:2], 7, pat) is None


async def test_mine_gold_uses_search_then_comments():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search/issues":
            assert "in:comments" in request.url.params["q"]
            return httpx.Response(200, json={"items": [{"number": 5}, {"number": 9}]})
        if request.url.path.endswith("/5/comments"):
            return httpx.Response(200, json=[
                {"author_association": "OWNER", "body": "Duplicate of #2"}])
        return httpx.Response(200, json=[{"author_association": "NONE", "body": "dup of #1"}])

    gh = GitHubRest(transport=httpx.MockTransport(handler))
    gold = await mine_gold(gh, "a/b")
    await gh.aclose()
    assert gold.search_hits == 2
    assert [(p.duplicate, p.original) for p in gold.pairs] == [(5, 2)]


# ---------- 指标 ----------

def cand(n: int, raw: float, *, quotes: bool = True, same: bool | None = True) -> JudgedCandidate:
    return JudgedCandidate(number=n, raw_score=raw, has_quotes=quotes, quotes_found=quotes,
                           same_root_cause=same)


def test_wilson_interval():
    lo, hi = wilson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-3)
    lo, hi = wilson(9, 10)
    assert lo == pytest.approx(0.596, abs=1e-3) and hi == pytest.approx(0.982, abs=1e-3)


def synthetic_records() -> list[Record]:
    return [
        Record(kind="pos", issue=10, gold=[1], recall_rank=1, judged=True,
               candidates=[cand(1, 0.9)]),                       # 判对
        Record(kind="pos", issue=11, gold=[2], recall_rank=2, judged=True,
               candidates=[cand(7, 0.95), cand(2, 0.6)]),        # 指错目标，但正确的出现在相关里
        Record(kind="pos", issue=12, gold=[3], recall_rank=None, judged=True,
               candidates=[cand(8, 0.3)]),                       # 召回没找到 → 漏判
        Record(kind="pos", issue=13, gold=[4], recall_rank=1, judged=True,
               candidates=[cand(4, 0.8)]),                       # 0.8：阈值 0.85 下漏判
        Record(kind="neg", issue=20, judged=True, candidates=[cand(5, 0.9)]),   # 复核：是重复
        Record(kind="neg", issue=21, judged=True, candidates=[cand(6, 0.9)]),   # 未复核
        Record(kind="neg", issue=22, judged=True, candidates=[cand(9, 0.2)]),
        Record(kind="neg", issue=23, judged=False, error="boom"),               # 出错的不计
    ]


def test_evaluate_counts_and_labels():
    labels = Labels(pairs={"20->5": PairLabel(label="duplicate")})
    m = evaluate(synthetic_records(), high=0.85, low=0.5, labels=labels)
    assert (m.n_pos, m.n_neg) == (4, 3)
    assert (m.tp, m.wrong_target, m.missed) == (1, 1, 2)
    assert (m.neg_flagged, m.neg_flagged_confirmed_dup, m.neg_flagged_unreviewed) == (2, 1, 1)
    # 判对 = 1 (tp) + 1 (复核确认)；判为重复 = 1 + 1 (指错) + 2 (对照)
    assert m.precision == pytest.approx(2 / 4)
    assert m.recall == pytest.approx(1 / 4)
    assert m.recall_given_recalled == pytest.approx(1 / 3)
    assert m.surfaced == 3  # 10、11（相关里出现）、13（0.8 为相关）


def test_sweep_and_recommend_prefer_recall_under_precision_target():
    labels = Labels(pairs={"20->5": PairLabel(label="duplicate"),
                           "21->6": PairLabel(label="duplicate")})
    recs = [r for r in synthetic_records() if r.issue != 11]  # 去掉指错的样本
    results = sweep(recs, labels=labels, highs=(0.75, 0.85), gates=(True,))
    by_high = {m.high: m for m in results}
    assert by_high[0.75].tp == 2 and by_high[0.85].tp == 1
    best = recommend(results, min_precision=0.9)
    assert best is not None and best.high == 0.75
    assert recommend(results, min_precision=1.01) is None


def test_gate_changes_outcome():
    recs = [Record(kind="pos", issue=1, gold=[2], recall_rank=1, judged=True,
                   candidates=[cand(2, 0.95, same=False)])]
    assert evaluate(recs, high=0.85, low=0.5, gate=True).tp == 0
    assert evaluate(recs, high=0.85, low=0.5, gate=False).tp == 1


# ---------- 端到端回放 ----------

@pytest.fixture
async def corpus(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'r.db').as_posix()}")
    await db.create_all()
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    index = IssueIndex(db)
    async with db.session() as s, s.begin():
        repo = Repo(platform="github", full_name="a/b", mode="shadow")
        s.add(repo)
        await s.flush()
        docs = [(i, f"filler issue number {i}", f"unrelated body {i}", None) for i in range(1, 61)]
        docs += [
            (100, "parquet KeyError when reading partitioned dir", "KeyError: 'a'", None),
            (101, "read partitioned parquet raises KeyError", "same KeyError: 'a'", None),
            (102, "csv slow", "to_csv slow", "duplicate"),  # 已标成 duplicate，不能进对照组
        ]
        for i, (n, title, body, reason) in enumerate(docs):
            await index.upsert(s, repo.id, number=n, title=title, body=body,
                               state_reason=reason, created_at=t0 + timedelta(days=i))
    yield db
    await db.dispose()


async def test_run_dedup_replay_end_to_end_with_cache(corpus, tmp_path):
    gold = GoldSet(repo="a/b", mined_at=datetime.now(UTC),
                   pairs=[GoldPair(duplicate=101, original=100)])
    fake = FakeLLM()
    fake.defaults["dedup"] = {"judgements": [
        {"id": "c1", "score": 0.92, "same_root_cause": True, "reason": "same",
         "quote_new": "KeyError: 'a'", "quote_candidate": "KeyError: 'a'"}]}
    llm = LLMClient("http://llm.test", "k", transport=fake.transport)
    cfg = RunConfig(repo="a/b", positives=5, negatives=3, seed=1, min_history=10)
    cache = tmp_path / "cache"

    run = await run_dedup_replay(corpus, llm, gold, cfg, cache_root=cache, progress=lambda _: None)
    assert run.recall.usable_pairs == 1 and run.recall.hits[1] == 1
    pos = [r for r in run.records if r.kind == "pos"]
    neg = [r for r in run.records if r.kind == "neg"]
    assert len(pos) == 1 and pos[0].gold == [100] and pos[0].judged
    assert len(neg) == 3 and all(r.issue not in (100, 101, 102) for r in neg)
    assert run.model_calls == 8 and run.cached_calls == 0  # 4 个样本 × (intake + dedup)
    m = evaluate(run.records, high=0.85, low=0.5)
    assert m.tp == 1

    calls_before = len(fake.requests)
    again = await run_dedup_replay(corpus, llm, gold, cfg, cache_root=cache,
                                   progress=lambda _: None)
    assert len(fake.requests) == calls_before  # 全部命中缓存，不再调用模型
    assert again.model_calls == 0 and again.cached_calls == 8 and again.cost_usd == 0
    await llm.aclose()

    report = render_report(run, m, sweep(run.records), recommend(sweep(run.records)),
                           Labels(), 0.9)
    assert "## 召回" in report and "## 阈值扫描" in report and "待人工复核" in report
    json.loads(run.model_dump_json())  # 可以序列化成评测记录文件


async def test_recall_only_mode_makes_no_model_calls(corpus, tmp_path):
    gold = GoldSet(repo="a/b", mined_at=datetime.now(UTC),
                   pairs=[GoldPair(duplicate=101, original=100)])
    cfg = RunConfig(repo="a/b", judge=False, min_history=10)
    run = await run_dedup_replay(corpus, None, gold, cfg, cache_root=tmp_path,
                                 progress=lambda _: None)
    assert run.recall.hits[8] == 1 and run.model_calls == 0
    assert all(not r.judged for r in run.records)


def test_reviewed_alternative_target_counts_as_correct():
    rec = Record(kind="pos", issue=11, gold=[2], recall_rank=1, judged=True,
                 candidates=[cand(7, 0.95), cand(2, 0.6)])
    plain = evaluate([rec], high=0.85, low=0.5)
    counts = (plain.tp, plain.tp_alt, plain.wrong_target, plain.wrong_target_unreviewed)
    assert counts == (0, 0, 1, 1)
    labels = Labels(pairs={"11->7": PairLabel(label="duplicate", note="同一根因")})
    reviewed = evaluate([rec], high=0.85, low=0.5, labels=labels)
    assert (reviewed.tp_alt, reviewed.wrong_target) == (1, 0)
    assert reviewed.precision == 1.0 and reviewed.recall == 1.0
    rejected = evaluate([rec], high=0.85, low=0.5,
                        labels=Labels(pairs={"11->7": PairLabel(label="not_duplicate")}))
    assert (rejected.wrong_target, rejected.wrong_target_unreviewed) == (1, 0)
