import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from warden.db import Database, Repo
from warden.index.bm25 import BM25
from warden.index.embed import Embedder, cosine
from warden.index.store import IssueIndex, rrf
from warden.index.text import tokenize
from warden.index.trace import signature, similarity
from warden.platforms.github_rest import GitHubRest

TB_A = """Traceback (most recent call last):
  File "/home/alice/repro.py", line 2, in <module>
    df = pd.read_parquet("data/")
  File "/home/alice/.venv/lib/python3.12/site-packages/pandas/io/parquet.py", line 667, in read
    return impl.read(path)
KeyError: 'a'
"""
# 同一个 bug，另一台机器：路径、行号、列名都不同
TB_B = """Traceback (most recent call last):
  File "C:\\Users\\bob\\x.py", line 9, in <module>
    main()
  File "C:\\Python311\\Lib\\site-packages\\pandas\\io\\parquet.py", line 670, in read
    return impl.read(path)
KeyError: 'b'
"""


# ---------- 分词 ----------

def test_tokenize_cjk_bigrams_and_identifiers():
    toks = tokenize("读取分区 read_parquet 报错 of the")
    assert {"读取", "取分", "分区", "报错"} <= set(toks)
    assert {"read_parquet", "read", "parquet"} <= set(toks)
    assert "the" not in toks and "of" not in toks


# ---------- BM25 ----------

def test_bm25_prefers_rare_matching_terms_and_short_docs():
    docs = [
        tokenize("parquet keyerror when reading partitioned directory"),
        tokenize("csv writer crashes"),
        tokenize("parquet " + "filler " * 200),  # 同样命中 parquet，但很长
    ]
    s = BM25(docs).scores(tokenize("keyerror reading parquet"))
    assert s[0] > s[2] > s[1] == 0.0


def test_bm25_empty_query_and_corpus():
    assert BM25([]).scores(["x"]) == []
    assert BM25([["a"]]).scores(["zzz"]) == [0.0]


# ---------- 堆栈签名 ----------

def test_signature_normalizes_paths_and_drops_user_entrypoint():
    sa, sb = signature(TB_A), signature(TB_B)
    assert sa is not None and sb is not None
    assert sa.frames == ["pandas/io/parquet.py:read"] == sb.frames
    assert sa.exc_type == "KeyError"
    assert similarity(sa, sb) == 1.0


def test_signature_similarity_partial_and_none():
    other = signature(TB_A.replace("KeyError", "ValueError"))
    assert similarity(signature(TB_A), other) == 0.5  # 栈帧相同、异常类型不同
    assert similarity(signature(TB_A), None) == 0.0
    assert signature("no traceback here") is None


# ---------- RRF ----------

def test_rrf_rewards_agreement_between_channels():
    fused = rrf({"lexical": [0, 1, 2], "trace": [2, 0]}, k=60)
    # 文档 0：1/61 + 1/62；文档 2：1/63 + 1/61；文档 1 只出现在一路
    assert fused[0] > fused[2] > fused[1]


def test_cosine():
    assert cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine([1, 0], [0, 1]) == 0.0
    assert cosine([0, 0], [1, 1]) == 0.0


# ---------- IssueIndex ----------

@pytest.fixture
async def index_env(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'i.db').as_posix()}")
    await db.create_all()
    async with db.session() as s, s.begin():
        repo = Repo(platform="github", full_name="acme/w", mode="shadow")
        s.add(repo)
        await s.flush()
        repo_id = repo.id
    yield db, repo_id
    await db.dispose()


async def _seed(db, repo_id, index, docs):
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    async with db.session() as s, s.begin():
        for i, (number, title, body) in enumerate(docs):
            await index.upsert(
                s, repo_id, number=number, title=title, body=body,
                created_at=t0 + timedelta(days=i),
            )


async def test_search_fuses_lexical_and_trace(index_env):
    db, repo_id = index_env
    index = IssueIndex(db)
    await _seed(db, repo_id, index, [
        (1, "read_parquet 报 KeyError", TB_A),
        (2, "CSV 写入很慢", "to_csv takes 10 minutes"),
        (3, "Crash when loading data", TB_B),  # 标题完全不同，但堆栈相同
    ])
    results = await index.search(
        repo_id, title="读取 parquet 目录失败", body=TB_B, trace=signature(TB_B),
        exclude_number=99, before=None, k=5,
    )
    numbers = [r.number for r in results]
    assert numbers[:2] == [3, 1] or numbers[:2] == [1, 3]
    assert 2 not in numbers[:2]
    top = {r.number: r for r in results}
    assert "trace" in top[3].ranks and "trace" in top[1].ranks


async def test_search_excludes_self_and_future_issues(index_env):
    db, repo_id = index_env
    index = IssueIndex(db)
    await _seed(db, repo_id, index, [(1, "parquet bug", "x"), (2, "parquet bug", "y")])
    only_past = await index.search(
        repo_id, title="parquet bug", body="", trace=None, exclude_number=1,
        before=datetime(2026, 1, 1, 12, tzinfo=UTC), k=5,
    )
    assert only_past == []  # #1 是自己，#2 在"之后"才创建
    all_docs = await index.search(
        repo_id, title="parquet bug", body="", trace=None, exclude_number=1, before=None, k=5,
    )
    assert [r.number for r in all_docs] == [2]


