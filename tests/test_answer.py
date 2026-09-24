"""Answer：文档切块、防编造的三道检查、维护者回答的筛选、端到端（提问 → ANSWERED → 汇总评论）。"""

import io
import tarfile
from datetime import UTC, datetime
from typing import Any

from conftest import PUBLIC_COMMENTS, Harness, issue_event
from fake_llm import INTAKE_OK, TRIAGE_OK
from harness_utils import only_case
from sqlalchemy import select

from warden.db import Repo
from warden.index.docs import Chunk, DocIndex, chunk_document, extract_docs, is_doc_path
from warden.platforms.base import Comment, User
from warden.skills.answer import (
    RawAnswer,
    RawCitation,
    Source,
    finalize_answer,
    maintainer_answers,
    strip_links,
)

SOURCES = [
    Source(id="S1", kind="doc", title="docs/usage.md › Line length",
           url="https://github.com/o/r/blob/abc/docs/usage.md#line-length",
           text="Use the --line-length option (or line-length in pyproject.toml) to change it."),
    Source(id="S2", kind="issue", title="#12 How to skip files",
           url="https://github.com/o/r/issues/12",
           text="#12 How to skip files\n问：...\n\n维护者回答：Use --extend-exclude with a regex."),
]


def _raw(answer: str, *cites: tuple[str, str], confidence: float = 0.9, **kw: Any) -> RawAnswer:
    return RawAnswer(
        answer=answer,
        citations=[RawCitation(id=i, quote=q) for i, q in cites],
        confidence=confidence,
        **kw,
    )


# ---------- finalize_answer：防编造的三道检查 ----------

def test_verified_citation_becomes_numbered_link():
    out = finalize_answer(
        _raw("Pass --line-length [S1].", ("S1", "Use the --line-length option")), SOURCES
    )
    assert out.status == "answered"
    assert out.answer_md == f"Pass --line-length [[1]]({SOURCES[0].url})."
    assert [(r.n, r.id) for r in out.references] == [(1, "S1")]


def test_numbering_follows_first_use_and_merges_multi_markers():
    out = finalize_answer(
        _raw("A [S2, S1]. B [S1].",
             ("S1", "line-length in pyproject.toml"), ("S2", "Use --extend-exclude")),
        SOURCES,
    )
    assert out.status == "answered"
    assert out.answer_md.startswith(f"A [[1]]({SOURCES[1].url})[[2]]({SOURCES[0].url}).")
    assert [r.id for r in out.references] == ["S2", "S1"]


def test_fabricated_source_id_is_dropped():
    out = finalize_answer(_raw("Use --magic [S7].", ("S7", "anything")), SOURCES)
    assert out.status == "rejected" and out.reject_reason == "no_verified_citation"
    assert out.citations[0].known_source is False and out.dropped_markers == ["S7"]


def test_quote_not_in_source_is_rejected():
    out = finalize_answer(
        _raw("Use --magic [S1].", ("S1", "the --magic flag does it")), SOURCES
    )
    assert out.status == "rejected" and out.citations[0].quote_found is False


def test_unverified_marker_removed_but_verified_kept():
    out = finalize_answer(
        _raw("Real [S1]. Invented [S2].", ("S1", "Use the --line-length option"),
             ("S2", "not in the text")),
        SOURCES,
    )
    assert out.status == "answered"
    assert "Invented ." in out.answer_md and out.dropped_markers == ["S2"]


def test_low_confidence_is_rejected():
    out = finalize_answer(
        _raw("x [S1]", ("S1", "Use the --line-length option"), confidence=0.6), SOURCES
    )
    assert out.status == "rejected" and out.reject_reason == "low_confidence"


def test_abstain_and_no_sources():
    assert finalize_answer(RawAnswer(abstain=True), SOURCES).status == "abstained"
    assert finalize_answer(_raw("x [S1]", ("S1", "y")), []).status == "abstained"


