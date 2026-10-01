"""情景记忆（ADR 0032）：构建、时间旅行检索、接进修复 Agent。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from failgate.memory.build import attach_patches, episode_from, fetch_episodes, is_source
from failgate.memory.episodic import (
    Episode,
    EpisodicMemory,
    FileChange,
    IssueRef,
    hunk_functions,
    render,
    trim_patch,
)
from failgate.replay import fix_eval as fe

PATCH = """@@ -10,7 +10,7 @@ def delimiter_split(line, mode):
-    if trailing_comma:
+    if trailing_comma and not is_walrus(leaf):
@@ -40,3 +40,6 @@ class LineGenerator(Visitor[Line]):
+def is_walrus(leaf):
+    return leaf.type == token.COLONEQUAL
"""


def ep(pr: int, title: str, day: str, *, path: str = "src/black/linegen.py",
       issues: tuple[str, ...] = (), funcs: tuple[str, ...] = ()) -> Episode:
    return Episode(pr=pr, title=title, merged_at=datetime.fromisoformat(day).replace(tzinfo=UTC),
                   issues=[IssueRef(number=pr + 1000, title=t) for t in issues],
                   files=[path, "CHANGES.md"],
                   changes=[FileChange(path=path, functions=list(funcs), patch=PATCH)])


def test_hunk_functions_and_trim():
    assert hunk_functions(PATCH) == ["delimiter_split", "LineGenerator", "is_walrus"]
    long = "\n".join(f"+x{i}" for i in range(100))
    out = trim_patch(long, 10)
    assert out.count("\n") == 10 and out.endswith("（还有 90 行）")


def test_is_source():
    assert is_source("src/black/linegen.py") and is_source("src/blib2to3/Grammar.txt")
    for p in ["tests/test_black.py", "tests/data/cases/x.py", "CHANGES.md", "docs/a.md",
              ".github/workflows/x.yml", "pyproject.toml"]:
        assert not is_source(p), p


def _node(n: int, files: list[str], merged: str | None = "2024-01-02T00:00:00Z") -> dict[str, Any]:
    return {"number": n, "title": f"Fix {n}", "body": "x" * 3000, "mergedAt": merged,
            "mergeCommit": {"oid": "abc"},
            "closingIssuesReferences": {"nodes": [{"number": 7, "title": "crash", "body": "b"}]},
            "files": {"nodes": [{"path": p} for p in files]}}


def test_episode_from_keeps_source_prs_only():
    e = episode_from(_node(1, ["src/black/linegen.py", "tests/data/cases/a.py"]))
    assert e is not None and e.pr == 1 and len(e.body) == 1500
    assert e.issues[0].number == 7 and e.merged_at == datetime(2024, 1, 2, tzinfo=UTC)
    assert episode_from(_node(2, ["tests/test_black.py", "CHANGES.md"])) is None
    assert episode_from(_node(3, ["src/black/x.py"], merged=None)) is None
    full = attach_patches(e, [{"filename": "src/black/linegen.py", "patch": PATCH},
                              {"filename": "tests/data/cases/a.py", "patch": "+x"}])
    assert [c.path for c in full.changes] == ["src/black/linegen.py"]
    assert full.changes[0].functions[0] == "delimiter_split"


class FakeGH:
    def __init__(self) -> None:
        self.pages = [
            {"repository": {"pullRequests": {
                "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                "nodes": [_node(1, ["src/a.py"]), _node(2, ["docs/x.md"])]}}},
            {"repository": {"pullRequests": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [_node(3, ["src/b.py"])]}}},
        ]
        self.cursors: list[Any] = []

    async def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.cursors.append(variables["after"])
        return self.pages.pop(0)

    async def pull_files(self, full_name: str, number: int) -> list[dict[str, Any]]:
        return [{"filename": f"src/{'a' if number == 1 else 'b'}.py", "patch": PATCH}]


async def test_fetch_episodes_paginates_and_attaches_patches():
    gh = FakeGH()
    eps = await fetch_episodes(gh, "psf/black")
    assert gh.cursors == [None, "c1"]
    assert [e.pr for e in eps] == [1, 3] and eps[1].changes[0].path == "src/b.py"


def _memory() -> EpisodicMemory:
    return EpisodicMemory([
        ep(1, "Fix crash on walrus in return annotation", "2023-01-01",
           issues=("Black crashes on walrus",), funcs=("delimiter_split",)),
        ep(2, "Fix crash on walrus in subscript", "2024-06-01",
           issues=("walrus subscript crash",), funcs=("delimiter_split",)),
        ep(3, "Improve docstring handling", "2023-02-01", path="src/black/strings.py",
           funcs=("normalize_docstring",)),
    ])


def test_recall_is_time_travel_safe_and_filters():
    mem = _memory()
    before = datetime(2024, 1, 1, tzinfo=UTC)
    got = mem.recall("walrus crash", before=before)
    assert [e.pr for e in got] == [1]  # #2 在截止时间之后合并，看不到
    assert mem.visible(before) == 2
    later = datetime(2025, 1, 1, tzinfo=UTC)
    assert [e.pr for e in mem.recall("walrus crash", before=later)] == [2, 1]
    assert [e.pr for e in mem.recall("walrus crash", before=later, exclude_prs=[2])] == [1]
    assert mem.recall("walrus", before=later, path="src/black/strings.py") == []
    assert [e.pr for e in mem.recall("docstring", before=later,
                                     path="src/black/strings.py")] == [3]
    assert mem.recall("completely unrelated zebra", before=later) == []


def test_index_uses_only_past_documents():
    mem = _memory()
    idx, bm25 = mem._index(datetime(2023, 1, 15, tzinfo=UTC))
    assert idx == [0] and bm25.n == 1  # 词频统计也只用截止时间之前的文档


def test_dump_load_round_trip(tmp_path: Path):
    mem = _memory()
    path = tmp_path / "m.jsonl"
    EpisodicMemory.dump(mem.episodes, path)
    again = EpisodicMemory.load(path)
    assert [e.pr for e in again.episodes] == [1, 3, 2]  # 按合并时间排序


def test_render_with_and_without_patch():
    e = _memory().episodes[0]
    full = render([e])
    assert "PR #1（2023-01-01 合并）" in full and "关闭的 issue #1001" in full
    assert "（delimiter_split）" in full and "```diff" in full
    short = render([e], with_patch=False)
    assert "```diff" not in short
    assert render([]) == "（没有找到相关的历史修改）"


def test_arm_parsing_with_memory():
    assert fe.valid_arm("control+mem") and fe.valid_arm("exam:notes+mem")
    assert fe.arm_base("control+mem") == "control" and fe.arm_memory("control+mem")
    assert fe.arm_handoff("exam:notes+mem") == "notes" and not fe.arm_memory("exam:notes")
    assert "带情景记忆" in fe.arm_name("control+mem")


def test_summarize_memory_pairs():
    def row(n: int, arm: str, resolved: bool, calls: int = 0) -> dict[str, Any]:
        return {"type": "run", "number": n, "rep": 1, "arm": arm,
                "fix": {"status": "done", "passed": False, "cost_usd": 0.03, "steps": 40,
                        "duration_s": 100.0, "files": ["src/x.py"], "denied": 0,
                        "tool_counts": {"recall_fixes": calls}, "first_edit_step": 20},
                "gold": {"valid": True, "resolved": resolved, "reason": "", "f2p_passed": 1,
                         "f2p_total": 1, "broken_n": 0}, "hidden": None}

    rows = [row(1, "control", False), row(1, "control+mem", True, 3),
            row(2, "control", True), row(2, "control+mem", True, 1)]
    s = fe.summarize(rows)
    assert s["memory_pairs"]["control+mem"] == {"gained": 1, "lost": 0, "same": 1, "p": 1.0}
    assert s["handoff_pairs"] == {} and s["arms"]["control+mem"]["recall_calls"] == 4
    text = fe.render(rows, {"repo": "psf/black", "started": "t", "source": "s"})
    assert "## 情景记忆" in text and "1 / 0 / 1，p = 1.00" in text
    assert "| control+mem | 2 | 2/2（100%） | 40.0 | 20 | $0.03 | 2.0 |" in text


def test_json_line_format():
    line = _memory().episodes[0].model_dump_json()
    assert json.loads(line)["pr"] == 1


def test_fix_cutoffs_from_fixset_and_l2_run():
    fixset = {"cases": [{"number": 1, "fix": {"committed_at": "2026-04-14T10:00:00Z"}},
                        {"number": 2, "fix": {"committed_at": None}}]}
    assert fe.fix_cutoffs(fixset) == {1: datetime(2026, 4, 14, 10, tzinfo=UTC)}
    l2 = {"reports": [], "fbpa": [{"number": 3, "fix": {"committed_at": "2024-01-02T00:00:00"}},
                                  {"number": 4, "fix": None}]}
    assert fe.fix_cutoffs(l2) == {3: datetime(2024, 1, 2, tzinfo=UTC)}
