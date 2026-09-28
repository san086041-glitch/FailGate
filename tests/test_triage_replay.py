"""分诊回放评测：标准答案、分层抽样、指标、端到端（含缓存）与报告。"""

from datetime import UTC, datetime, timedelta

import pytest
from fake_llm import TRIAGE_OK, FakeLLM

from failgate.db import Database, IssueDoc, Repo
from failgate.index.store import IssueIndex
from failgate.llm import LLMClient
from failgate.replay.triage import (
    RepoLabel,
    TriageRecord,
    TriageRunConfig,
    evaluate_triage,
    gold_label,
    load_repo_labels,
    render_triage_report,
    run_triage_replay,
    sample,
    save_repo_labels,
)


def test_gold_label_requires_exactly_one_type_label():
    assert gold_label(["T: bug", "C: crash"]) == "T: bug"
    assert gold_label(["T: bug", "T: style"]) is None  # 两个类型标签，有歧义
    assert gold_label(["C: crash"]) is None
    assert gold_label(["T: unknown"]) is None  # 不认识的类型标签
    assert gold_label(["T: bug", "T: unknown"]) is None


def _doc(n: int, labels: list[str]) -> IssueDoc:
    return IssueDoc(number=n, title=f"t{n}", body="", labels=labels,
                    created_at=datetime(2024, 1, 1))


def test_sample_is_stratified_reproducible_and_split():
    docs = [_doc(i, ["T: bug"]) for i in range(100)] + [_doc(1000 + i, ["T: user support"])
                                                        for i in range(5)]
    picked = sample(docs, per_class=10, seed=7)
    by = {}
    for d, label, split in picked:
        by.setdefault(label, []).append((d.number, split))
    assert len(by["T: bug"]) == 10 and len(by["T: user support"]) == 5  # 不足的类全取
    splits = [s for _, s in by["T: bug"]]
    assert splits.count("dev") == 5 and splits.count("holdout") == 5
    # 与输入顺序无关，同一种子结果相同
    again = sample(list(reversed(docs)), per_class=10, seed=7)
    assert [(d.number, s) for d, _, s in again] == [(d.number, s) for d, _, s in picked]
    assert [d.number for d, _, _ in sample(docs, per_class=10, seed=8)] != [
        d.number for d, _, _ in picked
    ]


def _rec(n, label, gold_type, pred, *, conf=0.9, year=2022, split="dev", labels=None):
    return TriageRecord(issue=n, year=year, split=split, gold_label=label, gold_type=gold_type,
                        pred_type=pred, pred_labels=labels or [], confidence=conf)


def test_evaluate_metrics():
    records = [
        _rec(1, "T: bug", "bug", "bug", labels=["T: bug"]),
        _rec(2, "T: bug", "bug", "bug", conf=0.6, labels=["T: bug", "C: crash"]),
        _rec(3, "T: bug", "bug", "question", year=2025, split="holdout"),
        _rec(4, "T: enhancement", "feature", "feature", labels=["T: enhancement"]),
        _rec(5, "T: style", None, "bug", labels=["T: style"]),  # 不算类型准确率，只算标签
        _rec(6, "T: style", None, "feature", labels=["T: bug"]),
        TriageRecord(issue=7, year=2022, split="dev", gold_label="T: bug", gold_type="bug",
                     error="boom"),  # 出错的不计入分母
    ]
    m = evaluate_triage(records, {"T: bug": 90, "T: enhancement": 10})
    assert (m.type_acc.k, m.type_acc.n) == (3, 4) and m.errors == 1 and m.judged == 6
    assert m.per_class["T: bug"].value == pytest.approx(2 / 3)
    assert m.macro == pytest.approx((2 / 3 + 1) / 2)
    assert m.weighted == pytest.approx(2 / 3 * 0.9 + 1 * 0.1)
    # 标签：#1、#4、#5 恰好命中；#2 多了非类型标签也算命中（只比较 T: 标签）
    assert (m.label_exact.k, m.label_exact.n) == (4, 6)
    assert m.confusion["T: style"] == {"bug": 1, "feature": 1}
    assert m.calibration["[0.00, 0.70)"].n == 1 and m.by_period["≥2024"].n == 1
    holdout = evaluate_triage(records, {}, "holdout")
    assert holdout.type_acc.n == 1 and holdout.type_acc.k == 0