def test_model_written_links_and_mentions_are_neutralized():
    text, n = strip_links("See [the docs](https://evil.example/x) or http://evil.example/y ok")
    assert n == 2 and "evil" not in text and "the docs" in text
    out = finalize_answer(
        _raw("Ask @alice or see https://phish.example [S1].",
             ("S1", "Use the --line-length option")),
        SOURCES,
    )
    assert "phish" not in out.answer_md and out.removed_links == 1
    # 代码里的网址不是链接，保留（实测：删掉后只剩一个孤零零的反引号）
    code = "Use `rev: https://github.com/psf/black-pre-commit-mirror` and\n```\nurl = http://x\n```"
    assert strip_links(code) == (code, 0)
    assert "@\u200balice" in out.answer_md


# ---------- 维护者回答的筛选 ----------

def _comment(body: str, assoc: str, *, bot: bool = False, day: int = 1) -> Comment:
    return Comment(
        id="1", body=body, created_at=datetime(2026, 1, day, tzinfo=UTC),
        author=User(login="x", is_bot=bot, association=assoc),
    )


def test_only_maintainer_comments_before_cutoff_count():
    comments = [
        _comment("random user tip", "NONE"),
        _comment("contributor tip", "CONTRIBUTOR"),
        _comment("bot says", "NONE", bot=True),
        _comment("old warden summary <!-- repowarden:summary -->", "OWNER"),
        _comment("maintainer answer", "MEMBER", day=2),
        _comment("answer from the future", "OWNER", day=20),
    ]
    cutoff = datetime(2026, 1, 10)  # 不带时区（SQLite 读出来的就是这样）
    assert maintainer_answers(comments, cutoff) == ["maintainer answer"]


# ---------- 文档切块 ----------

def test_markdown_chunking_respects_code_fences_and_anchors():
    md = (
        "# Black\nThe uncompromising formatter, long enough to keep.\n"
        "## Configuration: pyproject.toml\nSet line-length under the tool.black table here.\n"
        "```toml\n# not a heading\n[tool.black]\nline-length = 100\n```\n"
    )
    chunks = chunk_document("README.md", md)
    assert [c.heading for c in chunks] == ["Black", "Black › Configuration: pyproject.toml"]
    assert chunks[1].anchor == "configuration-pyprojecttoml"
    assert "# not a heading" in chunks[1].text


def test_rst_headings_and_long_sections_split_on_blank_lines():
    para = "Some sentence about options that is reasonably long. " * 12
    options = "Options\n-------\nThe --check flag reports problems without writing.\n"
    rst = "Usage\n=====\n" + "\n\n".join([para] * 5) + "\n\n" + options
    chunks = chunk_document("docs/usage.rst", rst)
    usage = [c for c in chunks if c.heading == "Usage"]
    assert len(usage) >= 2 and all(len(c.text) <= 1500 + len(para) for c in usage)
    assert chunks[-1].heading == "Usage › Options" and chunks[-1].anchor == "options"


def test_doc_path_filter():
    yes = ["README.md", "README", "CHANGES.md", "docs/a/b.rst", "doc/x.md", "CONTRIBUTING.md"]
    no = ["src/readme.md", "setup.py", "docs/logo.png", "README.png"]
    assert all(is_doc_path(p) for p in yes) and not any(is_doc_path(p) for p in no)


def test_extract_docs_from_tarball():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in [("o-r-abc/README.md", b"# Hi"), ("o-r-abc/src/x.py", b"x=1"),
                           ("o-r-abc/docs/guide.md", "# 指南".encode())]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    assert extract_docs(buf.getvalue()) == [("README.md", "# Hi"), ("docs/guide.md", "# 指南")]


# ---------- 端到端 ----------

async def _seed_docs(h: Harness) -> None:
    async with h.warden.db.session() as s, s.begin():
        repo = (await s.scalars(select(Repo))).one()
        await DocIndex(h.warden.db).replace(
            s, repo.id, repo.full_name, "abc123",
            [Chunk("docs/usage.md", "Usage › Line length", "line-length",
                   "Use the --line-length option to change the maximum line length.")],
        )


