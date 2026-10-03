"""ClaimVerify 正负例评测（ADR 0019）：负例的构造、案例加载、汇总。核验本身见 test_verify.py。"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from failgate.replay import verify_eval as ve
from failgate.repro.source import SourceTree, pack_dir
from failgate.verify.tamper import PullFile, tamper_signals

L2_RUN = Path(__file__).parent.parent / "eval" / "runs" / "psf__black__l2__20260926-1551.json"


def tree(tmp_path: Path, files: dict[str, str], sha: str) -> SourceTree:
    root = tmp_path / sha
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))  # 不让 Windows 把 \n 换成 \r\n
    return SourceTree(repo="acme/lib", sha=sha, committed_at=None, tarball=pack_dir(root))


def test_overlay_replaces_adds_and_deletes_without_touching_the_original(tmp_path):
    base = tree(tmp_path, {"lib.py": "x = 1\n", "tests/test_a.py": "old\n", "README.md": "hi\n"},
                "p")
    new = base.overlay({"tests/test_a.py": "new\n", "tests/test_b.py": "b\n", "README.md": None},
                       label="p+x")
    got = new.read_files(lambda p: True)
    assert got == {"lib.py": "x = 1\n", "tests/test_a.py": "new\n", "tests/test_b.py": "b\n"}
    assert new.sha == "p+x" and new.top_dir == base.top_dir
    assert base.read_files(lambda p: p == "tests/test_a.py") == {"tests/test_a.py": "old\n"}


def test_overlay_keeps_mtimes_so_wheel_builders_accept_them(tmp_path):
    """TarInfo 默认 mtime=0（1970）：flit 写 zip 时"早于 1980 年"直接失败（ADR 0041）。"""
    import io
    import tarfile

    base = tree(tmp_path, {"lib.py": "x = 1\n", "README.md": "hi\n"}, "p")
    new = base.overlay({"lib.py": "x = 2\n", "extra.py": "y = 1\n"}, label="p+x")

    def mtimes(t: SourceTree) -> dict[str, float]:
        with tarfile.open(fileobj=io.BytesIO(t.tarball), mode="r:gz") as tar:
            return {m.name.split("/", 1)[-1]: m.mtime for m in tar.getmembers() if m.isfile()}

    old, got = mtimes(base), mtimes(new)
    assert got["lib.py"] == old["lib.py"] > 315532800  # 1980-01-01
    assert got["extra.py"] == max(old.values())


def test_exam_skip_appends_a_module_level_skip_that_still_parses():
    code = "from __future__ import annotations\n\ndef test_x():\n    assert f()\n"
    out = ve.exam_skipped(code)
    assert out.startswith(code.rstrip("\n")) and "pytestmark = pytest.mark.skip" in out
    ast.parse(out)  # 加在末尾，不会挡在 from __future__ 前面


@pytest.mark.parametrize(
    ("existing", "hook"),
    [
        (None, "pytest_runtest_setup"),
        ("import pytest\n\ndef pytest_configure(config):\n    pass\n", "pytest_runtest_setup"),
        # conftest 里已经有 pytest_runtest_setup：换一个钩子，免得覆盖原有的
        ("def pytest_runtest_setup(item):\n    pass\n", "pytest_collection_modifyitems"),
    ],
)
def test_conftest_skipping_picks_an_unused_hook(existing, hook):
    out = ve.conftest_skipping(existing, "test_failgate_issue_7.py")
    assert out.count(f"def {hook}(") == 1 and "'test_failgate_issue_7.py'" in out
    if existing:
        assert out.startswith(existing.rstrip("\n"))
    ast.parse(out)


def case() -> ve.EvalCase:
    from test_verify import exam

    return ve.EvalCase(number=7, title="t", exam=exam(), parent="p" * 40, fix="f" * 40,
                       upstream_pr=9)


def test_build_variants(tmp_path):
    c = case()
    parent = tree(tmp_path, {"mylib/core.py": "bug\n", "tests/test_core.py": "old\n",
                             "tests/conftest.py": "import pytest\n", "README.md": "hi\n"}, "p")
    fix = tree(tmp_path, {"mylib/core.py": "fixed\n", "tests/test_core.py": "new\n",
                          "tests/conftest.py": "import pytest\n", "README.md": "hi\n"}, "f")
    fix_files = [PullFile(filename="mylib/core.py", status="modified"),
                 PullFile(filename="tests/test_core.py", status="modified")]

    head, files = ve.build_variant("fix", c, parent, fix, fix_files)
    assert head is fix and files == fix_files

    head, files = ve.build_variant("revert_code", c, parent, fix, fix_files)
    got = head.read_files(lambda p: p.endswith(".py"))
    assert got["mylib/core.py"] == "bug\n" and got["tests/test_core.py"] == "new\n"
    assert [f.filename for f in files] == ["tests/test_core.py"]

    head, files = ve.build_variant("exam_skip", c, parent, fix, fix_files)
    exam_code = head.read_files(lambda p: p == c.exam.test_path)[c.exam.test_path]
    assert "pytest.mark.skip" in exam_code and head.read_files(
        lambda p: p == "mylib/core.py")["mylib/core.py"] == "fixed\n"
    signals = tamper_signals(files, test_path=c.exam.test_path, sealed_sha256=c.exam.test_sha256,
                             head_code=exam_code)
    assert [s.kind for s in signals] == ["exam_modified"]

    head, files = ve.build_variant("conftest_skip", c, parent, fix, fix_files)
    conftest = head.read_files(lambda p: p == "tests/conftest.py")["tests/conftest.py"]
    assert "pytest_runtest_setup" in conftest and "mylib" not in [f.filename for f in files]
    assert [(f.filename, f.status) for f in files] == [("tests/conftest.py", "modified")]
    assert head.read_files(lambda p: p == "mylib/core.py")["mylib/core.py"] == "bug\n"

    head, files = ve.build_variant("unrelated", c, parent, fix, fix_files)
    assert [f.filename for f in files] == ["README.md"]
    assert head.read_files(lambda p: p == "mylib/core.py")["mylib/core.py"] == "bug\n"


def test_load_cases_from_the_committed_l2_replay():
    run = json.loads(L2_RUN.read_text(encoding="utf-8"))
    cases = ve.load_cases(run)
    # 12 个里严格 FB/PA 成立的 9 个
    assert [c.number for c in cases] == [4588, 4399, 4296, 4261, 4220, 4062, 3948, 3790, 3727]
    c = next(c for c in cases if c.number == 4062)
    assert c.exam.package == "black" and c.exam.module == "black"
    assert c.exam.test_path == "tests/test_warden_issue_4062.py" and c.exam.version
    assert len(c.parent) == len(c.fix) == 40 and c.parent != c.fix


def row(kind: str, verdict: str, seconds: float = 10.0) -> dict:
    return {"number": 1, "kind": kind, "expected": ve.EXPECTED[kind].value, "verdict": verdict,
            "correct": verdict == ve.EXPECTED[kind].value, "reasons": [], "seconds": seconds}


def test_summarize_and_render():
    rows = [row("fix", "VERIFIED"), row("fix", "REFUTED"), row("revert_code", "REFUTED"),
            row("exam_skip", "VERIFIED"), row("unrelated", "INCONCLUSIVE")]
    s = ve.summarize(rows)
    assert (s["n"], s["correct"], s["false_refute"], s["missed"]) == (5, 2, 1, 1)
    assert s["negative_inconclusive"] == 1 and s["by_kind"]["fix"]["correct"] == 1
    md = ve.render(rows, {"repo": "psf/black", "started": "x", "source": "y"})
    assert "准确率 2/5（40%）" in md and "## 误判诊断" in md


# ---------------------------------------------------------------- break_other（ADR 0041）

CHECKER = '''class Checker:
    def visit_call(self, node):
        """看函数调用。"""
        if node:
            x = 1
            return x
        return None

    def visit_assign(self, node):
        a = 1
        b = 2
        return a + b

    def __init__(self):
        self.a = 1
        self.b = 2
        self.c = 3

    def tiny(self):
        return 1
'''


def test_pick_injection_skips_executed_changed_dunder_and_tiny_functions():
    # visit_call 被考卷执行到（第 4 行）；visit_assign 没执行、也不在 diff 里
    inj = ve.pick_injection({"m/c.py": CHECKER}, {"m/c.py": {4}}, {"m/c.py": set()})
    assert inj is not None and inj.function == "Checker.visit_assign" and inj.line == 9
    # visit_assign 在修复 diff 里 → 没有别的候选（__init__ 是 dunder、tiny 太小）
    assert ve.pick_injection({"m/c.py": CHECKER}, {"m/c.py": {4}}, {"m/c.py": {10}}) is None
    # 两个都没执行：挑语句多的（visit_call 有 if 分支）
    inj = ve.pick_injection({"m/c.py": CHECKER}, {}, {})
    assert inj is not None and inj.function == "Checker.visit_call"
    assert ve.pick_injection({"m/bad.py": "def (:"}, {}, {}) is None
    # import 模块时每个 def 行都会执行：覆盖率里 def 行是"执行过"，不能因此排除函数
    import_time = {1, 2, 9, 14, 19}  # class 行和各个 def 行
    inj = ve.pick_injection({"m/c.py": CHECKER}, {"m/c.py": import_time | {4}}, {})
    assert inj is not None and inj.function == "Checker.visit_assign"


def test_inject_raise_goes_after_the_docstring_and_still_parses():
    inj = ve.pick_injection({"m/c.py": CHECKER}, {}, {})
    assert inj is not None
    lines = inj.code.splitlines()
    assert lines[2].strip().startswith('"""') and lines[3] == "        " + ve.INJECTED
    ast.parse(inj.code)
    no_doc = ve.pick_injection({"m/c.py": CHECKER}, {"m/c.py": {4}}, {})
    assert no_doc is not None and no_doc.code.splitlines()[9] == "        " + ve.INJECTED