def test_repo_labels_roundtrip(tmp_path):
    labels = [RepoLabel(name="T: bug", description="Something isn't working"),
              RepoLabel(name="C: crash")]
    save_repo_labels("a/b", labels, tmp_path)
    assert load_repo_labels("a/b", tmp_path) == sorted(labels, key=lambda x: x.name)
    assert load_repo_labels("x/y", tmp_path) is None


@pytest.fixture
async def corpus(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'r.db').as_posix()}")
    await db.create_all()
    t0 = datetime(2023, 1, 1, tzinfo=UTC)
    index = IssueIndex(db)
    async with db.session() as s, s.begin():
        repo = Repo(platform="github", full_name="a/b", mode="shadow")
        s.add(repo)
        await s.flush()
        rows = [(i, ["T: bug"]) for i in range(1, 7)] + [(10, ["T: enhancement"]),
                                                          (11, ["T: style"]),
                                                          (12, ["T: bug", "T: style"]),
                                                          (13, [])]
        for i, (n, labels) in enumerate(rows):
            await index.upsert(s, repo.id, number=n, title=f"issue {n}", body="body",
                               labels=labels, created_at=t0 + timedelta(days=i))
    yield db
    await db.dispose()


async def test_run_triage_replay_end_to_end_with_cache(corpus, tmp_path):
    fake = FakeLLM()
    fake.defaults["triage"] = {**TRIAGE_OK, "labels": ["T: bug", "made-up"]}
    llm = LLMClient("http://llm.test", "k", transport=fake.transport)
    cfg = TriageRunConfig(repo="a/b", per_class=4, seed=1)
    labels = [RepoLabel(name="T: bug"), RepoLabel(name="T: enhancement"),
              RepoLabel(name="T: style")]
    cache = tmp_path / "cache"

    run = await run_triage_replay(corpus, llm, cfg, labels, cache_root=cache,
                                  progress=lambda _: None)
    # 4 个 bug + 1 个 enhancement + 1 个 style；#12（两个类型标签）和 #13（没有）不参与
    assert len(run.records) == 6 and {r.issue for r in run.records}.isdisjoint({12, 13})
    assert run.population == {"T: bug": 6, "T: enhancement": 1, "T: style": 1}
    assert run.model_calls == 12 and all(r.judged for r in run.records)
    # 标签只能从仓库标签表里选：made-up 被过滤
    assert all(r.pred_labels == ["T: bug"] for r in run.records)
    m = evaluate_triage(run.records, run.population)
    assert m.per_class["T: bug"].value == 1.0 and m.per_class["T: enhancement"].value == 0.0

    calls = len(fake.requests)
    again = await run_triage_replay(corpus, llm, cfg, labels, cache_root=cache,
                                    progress=lambda _: None)
    assert len(fake.requests) == calls and again.model_calls == 0 and again.cached_calls == 12
    # 换了标签表，分诊的缓存失效（Intake 的仍然命中）
    third = await run_triage_replay(corpus, llm, cfg, labels[:2], cache_root=cache,
                                    progress=lambda _: None)
    assert third.model_calls == 6 and third.cached_calls == 6
    await llm.aclose()

    report = render_triage_report(run)
    for section in ("## 结论（留出集）", "## 混淆矩阵", "## 置信度校准", "## 判错的样本"):
        assert section in report
    assert "[#10](https://github.com/a/b/issues/10)" in report  # enhancement 被判成 bug
