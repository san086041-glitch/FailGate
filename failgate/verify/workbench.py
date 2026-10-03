"""ClaimVerify 的真实 Workbench：GitHub 源码包 + Docker 沙箱（复用 L2 的 TestReproducer）。

安全边界和 L2 一样：PR 的代码（含 setup.py 等构建钩子）只在沙箱里执行，安装阶段没有密钥，
运行阶段断网、只读根文件系统、非 root。环境缓存的键含提交 SHA 和源码摘要，fork 的环境
不会覆盖 base 的环境。已知缺口：安装阶段还能访问整个互联网（egress 代理没做）。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

import httpx

from failgate.platforms.github_rest import GitHubRest
from failgate.repro.config import PackageConfig
from failgate.repro.envcache import EnvBuildError
from failgate.repro.l2 import (
    RUN_PREFIXES,
    WORK_SRC,
    L2Unsupported,
    SourcePrepared,
    TestReproducer,
    pick_release,
    pytest_argv,
)
from failgate.repro.pypi import PyPIError
from failgate.repro.sandbox import ExecResult, SandboxError
from failgate.repro.source import SourceError, SourceTree, fetch_github_tree

from .engine import Exam, PullRequest, SetupFailed
from .hidden import is_hidden_path
from .strength import COVERAGE_PREFIX
from .tamper import PullFile

EXAM_TIMEOUT_S = 120
RELATED_ARGS = ["-q", "--tb=native", "-p", "no:cacheprovider", "--rootdir=src", "-rfE",
                "--continue-on-collection-errors"]
_SETUP_ERRORS = (SourceError, EnvBuildError, PyPIError, SandboxError, L2Unsupported,
                 httpx.HTTPError)


STRENGTH_PREFIXES = (*RUN_PREFIXES, COVERAGE_PREFIX)


@dataclasses.dataclass
class StrengthHandle:
    source: SourcePrepared
    workspace: str


@dataclasses.dataclass
class Prepared:
    source: SourcePrepared

    @property
    def sha(self) -> str:
        return self.source.tree.sha


FetchTree = Callable[[str, str], Awaitable[SourceTree]]


class SandboxWorkbench:
    """fetch_tree(仓库, 提交) → 源码包。线上从 GitHub 取；测试和离线评测可以换成本地目录。"""

    def __init__(self, fetch_tree: FetchTree, tester: TestReproducer,
                 test_deps: Sequence[str] = ()) -> None:
        self.fetch_tree = fetch_tree
        self.tester = tester
        # 第三层相关测试要的第三方依赖（仓库配置，ADR 0041）：只装 pytest 时 packaging 的
        # 测试 import pretend 就收集失败。按提交日期锁版本，进入环境缓存 key。
        self.test_deps = list(test_deps)

    @classmethod
    def for_github(cls, gh: GitHubRest, tester: TestReproducer,
                   test_deps: Sequence[str] = ()) -> SandboxWorkbench:
        async def fetch(repo: str, sha: str) -> SourceTree:
            return await fetch_github_tree(gh, repo, sha)

        return cls(fetch, tester, test_deps)

    async def _pinned_test_deps(self, tree: SourceTree, python: str) -> list[str]:
        return [pick_release(d, await self.tester.pypi.releases(d), python, tree.committed_at)
                for d in self.test_deps]

    async def prepare(self, repo: str, sha: str, exam: Exam) -> Prepared:
        try:
            tree = await self.fetch_tree(repo, sha)
            cfg = PackageConfig(name=exam.package, import_name=exam.module)
            extra = await self._pinned_test_deps(tree, exam.python or "3.12")
            src = await self.tester.prepare(cfg, tree, number=exam.issue, python=exam.python,
                                            version=exam.version, pytest=exam.pytest,
                                            extra=extra)
        except _SETUP_ERRORS as e:
            raise SetupFailed(f"{type(e).__name__}: {str(e)[:300]}") from e
        # 考卷的路径以封存时为准（head 上的测试目录可能变了）
        return Prepared(dataclasses.replace(src, test_path=exam.test_path))

    def read_files(self, prepared: Any, paths: set[str] | None = None) -> dict[str, str]:
        tree = prepared.source.tree
        if paths is None:
            return tree.read_files(lambda p: p.endswith(".py"))
        return tree.read_files(lambda p: p in paths)

    async def run_exam(self, prepared: Any, exam: Exam) -> ExecResult:
        """全新工作区：源码副本 + 封存的考卷（覆盖 PR 里的同名文件）。"""
        src: SourcePrepared = prepared.source
        ws = await self.tester.open_workspace(src, f"verify-{src.env.key[:8]}")
        try:
            # 写到这份考卷自己的路径（隐藏考卷和公开考卷路径不同）
            await self.tester.write_test(ws, dataclasses.replace(src, test_path=exam.test_path),
                                         exam.code)
            argv = pytest_argv(exam.test_path)
            if is_hidden_path(exam.test_path):
                # 隐藏考卷有好几道题，每道都要跑到；公开考卷保持 -x（和封存时的命令一致）
                argv = [a for a in argv if a != "-x"]
            return await self.tester.sandbox.run(
                src.env.image, ws, [*argv, "-rA"], timeout_s=EXAM_TIMEOUT_S,
                allowed=RUN_PREFIXES,
            )
        finally:
            await self.tester.sandbox.remove_workspace(ws)

    # ---------------------------------------------------------------- 考卷强度（strength.py）

    async def prepare_strength(self, repo: str, sha: str, exam: Exam) -> SourcePrepared:
        """head 的源码环境 + coverage：和核验用的环境分开缓存，核验本身不受影响。"""
        tree = await self.fetch_tree(repo, sha)
        cfg = PackageConfig(name=exam.package, import_name=exam.module)
        python = exam.python
        releases = await self.tester.pypi.releases("coverage")
        # coverage 是我们的工具，不是项目的依赖：不按提交日期锁，取支持这个 Python 的最新版
        pin = pick_release("coverage", releases, python or "3.12", None)
        src = await self.tester.prepare(cfg, tree, number=exam.issue, python=python,
                                        version=exam.version, pytest=exam.pytest, extra=[pin])
        return dataclasses.replace(src, test_path=exam.test_path)

    async def open_strength(self, prepared: SourcePrepared, exam: Exam) -> StrengthHandle:
        ws = await self.tester.open_workspace(prepared, f"strength-{prepared.env.key[:8]}")
        try:
            await self.tester.write_test(ws, prepared, exam.code)
        except BaseException:
            await self.tester.sandbox.remove_workspace(ws)
            raise
        return StrengthHandle(prepared, ws)

    async def run_coverage(self, handle: StrengthHandle, exam: Exam, pythonpath: str,
                           watch: list[str]) -> ExecResult:
        argv = [*COVERAGE_PREFIX, json.dumps(watch), *pytest_argv(exam.test_path)[3:], "-rA"]
        return await self.tester.sandbox.run(
            handle.source.env.image, handle.workspace, argv, timeout_s=EXAM_TIMEOUT_S,
            allowed=STRENGTH_PREFIXES, env=[f"PYTHONPATH={pythonpath}"],
        )

    async def put_file(self, handle: StrengthHandle, path: str, content: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp, WORK_SRC, path)
            await asyncio.to_thread(dest.parent.mkdir, parents=True)
            await asyncio.to_thread(dest.write_text, content, encoding="utf-8")
            await self.tester.sandbox.copy_in(handle.workspace, Path(tmp),
                                              handle.source.env.image)

    async def run_mutant(self, handle: StrengthHandle, exam: Exam, pythonpath: str,
                         timeout_s: int) -> ExecResult:
        return await self.tester.sandbox.run(
            handle.source.env.image, handle.workspace, [*pytest_argv(exam.test_path), "-rA"],
            timeout_s=timeout_s, allowed=RUN_PREFIXES, env=[f"PYTHONPATH={pythonpath}"],
        )

    async def close_strength(self, handle: StrengthHandle) -> None:
        await self.tester.sandbox.remove_workspace(handle.workspace)

    async def run_tests(self, prepared: Any, targets: list[str], timeout_s: int) -> ExecResult:
        src: SourcePrepared = prepared.source
        ws = await self.tester.open_workspace(src, f"related-{src.env.key[:8]}")
        try:
            argv = ["python", "-m", "pytest", *[f"src/{t}" for t in targets], *RELATED_ARGS]
            return await self.tester.sandbox.run(src.env.image, ws, argv, timeout_s=timeout_s,
                                                 allowed=RUN_PREFIXES)
        finally:
            await self.tester.sandbox.remove_workspace(ws)


async def fetch_pull(gh: GitHubRest, repo: str, number: int) -> PullRequest:
    """PR 信息 + 改动文件 + 合并基点。head_repo 只记录来源（fork 的提交也从 base 仓库取）。"""
    data = await gh.pull(repo, number)
    head_sha = data["head"]["sha"]
    head_repo = ((data["head"].get("repo") or {}).get("full_name")) or repo
    base = await gh.merge_base(repo, data["base"]["sha"], head_sha)
    files = [PullFile(filename=f["filename"], status=f["status"],
                      previous_filename=f.get("previous_filename"), patch=f.get("patch"))
             for f in await gh.pull_files(repo, number)]
    return PullRequest(repo=repo, number=number, title=data.get("title") or "",
                       body=data.get("body") or "", base_sha=base, head_sha=head_sha,
                       head_repo=head_repo, files=files)

