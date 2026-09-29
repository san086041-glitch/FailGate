"""考卷强度的外部校准（ADR 0022）：命令拼装、选择器、结果判定、统计、编排。

编排用进程内的假容器：变异体真的被执行（exec 源码再跑考卷函数），只是不起 Docker、
不装 swebench。真实运行在 GitHub Actions 上（.github/workflows/strength-calibration.yml）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from failgate.replay.swebench_strength import (
    Condition,
    Instance,
    SweBench,
    build_test_command,
    coverage_command,
    exam_outcome,
    exam_selectors,
    load_instances,
    patch_files,
    permutation_p,
    render,
    run_instance,
    sign_test,
    source_files,
    summarize,
)
from failgate.replay.swebench_strength import _coverage_lines as coverage_lines

GOLD = """diff --git a/pkg/core.py b/pkg/core.py
--- a/pkg/core.py
+++ b/pkg/core.py
@@ -1,2 +1,4 @@
 def clamp(x, hi):
-    return x
+    if x > hi:
+        return hi
+    return x
diff --git a/tests/test_core.py b/tests/test_core.py
"""
BASE_SRC = "def clamp(x, hi):\n    return x\n"
HEAD_SRC = "def clamp(x, hi):\n    if x > hi:\n        return hi\n    return x\n"


def test_patch_and_source_files():
    assert patch_files(GOLD) == ["pkg/core.py", "tests/test_core.py"]
    assert source_files(GOLD) == ["pkg/core.py"]
    other = "diff --git a/a/test_x.py b/a/test_x.py\ndiff --git a/x.txt b/x.txt\n"
    assert source_files(other) == []


@pytest.mark.parametrize(
    ("repo", "f2p", "expected"),
    [
        ("django/django", ["test_a (forms.tests.FormTests)"], ["forms.tests.FormTests.test_a"]),
        ("django/django", ["test_a (forms.tests.FormTests.test_a)"],
         ["forms.tests.FormTests.test_a"]),  # Django 4.x 的格式
        ("django/django", ["Field instances are not equal."], ["model_fields.tests"]),  # 退回模块
        ("sympy/sympy", ["test_foo"], ["sympy/core/tests/test_x.py"]),
        ("pydata/xarray", ["xarray/tests/test_a.py::test_b[1]"],
         ["xarray/tests/test_a.py::test_b[1]"]),
    ],
)
def test_exam_selectors(repo, f2p, expected):
    directives = {"django/django": ["model_fields.tests"],
                  "sympy/sympy": ["sympy/core/tests/test_x.py"]}.get(repo, [])
    assert exam_selectors(repo, f2p, directives) == expected


def test_commands():
    assert build_test_command("pytest -rA", ["a.py::t[x y]"]) == "pytest -rA 'a.py::t[x y]'"
    assert build_test_command(["pip install x", "pytest -rA"], ["a"]) == "pytest -rA a"
    inc = ["/testbed/pkg/core.py"]
    assert coverage_command("pytest -rA a.py::t", inc) == (
        "python -m coverage run --include=/testbed/pkg/core.py -m pytest -rA a.py::t")
    # 脚本所在目录要进 PYTHONPATH
    # （试跑实测：runtests.py 找不到 test_sqlite、bin/test 找不到 get_sympy）
    assert coverage_command("./tests/runtests.py --parallel 1 forms", inc) == (
        "PYTHONPATH=./tests${PYTHONPATH:+:$PYTHONPATH} python -m coverage run "
        "--include=/testbed/pkg/core.py ./tests/runtests.py --parallel 1 forms")
    sympy = "PYTHONWARNINGS='ignore::UserWarning' bin/test -C --verbose a.py"
    assert coverage_command(sympy, inc) == (
        "PYTHONWARNINGS=ignore::UserWarning PYTHONPATH=bin${PYTHONPATH:+:$PYTHONPATH} "
        "python -m coverage run --include=/testbed/pkg/core.py bin/test --no-subprocess "
        "-C --verbose a.py")
    assert coverage_command("tox --current-env -epy39 -v -- t.py", inc) is None


def test_exam_outcome_requires_every_exam_test_to_pass():
    assert exam_outcome({"a": "PASSED", "b": "XFAIL"}, ["a", "b"]) == "pass"
    assert exam_outcome({"a": "PASSED", "b": "FAILED"}, ["a", "b"]) == "fail"
    assert exam_outcome({"a": "PASSED"}, ["a", "b"]) == "fail"  # 没跑到也算失败


def test_coverage_json_parsing_tolerates_noise():
    text = 'Wrote JSON report\n{"files": {"/testbed/pkg/core.py": {"executed_lines": [1, 2]}}}\n'
    assert coverage_lines(text) == {"/testbed/pkg/core.py": {1, 2}}
    assert coverage_lines("no json") == {}


def test_statistics():
    assert sign_test(5, 0) == pytest.approx(0.0625)
    assert sign_test(0, 0) == 1.0
    assert sign_test(3, 3) == 1.0
    assert permutation_p([0.9, 0.95, 1.0], [0.1, 0.2, 0.15], rounds=2000) < 0.2
    assert permutation_p([0.5, 0.5], [0.5, 0.5], rounds=200) == 1.0


def test_load_frozen_dataset():
    insts = load_instances(Path("eval/datasets/swebench_utboost/instances.json"))
    groups = [i.group for i in insts]
    assert groups.count("utboost") == 26 and groups.count("control") == 26
    ut = next(i for i in insts if i.instance_id == "django__django-11133")
    assert [c.name for c in ut.conditions] == ["official", "utboost"]
    assert len(ut.conditions[1].fail_to_pass) > len(ut.conditions[0].fail_to_pass)
    assert all(len(i.conditions) == 1 for i in insts if i.group == "control")


# ---------------------------------------------------------------- 编排：进程内的假容器


# 官方考卷只测一个"没超过上限"的输入：if 分支改坏了也察觉不到；增强测试补上了"超过上限"
OFFICIAL = "def test_under():\n    assert clamp(3, 5) == 3\n"
UTBOOST = OFFICIAL + "def test_over():\n    assert clamp(9, 5) == 5\n"


class FakeContainer:
    """/testbed 是一个字典；"跑测试"就是 exec 当前源码和当前测试文件，
    输出 pytest -rA 风格的日志。"""

    def __init__(self) -> None:
        self.files = {"pkg/core.py": BASE_SRC}
        self.tests = ""
        self.stopped = False
        self.offline_called = False

    def start(self) -> None: ...

    def offline(self) -> None:
        self.offline_called = True

    def write(self, path: str, content: str) -> None:
        if path.startswith("/tmp/"):
            self.pending = content
        else:
            self.files[path.removeprefix("/testbed/")] = content

    def read(self, path: str) -> str:
        return self.files[path.removeprefix("/testbed/")]

    def git_show(self, path: str) -> str | None:
        return BASE_SRC if path == "pkg/core.py" else None

    def stop(self) -> None:
        self.stopped = True

    def sh(self, script: str, *, timeout_s: int = 600) -> tuple[int, str, float]:
        if script.startswith("git apply"):
            if "gold" in script:
                self.files["pkg/core.py"] = HEAD_SRC
            else:
                self.tests = self.pending
            return 0, "", 0.0
        if "coverage json" in script:
            return 0, json.dumps({"files": {"/testbed/pkg/core.py":
                                            {"executed_lines": [1, 2, 3, 4]}}}), 0.0
        if script.startswith("python -c"):
            return 0, "3 11", 0.0
        if "pytest" in script:
            return 0, self._run(), 0.5
        return 0, "", 0.0

    def _run(self) -> str:
        ns: dict[str, Any] = {}
        exec(self.files["pkg/core.py"], ns)  # noqa: S102 — 测试里执行自己写的代码
        exec(self.tests, ns)  # noqa: S102
        out = []
        for name in [k for k in ns if k.startswith("test_")]:
            try:
                ns[name]()
                out.append(f"PASSED t.py::{name}")
            except Exception:  # noqa: BLE001
                out.append(f"FAILED t.py::{name}")
        return "\n".join(out)


def _parse(_: dict[str, Any], log: str) -> dict[str, str]:
    return {ln.split()[1]: ln.split()[0] for ln in log.splitlines() if ln.startswith(("PASSED",
                                                                                     "FAILED"))}


SWEB = SweBench(specs=lambda r, v: {"test_cmd": "pytest -rA"},
                directives=lambda inst: ["t.py"], parse=_parse)


def instance(group: str = "utboost") -> Instance:
    conds = [Condition("official", OFFICIAL, ["t.py::test_under"])]
    if group == "utboost":
        conds.append(Condition("utboost", UTBOOST, ["t.py::test_under", "t.py::test_over"]))
    return Instance(instance_id="acme__pkg-1", repo="acme/pkg", version="1.0",
                    base_commit="b" * 40, patch=GOLD, group=group,  # type: ignore[arg-type]
                    conditions=conds, raw={"repo": "acme/pkg", "version": "1.0"})


def test_same_mutants_are_run_against_both_exams_and_files_are_restored():
    c = FakeContainer()
    row = run_instance(instance(), SWEB, container=c)  # type: ignore[arg-type]
    assert row["status"] == "ok", row
    assert c.offline_called and c.stopped
    assert row["changed_lines"] == 2 and row["executed_lines"] == 2
    off = row["conditions"]["official"]["kill_rate"]
    ut = row["conditions"]["utboost"]["kill_rate"]
    assert ut > off  # 增强测试把官方考卷放过的变异体抓住了
    assert all(set(m["outcomes"]) == {"official", "utboost"} for m in row["mutants"])
    assert c.files["pkg/core.py"] == HEAD_SRC  # 每个变异体跑完都换回标准修复

    s = summarize([row, {**row, "instance_id": "acme__pkg-2"}])
    assert (s["up"], s["down"], s["tie"]) == (2, 0, 0)
    assert s["rescued"] > 0 and s["lost"] == 0
    report = render([row], {"started": "2026-09-29T20:00:00"})
    assert "成对比较" in report and "acme__pkg-1" in report


def test_control_instance_runs_only_the_official_exam():
    row = run_instance(instance("control"), SWEB, container=FakeContainer())  # type: ignore[arg-type]
    assert row["status"] == "ok" and set(row["conditions"]) == {"official"}


def test_baseline_that_does_not_pass_is_reported_not_scored():
    broken = SweBench(specs=SWEB.specs, directives=SWEB.directives,
                      parse=lambda inst, log: {})
    row = run_instance(instance(), broken, container=FakeContainer())  # type: ignore[arg-type]
    assert row["status"] == "baseline_failed"
    assert "mutants" not in row


class PickyContainer(FakeContainer):
    """第一种打补丁的方式总是失败（像 UTBoost 那个 corrupt patch），第二种才成功。"""

    def sh(self, script: str, *, timeout_s: int = 600) -> tuple[int, str, float]:
        if script.startswith("git apply -v /tmp/"):
            return 1, "error: corrupt patch at line 74", 0.0
        if script.startswith("git apply -v --recount"):
            return super().sh("git apply " + script.split()[-1], timeout_s=timeout_s)
        return super().sh(script, timeout_s=timeout_s)


def test_patches_fall_back_like_the_official_harness():
    row = run_instance(instance(), SWEB, container=PickyContainer())  # type: ignore[arg-type]
    assert row["status"] == "ok"
    assert row["gold_apply"] == "git apply -v --recount"
    assert row["conditions"]["utboost"]["test_patch_apply"] == "git apply -v --recount"


class CoverageBreaksContainer(FakeContainer):
    """带 coverage 跑时考卷出错（模块找不到），不带 coverage 就正常。"""

    def sh(self, script: str, *, timeout_s: int = 600) -> tuple[int, str, float]:
        if "coverage run" in script:
            return 1, "ModuleNotFoundError: No module named 'test_sqlite'", 0.1
        return super().sh(script, timeout_s=timeout_s)


def test_baseline_retries_without_coverage_and_targets_all_changed_lines():
    row = run_instance(instance(), SWEB, container=CoverageBreaksContainer())  # type: ignore[arg-type]
    assert row["status"] == "ok"
    info = row["conditions"]["official"]
    assert info["coverage"] is False and "test_sqlite" in info["coverage_failed_tail"]
    assert row["executed_lines"] == row["changed_lines"]



class SubprocessContainer(FakeContainer):
    """考卷在子进程里跑（sympy 的 bin/test），coverage 什么都没记到。"""

    def sh(self, script: str, *, timeout_s: int = 600) -> tuple[int, str, float]:
        if "coverage json" in script:
            return 0, json.dumps({"files": {}}), 0.0
        return super().sh(script, timeout_s=timeout_s)


def test_empty_coverage_falls_back_to_all_changed_lines():
    row = run_instance(instance(), SWEB, container=SubprocessContainer())  # type: ignore[arg-type]
    assert row["status"] == "ok"
    assert row["conditions"]["official"]["coverage"] == "no_data"
    assert row["executed_lines"] == row["changed_lines"] and row["mutants"]


def test_django_docstring_tests_are_matched_by_method_name():
    from failgate.replay.swebench_strength import django_docstring_status

    log = ("test_a (m.T.test_a) ... ok\n"
           "test_b (m.T.test_b)\n"
           "Docstring of b. ... ok\n"
           "test_c (m.T)\n"
           "Docstring of c. ... FAIL\n")
    assert django_docstring_status(log) == {"test_b (m.T.test_b)": "PASSED",
                                            "test_c (m.T)": "FAILED"}


def test_sympy_coverage_runs_without_subprocess():
    cmd = coverage_command("bin/test -C --verbose a.py", ["/testbed/x.py"])
    assert cmd is not None and "bin/test --no-subprocess -C --verbose a.py" in cmd


class OnlyImportLinesContainer(FakeContainer):
    """coverage 记到了行，但只是 import 时的行（函数体在子进程里执行），和改动行对不上。"""

    def sh(self, script: str, *, timeout_s: int = 600) -> tuple[int, str, float]:
        if "coverage json" in script:
            return 0, json.dumps({"files": {"/testbed/pkg/core.py": {"executed_lines": [1]}}}), 0
        return super().sh(script, timeout_s=timeout_s)


def test_no_overlap_with_changed_lines_counts_as_no_coverage():
    row = run_instance(instance(), SWEB, container=OnlyImportLinesContainer())  # type: ignore[arg-type]
    assert row["conditions"]["official"]["coverage"] == "no_data"
    assert row["executed_lines"] == row["changed_lines"]
