"""ClaimVerify 的真实 Workbench：GitHub 源码包 + Docker 沙箱（复用 L2 的 TestReproducer）。

安全边界和 L2 一样：PR 的代码（含 setup.py 等构建钩子）只在沙箱里执行，安装阶段没有密钥，
运行阶段断网、只读根文件系统、非 root。环境缓存的键含提交 SHA 和源码摘要，fork 的环境
不会覆盖 base 的环境。已知缺口：安装阶段还能访问整个互联网（egress 代理没做）。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from failgate.platforms.github_rest import GitHubRest
from failgate.repro.config import PackageConfig
from failgate.repro.envcache import EnvBuildError
from failgate.repro.l2 import (
    RUN_PREFIXES,
    L2Unsupported,
    SourcePrepared,
    TestReproducer,
    pytest_argv,
)
from failgate.repro.pypi import PyPIError
from failgate.repro.sandbox import ExecResult, SandboxError
from failgate.repro.source import SourceError, SourceTree, fetch_github_tree

from .engine import Exam, PullRequest, SetupFailed
from .tamper import PullFile

EXAM_TIMEOUT_S = 120
RELATED_ARGS = ["-q", "--tb=native", "-p", "no:cacheprovider", "--rootdir=src", "-rfE",
                "--continue-on-collection-errors"]
_SETUP_ERRORS = (SourceError, EnvBuildError, PyPIError, SandboxError, L2Unsupported,
                 httpx.HTTPError)


@dataclasses.dataclass
class Prepared:
    source: SourcePrepared

    @property
    def sha(self) -> str:
        return self.source.tree.sha


FetchTree = Callable[[str, str], Awaitable[SourceTree]]


class SandboxWorkbench:
    """fetch_tree(仓库, 提交) → 源码包。线上从 GitHub 取；测试和离线评测可以换成本地目录。"""

    def __init__(self, fetch_tree: FetchTree, tester: TestReproducer) -> None:
        self.fetch_tree = fetch_tree
        self.tester = tester

    @classmethod
    def for_github(cls, gh: GitHubRest, tester: TestReproducer) -> SandboxWorkbench:
        async def fetch(repo: str, sha: str) -> SourceTree:
            return await fetch_github_tree(gh, repo, sha)

        return cls(fetch, tester)

    async def prepare(self, repo: str, sha: str, exam: Exam) -> Prepared:
        try:
            tree = await self.fetch_tree(repo, sha)
            cfg = PackageConfig(name=exam.package, import_name=exam.module)
            src = await self.tester.prepare(cfg, tree, number=exam.issue, python=exam.python,
                                            version=exam.version, pytest=exam.pytest)
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
            await self.tester.write_test(ws, src, exam.code)
            return await self.tester.sandbox.run(
                src.env.image, ws, [*pytest_argv(exam.test_path), "-rA"],
                timeout_s=EXAM_TIMEOUT_S, allowed=RUN_PREFIXES,
            )
        finally:
            await self.tester.sandbox.remove_workspace(ws)

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