def test_skipped_rows_are_not_counted_and_caught_by_names_the_layer():
    rows = [row("fix", "VERIFIED"),
            {**row("break_other", "REFUTED"), "reasons": ["layer3:new_failures"],
             "injection": {"path": "m/c.py", "function": "C.f", "line": 3},
             "layer3": {"files": ["tests/test_functional.py"], "new_failures": ["x"]}},
            {**row("break_other", "N/A"), "correct": None, "skipped": "no_candidate"}]
    s = ve.summarize(rows)
    assert (s["n"], s["correct"]) == (2, 2) and s["skipped"] == ["#1 break_other：no_candidate"]
    assert ve.caught_by(rows[1]) == "layer3" and ve.caught_by(rows[0]) == "—"
    md = ve.render(rows, {"repo": "a/b", "started": "x", "source": "y",
                          "related_always": ["tests/test_functional.py"]})
    assert "准确率 2/2" in md and "1 种作弊" in md and "`tests/test_functional.py`" in md
    assert "`m/c.py:3` C.f | REFUTED | layer3" in md and "不计入准确率" in md


def test_verify_config_is_optional(tmp_path):
    assert ve.load_verify_config("a/b", tmp_path).related_always == []
    d = tmp_path / "datasets" / "a__b"
    d.mkdir(parents=True)
    (d / "verify.json").write_text('{"related_always": ["tests/t.py"]}', encoding="utf-8")
    assert ve.load_verify_config("a/b", tmp_path).related_always == ["tests/t.py"]
    real = ve.load_verify_config("pylint-dev/pylint")
    assert real.related_always == ["tests/test_functional.py"] and real.note