QUESTION = {**TRIAGE_OK, "type": "question", "labels": ["question"], "rationale": "How-to."}


def _asking(number: int, title: str, body: str) -> dict[str, Any]:
    payload = issue_event("opened", number)
    payload["issue"].update(title=title, body=body)
    return payload


async def test_question_is_answered_with_references(harness: Harness):
    # 先有一个历史提问，登记仓库；再建文档索引
    old = _asking(1, "Can I change the max line length?", "Is the line length configurable?")
    await harness.send("issues", old, "d-1")
    await harness.warden.worker.drain()
    await _seed_docs(harness)
    # 历史 issue #1 下有维护者的回答（读评论走只读 REST 后备，测试里是假的）
    PUBLIC_COMMENTS[1] = [
        {"id": 5, "body": "Set line-length in pyproject.toml.", "author_association": "OWNER",
         "user": {"login": "maint", "type": "User"}, "created_at": "2020-01-01T00:00:00Z"},
    ]

    harness.llm.queue("intake", {**INTAKE_OK, "language": "en", "missing": []})
    harness.llm.queue("triage", QUESTION)
    harness.llm.queue("answer", {
        "abstain": False,
        "answer": "Use `--line-length` [S1], or set it in pyproject.toml [S2].",
        "citations": [{"id": "S1", "quote": "Use the --line-length option"},
                      {"id": "S2", "quote": "Set line-length in pyproject.toml"}],
        "confidence": 0.9,
    })
    new = _asking(2, "How to set line length?", "I want lines up to 100 chars. Which option?")
    await harness.send("issues", new, "d-2")
    await harness.warden.worker.drain()

    case = await only_case(harness, 2)
    assert case["state"] == "ANSWERED"
    answer = next(r for r in case["runs"] if r["skill"] == "answer")["output"]
    assert answer["status"] == "answered", (answer["citations"], answer["sources"])
    assert [r["kind"] for r in answer["references"]] == ["doc", "issue"]
    summaries = [e for e in case["effects"] if e["action"] == "upsert_summary"]
    # 只在流水线停下时生成一次汇总，而不是查重后一次、回答后再一次
    assert len(summaries) == 1
    body = summaries[0]["payload"]["body"]
    assert "**Answer**" in body and "Sources:" in body
    assert "https://github.com/acme/widgets/blob/abc123/docs/usage.md#line-length" in body
    assert "please add" not in body  # 提问不追问复现信息


async def test_question_without_reliable_source_waits_for_maintainer(harness: Harness):
    harness.llm.queue("intake", {**INTAKE_OK, "language": "zh"})
    harness.llm.queue("triage", QUESTION)
    await harness.send("issues", issue_event("opened"), "d-1")
    await harness.warden.worker.drain()
    case = await only_case(harness)
    assert case["state"] == "ANSWERED"
    answer = next(r for r in case["runs"] if r["skill"] == "answer")
    # 没有任何资料：不调用模型，零花费
    assert answer["output"]["status"] == "abstained" and answer["usd"] == 0
    assert not any("Answer（答疑）模块" in r["messages"][0]["content"]
                   for r in harness.llm.requests)
    body = next(e for e in case["effects"] if e["action"] == "upsert_summary")["payload"]["body"]
    assert "请等待维护者回复" in body


async def test_bug_summary_still_posted_once(harness: Harness):
    await harness.send("issues", issue_event("opened"), "d-1")
    await harness.warden.worker.drain()
    case = await only_case(harness)
    assert case["state"] == "TRIAGE_ONLY"
    assert [e["action"] for e in case["effects"]].count("upsert_summary") == 1
    assert "回答" not in next(
        e for e in case["effects"] if e["action"] == "upsert_summary"
    )["payload"]["body"]