async def test_semantic_channel_and_graceful_failure(index_env):
    db, repo_id = index_env

    def vec(text: str) -> list[float]:
        # 玩具 embedding：谈"加载/load"的文本指向同一个方向
        return [1.0, 0.0] if "加载" in text or "load" in text else [0.0, 1.0]

    def handler(request: httpx.Request) -> httpx.Response:
        inputs = json.loads(request.content)["input"]
        data = [{"index": i, "embedding": vec(t)} for i, t in enumerate(inputs)]
        return httpx.Response(200, json={"data": data})

    embedder = Embedder("http://e.test", "k", "m", transport=httpx.MockTransport(handler))
    index = IssueIndex(db, embedder)
    await _seed(db, repo_id, index, [(1, "数据加载失败", "无法打开"), (2, "文档错别字", "typo")])
    results = await index.search(
        repo_id, title="load fails", body="", trace=None, exclude_number=99, before=None, k=5,
    )
    assert results[0].number == 1 and "semantic" in results[0].ranks
    await embedder.aclose()

    broken = Embedder(
        "http://e.test", "k", "m", transport=httpx.MockTransport(lambda r: httpx.Response(500))
    )
    fallback = await IssueIndex(db, broken).search(
        repo_id, title="数据加载失败", body="", trace=None, exclude_number=99, before=None, k=5,
    )
    assert fallback and "semantic" not in fallback[0].ranks  # 降级为词法通道
    await broken.aclose()


# ---------- GitHub REST 回填 ----------

async def test_github_rest_paginates_and_skips_pull_requests():
    pages = {
        "1": (
            [{"number": 1}, {"number": 2, "pull_request": {}}],
            '<https://api.github.com/repos/a/b/issues?page=2>; rel="next"',
        ),
        "2": ([{"number": 3}], ""),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/a/b/issues"
        body, link = pages[request.url.params.get("page", "1")]
        return httpx.Response(200, json=body, headers={"link": link} if link else {})

    gh = GitHubRest(transport=httpx.MockTransport(handler))
    got = [i["number"] async for i in gh.iter_issues("a/b")]
    assert got == [1, 3]
    limited = [i["number"] async for i in gh.iter_issues("a/b", limit=1)]
    assert limited == [1]
    await gh.aclose()


# ---------- 模板行过滤 ----------

def test_boilerplate_lines_learned_from_corpus():
    from warden.index.text import boilerplate_lines, strip_boilerplate

    template = "**Describe the bug**\n{}\n**To Reproduce**\n<!-- fill this in -->\n{}"
    texts = [template.format(f"bug {i}", f"step {i}") for i in range(20)] + ["unrelated"]
    bp = boilerplate_lines(texts)
    assert "**describe the bug**" in bp and "**to reproduce**" in bp
    assert "bug 3" not in bp
    cleaned = strip_boilerplate(texts[3], bp)
    assert cleaned.split() == ["bug", "3", "step", "3"]


def test_boilerplate_needs_minimum_support():
    from warden.index.text import boilerplate_lines

    # 只有 2 个 issue 时，任何行都达不到"至少 3 次"的门槛，避免小语料误删真实内容
    assert boilerplate_lines(["same line", "same line"]) == frozenset()


async def test_lexical_index_is_cached_until_corpus_changes(index_env):
    db, repo_id = index_env
    index = IssueIndex(db)
    await _seed(db, repo_id, index, [(1, "parquet bug", "x"), (2, "csv bug", "y")])
    kw = dict(title="parquet", body="", trace=None, exclude_number=99, before=None, k=5)
    await index.search(repo_id, **kw)
    await index.search(repo_id, **kw)
    assert len(index._lexical_cache) == 1  # 语料没变，复用
    async with db.session() as s, s.begin():
        await index.upsert(s, repo_id, number=3, title="parquet again", body="z")
    results = await index.search(repo_id, **kw)
    assert len(index._lexical_cache) == 2 and 3 in [r.number for r in results]


async def test_template_line_filter_is_opt_in(index_env):
    db, repo_id = index_env
    body = "**Describe the bug**\n<!-- 请填写 -->\n{}"
    docs = [(i, f"issue {i}", body.format(f"detail {i}")) for i in range(1, 8)]
    default, opt_in = IssueIndex(db), IssueIndex(db, strip_template_lines=True)
    await _seed(db, repo_id, default, docs)
    kw = dict(title="x", body="", trace=None, exclude_number=99, before=None, k=5)
    await default.search(repo_id, **kw)
    await opt_in.search(repo_id, **kw)
    assert next(iter(default._lexical_cache.values())).boilerplate == frozenset()
    assert "**describe the bug**" in next(iter(opt_in._lexical_cache.values())).boilerplate
