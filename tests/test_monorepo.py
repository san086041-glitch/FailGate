"""monorepo 适配（ADR 0045）：PackageConfig.subdir 贯穿安装、测试路径、导入根、选题和核验。

subdir 为空时所有行为逐字节不变；最后两个是真实 Docker 测试（fixtures/repos/mono-keyerror：
包在 libs/confkit，根目录不可安装，根目录的 conftest.py 一旦被加载就报错）。
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from packaging.specifiers import SpecifierSet
from test_repro_l2 import FakeTestSandbox, OfflinePyPI, res
from test_repro_source import RecordingSandbox, make_tar
from test_verify import BASE, HEAD, FakeBench, exam, pf, pull
from test_verify import res as vres

from failgate.fix.workspace import workspace_pythonpath
from failgate.replay import fixtures as fx_mod
from failgate.replay import verify_eval as ve
from failgate.replay.selection import Selection
from failgate.repro.config import PackageConfig
from failgate.repro.envcache import EnvCache
from failgate.repro.l2 import RUN_PREFIXES, SourcePrepared, TestReproducer, repo_test_file
from failgate.repro.sandbox import DockerSandbox, SandboxError, check_command
from failgate.repro.source import (
    INSTALL_PREFIXES,
    INSTALLER,
    INSTALLER_SUBDIR,
    SRC_DIR,
    SourceTree,
    pack_dir,
    pick_python_for_commit,
    source_env,
)
from failgate.verify.engine import ClaimVerdict, ClaimVerifier
from failgate.verify.receipt import build_receipt, code_sha256, receipt_digest
from failgate.verify.related import module_of, select_related_tests
from failgate.verify.store import exam_from_receipt
from failgate.verify.strength import executed_in_copy, import_root, installed_path
from failgate.verify.tamper import PullFile

FIXTURE = Path(__file__).parent.parent / "fixtures" / "repos" / "mono-keyerror"
SUB = "libs/confkit"
MONO_EXAM_PATH = f"{SUB}/tests/unit_tests/test_failgate_issue_101.py"
PARSER = f"{SUB}/confkit/parser.py"


def mono_tree(*, sha: str = "m" * 40, extra: dict[str, bytes] | None = None) -> SourceTree:
    members = {
        "lc-abc/pyproject.toml": b"[tool.ruff]\n",
        f"lc-abc/{SUB}/pyproject.toml":
            b"[project]\nname = 'confkit'\nrequires-python = '>=3.10,<3.13'\n",
        f"lc-abc/{SUB}/confkit/__init__.py": b"",
        f"lc-abc/{SUB}/tests/__init__.py": b"",
        f"lc-abc/{SUB}/tests/unit_tests/test_parser.py": b"def test_x():\n    pass\n",
        f"lc-abc/{SUB}/tests/integration_tests/test_live.py": b"",
        "lc-abc/libs/other/tests/test_other.py": b"",
        "lc-abc/tests/test_root.py": b"",
        **(extra or {}),
    }
    return SourceTree(repo="acme/mono", sha=sha, committed_at=datetime(2025, 6, 1, tzinfo=UTC),
                      tarball=make_tar(members))


# ---------------------------------------------------------------- 配置


@pytest.mark.parametrize(("raw", "want"), [
    ("libs/core", "libs/core"), ("libs/core/", "libs/core"), (" libs/core ", "libs/core"),
    ("", None), (None, None), ("packages/my_pkg-2.x", "packages/my_pkg-2.x"),
])
def test_subdir_is_normalized(raw: str | None, want: str | None):
    assert PackageConfig(name="x", subdir=raw).subdir == want


@pytest.mark.parametrize("bad", [
    "/libs/core", "../core", "libs/../core", "libs/./core", "libs//core", "libs\\core",
    "libs/core;rm -rf", "libs core", "$(id)", "~/x",
])
def test_subdir_rejects_paths_outside_the_repo(bad: str):
    with pytest.raises(ValueError):
        PackageConfig(name="x", subdir=bad)


def test_test_deps_only_take_package_names():
    assert PackageConfig(name="x", test_deps=["pytest-asyncio", "syrupy"]).test_deps == [
        "pytest-asyncio", "syrupy"]
    for bad in (["pytest>=8"], ["git+https://x/y"], ["--index-url=http://evil"], ["./local"]):
        with pytest.raises(ValueError):
            PackageConfig(name="x", test_deps=bad)


def test_default_config_has_no_subdir_or_deps():
    cfg = PackageConfig(name="black")
    assert cfg.subdir is None and cfg.test_deps == []


# ---------------------------------------------------------------- 源码包


def test_pyproject_and_python_come_from_the_subdir():
    tree = mono_tree()
    assert tree.pyproject() == {"tool": {"ruff": {}}}
    assert tree.requires_python() is None
    assert tree.requires_python(SUB) == SpecifierSet(">=3.10,<3.13")
    # 提交时间 2025-06：3.13 已发布，但子目录的 requires-python 封顶 3.12
    assert pick_python_for_commit(tree, subdir=SUB) == "3.12"
    assert pick_python_for_commit(tree) == "3.13"


def test_test_dir_in_subdir_prefers_unit_tests():
    tree = mono_tree()
    assert tree.test_dir() == "tests"  # 不给 subdir：和以前一样看仓库根
    assert tree.test_dir(SUB) == f"{SUB}/tests/unit_tests"
    assert repo_test_file(tree, 101, SUB) == MONO_EXAM_PATH
    assert repo_test_file(tree, 101) == "tests/test_failgate_issue_101.py"
    flat = SourceTree(repo="a/b", sha="s", committed_at=None, tarball=make_tar({
        "t/libs/pkg/pyproject.toml": b"", "t/libs/pkg/test/test_a.py": b""}))
    assert flat.test_dir("libs/pkg") == "libs/pkg/test"
    empty = SourceTree(repo="a/b", sha="s", committed_at=None, tarball=make_tar({
        "t/libs/pkg/pyproject.toml": b""}))
    assert empty.test_dir("libs/pkg") == "libs/pkg/tests"


# ---------------------------------------------------------------- 安装脚本


def run_subdir_installer(tmp_path: Path, tarball: bytes, subdir: str
                         ) -> subprocess.CompletedProcess[str]:
    work = tmp_path / "work"
    work.mkdir()
    (work / "src.tar.gz").write_bytes(tarball)
    return subprocess.run(
        [sys.executable, "-c", INSTALLER_SUBDIR, str(work / "src.tar.gz"),
         str(work / "out"), subdir],
        capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},  # Windows 上默认按 GBK 输出
    )


def test_subdir_installer_refuses_a_subdir_without_a_project(tmp_path: Path):
    proc = run_subdir_installer(tmp_path, make_tar({"top/libs/core/x.py": b""}), "libs/core")
    assert proc.returncode != 0 and "没有 pyproject.toml" in proc.stderr
    assert (tmp_path / "work" / "out" / "libs" / "core" / "x.py").exists()  # 整包照样解开


@pytest.mark.parametrize("bad", ["../etc", "/etc", "libs/../../x", "a b"])
def test_subdir_installer_rejects_bad_subdir_before_extracting(tmp_path: Path, bad: str):
    proc = run_subdir_installer(tmp_path, make_tar({"top/a.py": b""}), bad)
    assert proc.returncode != 0 and "subdir 不合法" in proc.stderr
    assert not (tmp_path / "work" / "out").exists()


def test_install_whitelist_has_both_installers_and_nothing_else():
    check_command(["python", "-c", INSTALLER, "/workspace/src.tar.gz", SRC_DIR],
                  INSTALL_PREFIXES)
    check_command(["python", "-c", INSTALLER_SUBDIR, "/workspace/src.tar.gz", SRC_DIR, SUB],
                  INSTALL_PREFIXES)
    with pytest.raises(SandboxError):
        check_command(["python", "-c", INSTALLER_SUBDIR + "\nimport os"], INSTALL_PREFIXES)


async def test_source_env_with_subdir_uses_its_own_installer_and_key(tmp_path: Path):
    sb = RecordingSandbox()
    cache = EnvCache(sb, tmp_path / "envcache.json")  # type: ignore[arg-type]
    tree = mono_tree()
    plain = await source_env(cache, tree, python="3.12", version="1.0.dev0",
                             extra_requirements=["pytest==8.0.0"])
    mono = await source_env(cache, tree, python="3.12", version="1.0.dev0",
                            extra_requirements=["pytest==8.0.0"], subdir=SUB)
    installs = [c[1]["argv"] for c in sb.calls if c[0] == "install"]  # type: ignore[index]
    assert installs[0][2] == INSTALLER and installs[0][3:] == [
        "/workspace/src.tar.gz", SRC_DIR, "pytest==8.0.0"]  # 不给 subdir：和以前逐字节一样
    assert installs[1][2] == INSTALLER_SUBDIR and installs[1][3:] == [
        "/workspace/src.tar.gz", SRC_DIR, SUB, "pytest==8.0.0"]
    assert plain.key != mono.key


class DepsPyPI(OfflinePyPI):
    """pytest 之外再给两个测试依赖的发布记录：按提交日期锁版本。"""

    async def releases(self, name: str):  # type: ignore[no-untyped-def]
        from test_repro_l2 import rel

        if name == "syrupy":
            return dict([rel("4.0.0", "2025-01-01", ">=3.8"), rel("5.0.0", "2025-12-01", ">=3.8")])
        if name == "pytest-asyncio":
            return dict([rel("0.25.0", "2025-03-01", ">=3.9")])
        return await super().releases(name)


async def test_prepare_installs_subdir_with_pinned_test_deps(tmp_path: Path):
    sb = RecordingSandbox()
    tester = TestReproducer(sb, EnvCache(sb, tmp_path / "envcache.json"),  # type: ignore[arg-type]
                            DepsPyPI())  # type: ignore[arg-type]
    cfg = PackageConfig(name="confkit", subdir=SUB, test_deps=["syrupy", "pytest-asyncio"])
    prep = await tester.prepare(cfg, mono_tree(), number=101)
    install = next(c[1]["argv"] for c in sb.calls if c[0] == "install")  # type: ignore[index]
    assert install[2] == INSTALLER_SUBDIR and install[5] == SUB
    # 2025-06 的提交：syrupy 取 4.0.0（5.0.0 在之后才发布），pytest-asyncio 0.25.0
    assert install[6:] == ["pytest==8.3.3", "syrupy==4.0.0", "pytest-asyncio==0.25.0"]
    assert prep.test_path == MONO_EXAM_PATH and prep.python == "3.12"
    # 预检的空测试写在子目录的测试目录里，pytest 仍以 src 为 rootdir（节点 ID 相对仓库根）
    probe = next(c[1] for c in sb.calls if c[0] == "run" and "pytest" in c[1])
    assert f"src/{MONO_EXAM_PATH}" in probe and "--rootdir=src" in probe  # type: ignore[operator]


async def test_l2_run_writes_the_test_under_the_subdir(tmp_path: Path):
    sb = FakeTestSandbox({"BUG": res(1, "KeyError: None")})
    prep = SourcePrepared(
        cfg=PackageConfig(name="confkit", subdir=SUB), tree=mono_tree(), python="3.12",
        version="0.3.1.dev0", pytest="pytest==8.0.0",
        env=__import__("failgate.repro.envcache", fromlist=["Env"]).Env(
            key="k" * 64, image="failgate-env:k", python="3.12", cache_hit=True),
        test_path=MONO_EXAM_PATH)
    run = await TestReproducer(sb, None, None).run_once(prep, "BUG = 1\n")  # type: ignore[arg-type]
    assert run.exit_code == 1
    assert f"src/{MONO_EXAM_PATH}" in sb.files
    check_command(sb.runs[-1], RUN_PREFIXES)


# ---------------------------------------------------------------- 导入根与 PYTHONPATH


def prepared_for(tree: SourceTree, subdir: str | None, module: str = "confkit") -> Any:
    return SourcePrepared(cfg=PackageConfig(name=module, subdir=subdir), tree=tree, python="3.12",
                          version="1", pytest="pytest", env=None, test_path="")  # type: ignore[arg-type]


def test_workspace_pythonpath_points_into_the_subdir():
    tree = mono_tree()
    assert workspace_pythonpath(prepared_for(tree, SUB)) == f"/workspace/src/{SUB}"
    src_layout = mono_tree(extra={f"lc-abc/{SUB}/src/confkit/__init__.py": b""})
    assert workspace_pythonpath(prepared_for(src_layout, SUB)) == f"/workspace/src/{SUB}/src"
    # 不给 subdir：和以前一样
    root = SourceTree(repo="a/b", sha="s", committed_at=None,
                      tarball=make_tar({"t/confkit/__init__.py": b""}))
    assert workspace_pythonpath(prepared_for(root, None)) == "/workspace/src"
    src = SourceTree(repo="a/b", sha="s", committed_at=None,
                     tarball=make_tar({"t/src/confkit/__init__.py": b""}))
    assert workspace_pythonpath(prepared_for(src, None)) == "/workspace/src/src"


@pytest.mark.parametrize(("path", "module", "root"), [
    ("libs/core/langchain_core/runnables/base.py", "langchain_core", "libs/core"),
    ("libs/core/src/pkg/a.py", "pkg", "libs/core/src"),
    ("src/black/linegen.py", "black", "src"),
    ("black/linegen.py", "black", ""),
    ("failgate_demo/text.py", "failgate_demo", ""),
    ("src/black/x.py", None, "src"),  # 不给包名：和以前一样
    ("libs/core/langchain_core/x.py", None, ""),
    ("src/blackd/x.py", "black", "src"),  # 包名只按整段目录名匹配
])
def test_import_root_follows_the_package(path: str, module: str | None, root: str):
    assert import_root(path, module) == root


def test_installed_path_and_coverage_lookup_in_a_monorepo():
    path = "libs/core/langchain_core/runnables/base.py"
    assert installed_path(path, "langchain_core") == "langchain_core/runnables/base.py"
    cov = {"/opt/venv/lib/python3.12/site-packages/langchain_core/runnables/base.py": {1}}
    assert executed_in_copy(cov, path, "langchain_core") is None  # 导入的是安装版：shadow
    cov = {f"/workspace/src/{path}": {3, 4}}
    assert executed_in_copy(cov, path, "langchain_core") == {3, 4}


# ---------------------------------------------------------------- 第三层：相关测试


def test_module_of_strips_the_monorepo_prefix():
    assert module_of("libs/core/langchain_core/runnables/base.py", "langchain_core") == \
        "langchain_core.runnables.base"
    assert module_of("libs/core/langchain_core/__init__.py", "langchain_core") == "langchain_core"
    assert module_of("libs/core/langchain_core/x.py") == "libs.core.langchain_core.x"  # 旧行为
    assert module_of("src/black/x.py", "black") == module_of("src/black/x.py") == "black.x"


def test_select_related_tests_in_a_monorepo():
    files = [PullFile(filename="libs/core/langchain_core/runnables/base.py", status="modified")]
    tests = {
        "libs/core/tests/unit_tests/runnables/test_runnable.py":
            "from langchain_core.runnables.base import Runnable\ndef test_a(): pass\n",
        "libs/core/tests/unit_tests/test_base.py": "def test_b(): pass\n",
        "libs/core/tests/unit_tests/test_misc.py":
            "from langchain_core import runnables\ndef test_c(): pass\n",
    }
    got = select_related_tests(files, tests, exclude="", package="langchain_core")
    assert got == ["libs/core/tests/unit_tests/test_base.py",
                   "libs/core/tests/unit_tests/runnables/test_runnable.py",
                   "libs/core/tests/unit_tests/test_misc.py"]
    # 不给包名时模块名带着 libs.core 前缀，import 匹配不上，只剩同名
    assert select_related_tests(files, tests, exclude="") == [
        "libs/core/tests/unit_tests/test_base.py"]


def test_layer3_only_picks_tests_inside_the_subdir():
    other = "libs/other/tests/test_parser.py"
    mine = f"{SUB}/tests/unit_tests/test_parser.py"
    t = "from confkit.parser import parse\ndef test_x(): pass\n"
    files = {BASE: {mine: t, other: t}, HEAD: {mine: t, other: t}}
    fail = vres(1, f"FAILED {MONO_EXAM_PATH}::test_parse - KeyError: None\n")
    ok = vres(0, f"PASSED {MONO_EXAM_PATH}::test_parse\n1 passed\n")
    bench = FakeBench(exam_runs={BASE: [fail, fail], HEAD: [ok, ok]}, files=files,
                      test_runs={BASE: [vres(0, "1 passed")], HEAD: [vres(0, "1 passed")]})
    e = exam(test_path=MONO_EXAM_PATH, package="confkit", module="confkit", subdir=SUB,
             signature=None)
    v = asyncio.run(ClaimVerifier(bench).verify(pull([pf(PARSER)]), [7], {7: e}))
    c = v.claims[0]
    assert v.verdict == ClaimVerdict.VERIFIED and c.layer3 is not None, c.reasons
    assert c.layer3.files == [mine]
    assert bench.test_calls == [(HEAD, [mine]), (BASE, [mine])]


# ---------------------------------------------------------------- 收据、考卷、负例


def receipt(**kw: Any) -> dict[str, Any]:
    from failgate.repro.judge import Verdict, VerdictKind

    r = build_receipt(
        repo="acme/mono", issue=101, level="L2", mode="source", test_path=MONO_EXAM_PATH,
        code="def test_x(): pass\n", package="confkit", command=["python", "-m", "pytest"],
        verdict=Verdict(kind=VerdictKind.REPRODUCED, reason="ok"),
        now=datetime(2026, 10, 9, tzinfo=UTC), evidence_id="e" * 32, **kw)
    return r.signed_dict()


def test_receipt_records_subdir_only_when_set():
    plain = receipt()
    assert "subdir" not in plain and "test_deps" not in plain  # 旧收据的哈希不变
    assert plain["receipt_sha256"] == receipt_digest(plain)
    mono = receipt(subdir=SUB, test_deps=["syrupy"])
    assert (mono["subdir"], mono["test_deps"]) == (SUB, ["syrupy"])
    assert mono["receipt_sha256"] != plain["receipt_sha256"]


def test_exam_from_receipt_keeps_the_environment():
    r = receipt(subdir=SUB, test_deps=["syrupy"])
    e = exam_from_receipt(r, "def test_x(): pass\n", "confkit")
    assert (e.subdir, e.test_deps) == (SUB, ["syrupy"])
    cfg = e.package_config()
    assert (cfg.name, cfg.module, cfg.subdir, cfg.test_deps) == (
        "confkit", "confkit", SUB, ["syrupy"])
    old = exam_from_receipt(receipt(), "def test_x(): pass\n", None)
    assert old.subdir is None and old.test_deps == [] and old.package_config().subdir is None


def test_negative_variants_use_the_subdir_test_dir(tmp_path: Path):
    e = exam(test_path=MONO_EXAM_PATH, package="confkit", module="confkit", subdir=SUB)
    assert ve.exam_test_dir(e) == f"{SUB}/tests"
    assert ve.exam_test_dir(exam()) == "tests"
    case = ve.EvalCase(number=101, title="t", exam=e, parent="p" * 40, fix="f" * 40,
                       upstream_pr=1)
    test_file = f"{SUB}/tests/unit_tests/test_parser.py"

    def tree(files: dict[str, str], sha: str) -> SourceTree:
        root = tmp_path / sha
        for p, c in files.items():
            (root / p).parent.mkdir(parents=True, exist_ok=True)
            (root / p).write_text(c, encoding="utf-8", newline="\n")
        return SourceTree(repo="acme/mono", sha=sha, committed_at=None, tarball=pack_dir(root))

    parent = tree({PARSER: "bug\n", test_file: "old\n", "README.md": "hi\n"}, "p")
    fix = tree({PARSER: "fixed\n", test_file: "new\n", "README.md": "hi\n"}, "f")
    fix_files = [PullFile(filename=PARSER, status="modified"),
                 PullFile(filename=test_file, status="modified")]
    head, files = ve.build_variant("revert_code", case, parent, fix, fix_files)
    got = head.read_files(lambda p: p.endswith(".py"))
    assert got[PARSER] == "bug\n" and got[test_file] == "new\n"
    assert [f.filename for f in files] == [test_file]  # 源码改动没有被当成测试带进来
    head, files = ve.build_variant("conftest_skip", case, parent, fix, fix_files)
    assert [f.filename for f in files] == [f"{SUB}/tests/conftest.py"]


def test_load_cases_reads_subdir_from_the_l2_record():
    run = {"reports": [{"number": 5, "source": {
        "test_path": MONO_EXAM_PATH, "package": "confkit", "module": "confkit",
        "python": "3.12", "pytest": "pytest==8", "version": "1.dev0", "subdir": SUB,
        "test_deps": ["syrupy"]}, "agent": {"final_script": "def test_x(): pass\n"}}],
        "fbpa": [{"number": 5, "title": "t", "outcome": "fb_pa",
                  "fix": {"sha": "f" * 40, "parent": "p" * 40}}]}
    (c,) = ve.load_cases(run)
    assert (c.exam.subdir, c.exam.test_deps) == (SUB, ["syrupy"])


# ---------------------------------------------------------------- 选题


def test_selection_source_prefix_is_optional_and_described():
    plain = Selection(bug_labels=["bug"])
    assert plain.source_prefix == "" and "修复须改到" not in plain.describe()
    mono = Selection(bug_labels=["bug"], source_prefix="libs/core/langchain_core/")
    assert "`libs/core/langchain_core/`" in mono.describe()


class CompareGH:
    def __init__(self, files: list[str]) -> None:
        self.files = files

    async def compare_files(self, repo: str, base: str, head: str) -> list[dict[str, Any]]:
        return [{"filename": f, "status": "modified"} for f in self.files]


@pytest.mark.parametrize(("changed", "prefix", "want"), [
    (["libs/core/langchain_core/x.py", "libs/core/tests/unit_tests/test_x.py"],
     "libs/core/langchain_core/", True),
    (["libs/partners/openai/langchain_openai/x.py"], "libs/core/langchain_core/", False),
    (["libs/partners/openai/langchain_openai/x.py"], "", True),  # 不配前缀：和以前一样
    (["libs/core/tests/unit_tests/test_x.py", "README.md"], "", False),
])
def test_fix_must_touch_the_source_prefix(changed: list[str], prefix: str, want: bool):
    from failgate.cli import _code_changed
    from failgate.replay.fixes import FixCommit

    trees = {"p" * 40: mono_tree(sha="p" * 40)}
    check = _code_changed(CompareGH(changed), "acme/mono", trees, subdir="libs/core",
                          source_prefix=prefix)
    fix = FixCommit(pr=None, sha="f" * 40, parent="p" * 40)
    assert asyncio.run(check(fix)) is want


def test_monorepo_fixture_loads_with_its_subdir():
    (fx,) = fx_mod.load_all(only=["mono-keyerror"])
    assert fx.cfg.subdir == SUB and fx.cfg.name == "confkit"
    assert fx.tree.test_dir(SUB) == f"{SUB}/tests/unit_tests"
    names = tarfile.open(fileobj=io.BytesIO(fx.fixed_tree.tarball), mode="r:gz").getnames()
    assert f"src/{PARSER}" in names


# ---------------------------------------------------------------- 真实 Docker


KEYERROR_EXAM = '''from confkit import parse


def test_keys_before_first_section_go_to_default() -> None:
    assert parse("name = demo\\n[server]\\nhost = example.org\\n") == {
        "DEFAULT": {"name": "demo"},
        "server": {"host": "example.org"},
    }
'''
REPORTED = (
    'Traceback (most recent call last):\n  File "/x/site-packages/confkit/parser.py", '
    "line 35, in _store\n    sections[section][key.strip()] = value.strip()\nKeyError: None\n"
)


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory: pytest.TempPathFactory) -> DockerSandbox:
    sb = DockerSandbox(artifacts_dir=tmp_path_factory.mktemp("artifacts"))
    if asyncio.run(sb.server_version()) is None:
        pytest.skip("Docker 不可用")
    return sb


async def _drop_images(sandbox: DockerSandbox, index: Path) -> None:
    import json

    try:
        raw = await asyncio.to_thread(index.read_text)
        images = [str(v["image"]) for v in json.loads(raw).values()]
    except (OSError, ValueError):
        return
    for image in images:
        await sandbox.remove_image(image)


@pytest.mark.docker
async def test_real_monorepo_source_l2_and_fix_acceptance(sandbox: DockerSandbox, tmp_path: Path):
    """装 libs/confkit（根目录不可安装）→ 预检（只用子目录的 pytest 配置）→ 考卷判 REPRODUCED
    → 修复 Agent 的补丁在全新工作区里通过（PYTHONPATH 指向子目录里的副本）。"""
    from test_fix import ScriptedLLM, call

    from failgate.fix.agent import FixTask
    from failgate.fix.run import fix_tree
    from failgate.repro.judge import VerdictKind
    from failgate.repro.package import IssueContext

    (fx,) = fx_mod.load_all(only=["mono-keyerror"])
    index = tmp_path / "envcache.json"
    tester = TestReproducer(sandbox, EnvCache(sandbox, index), OfflinePyPI())  # type: ignore[arg-type]
    try:
        prep = await tester.prepare(fx.cfg, fx.tree, number=fx.number, python=fx.python,
                                    version=fx.version)
        assert prep.test_path == MONO_EXAM_PATH
        run = await tester.evaluate(prep, KEYERROR_EXAM, reported_traceback=REPORTED)
        assert run.verdict.kind == VerdictKind.REPRODUCED, run.output_tail
        # 仓库自带的测试在子目录的配置下能跑（--strict-markers 只认子目录注册的 marker）
        ws = await tester.open_workspace(prep, "mono-own")
        try:
            own = await sandbox.run(prep.env.image, ws, [
                "python", "-m", "pytest", f"src/{SUB}/tests/unit_tests/test_parser.py",
                "-q", "-p", "no:cacheprovider", "--rootdir=src"], allowed=RUN_PREFIXES)
        finally:
            await sandbox.remove_workspace(ws)
        assert own.exit_code == 0, own.output_tail(20)

        llm = ScriptedLLM([
            {"tool_calls": [call("submit_plan", {
                "hypothesis": "current 初始为 None", "files": [PARSER],
                "approach": "默认 DEFAULT"})]},
            {"tool_calls": [
                call("edit_file", {"path": PARSER, "old": "    current = None\n",
                                   "new": "    current = DEFAULT\n"}, "e1"),
                call("edit_file", {"path": PARSER, "old": "    sections[section][key",
                                   "new": "    sections.setdefault(section, {})[key"}, "e2"),
            ]},
            {"tool_calls": [call("finish_edit", {"summary": "默认节"})]},
        ])
        task = FixTask(repo=fx.repo, number=fx.number,
                       issue=IssueContext(title=fx.title, body=fx.body),
                       test_path=MONO_EXAM_PATH, test_code=KEYERROR_EXAM)
        result = await fix_tree(llm.client(), "deepseek-flash", tester, fx.cfg, fx.tree, task,
                                python=fx.python, version=fx.version)
        assert result.status == "passed", result.attempts
        assert result.files == [PARSER]
    finally:
        await _drop_images(sandbox, index)


@pytest.mark.docker
def test_real_monorepo_verification(sandbox: DockerSandbox, tmp_path: Path):
    """base = 有 bug 的 monorepo，head = 打上 fix/：VERIFIED；第三层只跑 libs/confkit 的测试。"""
    from failgate.verify.workbench import SandboxWorkbench

    base_dir, head_dir = tmp_path / "base", tmp_path / "head"
    shutil.copytree(FIXTURE / "repo", base_dir)
    shutil.copytree(base_dir, head_dir)
    shutil.copytree(FIXTURE / "fix", head_dir, dirs_exist_ok=True)
    trees = {sha: SourceTree(repo="fixture/mono", sha=sha, committed_at=None,
                             tarball=pack_dir(d))
             for sha, d in ((BASE, base_dir), (HEAD, head_dir))}

    async def fetch(repo: str, sha: str) -> SourceTree:
        return trees[sha]

    index = tmp_path / "envcache.json"
    tester = TestReproducer(sandbox, EnvCache(sandbox, index), OfflinePyPI())  # type: ignore[arg-type]
    bench = SandboxWorkbench(fetch, tester)
    e = exam(test_path=MONO_EXAM_PATH, code=KEYERROR_EXAM, test_sha256=code_sha256(KEYERROR_EXAM),
             package="confkit", module="confkit", subdir=SUB, signature=None, pytest=None,
             version="0.3.1.dev0", python="3.12")
    try:
        v = asyncio.run(ClaimVerifier(bench).verify(pull([pf(PARSER)]), [7], {7: e}))
        c = v.claims[0]
        assert v.verdict == ClaimVerdict.VERIFIED, (c.reasons, c.layer1, c.layer3)
        assert c.layer3 is not None and c.layer3.files == [f"{SUB}/tests/unit_tests/test_parser.py"]
    finally:
        asyncio.run(_drop_images(sandbox, index))
