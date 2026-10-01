"""修复评测集 v2（ADR 0031）：选题、排除理由、转成 replay fix 的案例。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from failgate.replay import fix_eval as fe
from failgate.replay import fixset as fxs
from failgate.replay.fixes import FixCommit
from failgate.verify.tamper import PullFile


def doc(n: int, *, labels: tuple[str, ...] = ("T: bug",), state: str = "closed",
        reason: str = "completed", created: str = "2024-01-01") -> Any:
    return SimpleNamespace(number=n, labels=list(labels), state=state, state_reason=reason,
                           created_at=datetime.fromisoformat(created), title=f"bug {n}")


def test_candidates_newest_first_with_filters():
    docs = [
        doc(1),
        doc(5),
        doc(3, labels=("T: bug", "R: duplicate")),
        doc(4, labels=("T: style",)),
        doc(6, reason="not_planned"),
        doc(7, state="open"),
        doc(8, created="2021-06-01"),
        doc(9),
        doc(10),
    ]
    got = fxs.candidates(docs, since=datetime(2022, 1, 1), exclude={9})
    assert [d.number for d in got] == [10, 5, 1]


def _f(name: str, status: str = "modified") -> PullFile:
    return PullFile(filename=name, status=status)


def test_skip_reason_and_source_changes():
    tests_only = [_f("tests/data/cases/x.py"), _f("CHANGES.md")]
    assert fxs.skip_reason(tests_only, "tests") == "no_source_change"
    src_only = [_f("src/black/linegen.py"), _f("CHANGES.md")]
    assert fxs.skip_reason(src_only, "tests") == "no_test_change"
    pyproject = PullFile(filename="pyproject.toml", status="modified",
                         patch='@@ -1 +1 @@\n+  "pytokens>=0.1.10",')
    deps = [_f("src/black/linegen.py"), _f("tests/test_black.py"), pyproject]
    reason = fxs.skip_reason(deps, "tests")
    assert reason is not None and reason.startswith("deps_changed:")
    good = [_f("src/black/linegen.py"), _f("src/blib2to3/Grammar.txt"),
            _f("tests/data/cases/x.py"), _f("CHANGES.md"), _f("docs/change_log.md")]
    assert fxs.skip_reason(good, "tests") is None
    assert fxs.source_changes(good, "tests") == ["src/black/linegen.py",
                                                 "src/blib2to3/Grammar.txt"]


def test_exclude_from_runs(tmp_path: Path):
    a = tmp_path / "a.json"
    a.write_text(json.dumps({"reports": [{"number": 1}, {"number": 2}]}), encoding="utf-8")
    b = tmp_path / "b.json"
    b.write_text(json.dumps({"reports": [{"number": 3}], "fbpa": []}), encoding="utf-8")
    assert fxs.exclude_from_runs([a, b]) == {1, 2, 3}


def _case(n: int, status: str = "ok") -> fxs.FixsetCase:
    return fxs.FixsetCase(
        number=n, title=f"bug {n}", created_at=datetime(2025, 1, n % 28 + 1, tzinfo=UTC),
        fix=FixCommit(pr=100 + n, sha=f"{n:040d}", parent=f"{n + 1:040d}"),
        src_files=["src/black/linegen.py"], package="black", module="black",
        python="3.12", version="25.1.1.dev0", pytest="pytest==8.3.4",
        test_path=f"tests/test_failgate_issue_{n}.py",
        gold=fe.Gold(status=status, targets=["tests/test_format.py"], f2p=["t::a"]),
        test_overlay={"tests/data/cases/x.py": "x = 1\n"})


def test_eval_cases_gold_rows_and_round_trip(tmp_path: Path):
    fs = fxs.Fixset(repo="psf/black", since="2022-01-01", target=2,
                    cases=[_case(5), _case(4, status="no_f2p")],
                    skipped=[fxs.Skipped(number=3, reason="no_fix_commit")])
    assert fs.seen() == {3, 4, 5}
    cases = fxs.eval_cases(fs)
    assert [c.number for c in cases] == [5]
    c = cases[0]
    assert c.exam.code == "" and c.exam.python == "3.12" and c.exam.pytest == "pytest==8.3.4"
    assert c.parent == f"{6:040d}" and c.fix == f"{5:040d}" and c.upstream_pr == 105
    rows = fxs.gold_rows(fs)
    assert rows[0]["type"] == "gold" and rows[0]["gold"]["f2p"] == ["t::a"]
    assert rows[0]["test_overlay"] == {"tests/data/cases/x.py": "x = 1\n"}
    path = tmp_path / "fs.json"
    fxs.save(fs, path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert fxs.is_fixset(data) and not fxs.is_fixset({"reports": []})
    assert fxs.load(path) == fs
    text = fxs.render(fs)
    assert "收下 1 题，跳过 1 个" in text and "| #5 | 2025-01-06 | #105 | 3.12 | 1 |" in text
    assert "| no_fix_commit | 1 |" in text
