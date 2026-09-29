"""L2：仓库内的失败测试（技术方案 8.1 节）。

    某个提交的源码环境（source.py，另装"该提交当时最新的 pytest"）
        │ 每次运行：全新工作区 ← 从镜像里的原始源码拷一份到 /workspace/src
        │           ← 写入 src/<测试目录>/test_failgate_issue_N.py
        ▼
    python -m pytest src/<测试文件> --rootdir=src -x -q --tb=native -p no:cacheprovider
        │ 退出码 2/3/4/5（收集出错、内部错误、用法错误、没收集到测试）→ 无关失败
        │ 退出码 1 → 现有判定器：签名 / 防伪造 / 重跑；没有堆栈时由 LLM 评委打分
        ▼
    REPRODUCED = L2：这个测试可以直接当修复的验收标准（修复前失败、修复后通过）

和 L1 的区别：测试放在仓库自己的测试目录里、按仓库自己的 pytest 配置运行（conftest、
filterwarnings 等都生效），维护者可以原样合进仓库；M3 的修复也用它验收。

为什么要预检：一些仓库的 conftest 依赖额外的测试库，或者 pytest 配置和这个 Python 不兼容。
先跑一个空测试，不通过就如实报告"环境不支持 L2"，不让 Agent 在坏环境里白花钱。
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from packaging.version import Version
from pydantic import BaseModel

from failgate.repro.config import PackageConfig
from failgate.repro.envcache import Env, EnvCache
from failgate.repro.evidence import EvidenceLevel
from failgate.repro.judge import Verdict, VerdictKind, assess
from failgate.repro.package import IssueContext, VersionRun
from failgate.repro.pypi import PackageNotFound, PyPIClient, Release
from failgate.repro.sandbox import DockerSandbox, ExecResult
from failgate.repro.semantic import SemanticJudge, SemanticVerdict
from failgate.repro.source import (
    SRC_DIR,
    SourceTree,
    pick_python_for_commit,
    pretend_version,
    source_env,
)

WORK_SRC = "src"  # 工作区里源码副本的目录（相对 /workspace）
# 从镜像拷源码到工作区。和 INSTALLER 一样按整段脚本精确匹配白名单
PREPARE_TREE = "import shutil, sys; shutil.copytree(sys.argv[1], sys.argv[2], dirs_exist_ok=True)"
PREPARE_ARGV = ["python", "-c", PREPARE_TREE, SRC_DIR, f"/workspace/{WORK_SRC}"]
PYTEST_ARGS = ["-x", "-q", "--tb=native", "-p", "no:cacheprovider", f"--rootdir={WORK_SRC}"]
RUN_PREFIXES: tuple[tuple[str, ...], ...] = (tuple(PREPARE_ARGV), ("python", "-m", "pytest"))
PROBE_TEST = "def test_failgate_probe():\n    pass\n"

# pytest 的退出码：只有 1（有测试失败）说明测试"跑起来并且失败了"
PYTEST_INVALID = {
    2: "测试收集出错或被中断（import 写错、语法错误、conftest 报错）",
    3: "pytest 内部错误",
    4: "pytest 用法错误",
    5: "没有收集到任何测试",
}


def repo_test_file(tree: SourceTree, number: int | None) -> str:
    """测试文件相对仓库根的路径。"""
    return f"{tree.test_dir()}/test_failgate_issue_{number or 0}.py"


def pytest_argv(rel_path: str) -> list[str]:
    return ["python", "-m", "pytest", f"{WORK_SRC}/{rel_path}", *PYTEST_ARGS]


def invalid_run(run: ExecResult) -> Verdict | None:
    """退出码说明测试根本没正常跑起来：直接判为无关失败，不进入签名比对。"""
    if run.infra_failure or run.exit_code not in PYTEST_INVALID:
        return None
    return Verdict(
        kind=VerdictKind.UNRELATED_FAILURE,
        reason=f"pytest 退出码 {run.exit_code}：{PYTEST_INVALID[run.exit_code]}，测试没有正常运行",
    )


def pick_pytest(releases: dict[Version, Release], python: str, before: datetime | None) -> str:
    """该提交当时已发布、支持这个 Python 的最新 pytest。找不到就不锁版本。"""
    return pick_release("pytest", releases, python, before)


def pick_release(name: str, releases: dict[Version, Release], python: str,
                 before: datetime | None) -> str:
    """before 之前已发布、支持这个 Python 的最新正式版，写成 name==X；找不到就不锁版本。"""
    ok = [
        v for v, r in releases.items()
        if not v.is_prerelease and not r.yanked
        and (before is None or (r.uploaded is not None and r.uploaded <= before))
        and (r.requires_python is None or r.requires_python.contains(f"{python}.0"))
    ]
    return f"{name}=={max(ok)}" if ok else name


@dataclass
class SourcePrepared:
    cfg: PackageConfig
    tree: SourceTree
    python: str
    version: str  # 伪版本号
    pytest: str  # pytest==X
    env: Env
    test_path: str


class SourceRepro(BaseModel):
    """source 模式的复现结果（一个提交上）。"""

    repo: str
    sha: str | None = None
    committed_at: datetime | None = None
    package: str
    module: str
    python: str | None = None
    version: str | None = None
    pytest: str | None = None
    test_path: str | None = None
    run: VersionRun | None = None
    level: EvidenceLevel = EvidenceLevel.NONE
    error: str | None = None


class TestReproducer:
    """source 模式下准备环境、预检，并在全新工作区里运行和判定一个测试文件。"""

    __test__ = False  # 名字以 Test 开头，别让 pytest 当成测试类收集

    def __init__(
        self,
        sandbox: DockerSandbox,
        cache: EnvCache,
        pypi: PyPIClient,
        *,
        run_timeout_s: int = 120,
        judge: SemanticJudge | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.cache = cache
        self.pypi = pypi
        self.run_timeout_s = run_timeout_s
        self.judge = judge

    def scoped(self, judge: SemanticJudge) -> TestReproducer:
        return TestReproducer(self.sandbox, self.cache, self.pypi,
                              run_timeout_s=self.run_timeout_s, judge=judge)

    async def prepare(
        self,
        cfg: PackageConfig,
        tree: SourceTree,
        *,
        number: int | None,
        python: str | None = None,
        version: str | None = None,
        pytest: str | None = None,
        extra: Sequence[str] = (),
    ) -> SourcePrepared:
        """构建环境（含 pytest）并预检。失败时抛 PyPIError / EnvBuildError / SandboxError /
        SourceError，预检不通过抛 L2Unsupported。

        version / pytest：指定伪版本号和 pytest 版本（严格 FB/PA 在修复前后用同一套，
        只让代码这一个变量变化）；不指定时按提交日期推算。
        extra：额外装的依赖（考卷强度要 coverage）；会进入缓存 key，得到单独的环境。
        """
        py = pick_python_for_commit(tree, reported=python)
        if version is None:
            version = pretend_version(await self._own_releases(cfg.name), tree.committed_at)
        pin = pytest or pick_pytest(await self.pypi.releases("pytest"), py, tree.committed_at)
        env = await source_env(self.cache, tree, python=py, version=version,
                               extra_requirements=[pin, *extra])
        prepared = SourcePrepared(cfg=cfg, tree=tree, python=py, version=version, pytest=pin,
                                  env=env, test_path=repo_test_file(tree, number))
        probe = await self.run_once(prepared, PROBE_TEST)
        if probe.exit_code != 0:
            raise L2Unsupported(
                f"空测试在这个环境里跑不通（exit={probe.exit_code}）：{probe.output_tail(12)}"
            )
        return prepared

    async def open_workspace(self, prepared: SourcePrepared, key: str) -> str:
        """新建工作区并放入源码副本。调用方负责 remove_workspace。"""
        ws = await self.sandbox.create_workspace(key)
        try:
            res = await self.sandbox.run(prepared.env.image, ws, PREPARE_ARGV, timeout_s=120,
                                         allowed=RUN_PREFIXES)
            if res.exit_code != 0:
                raise L2Unsupported(f"复制源码到工作区失败：{res.output_tail(8)}")
        except BaseException:
            await self.sandbox.remove_workspace(ws)
            raise
        return ws

    async def write_test(self, ws: str, prepared: SourcePrepared, content: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp, WORK_SRC, prepared.test_path)
            await asyncio.to_thread(dest.parent.mkdir, parents=True)
            await asyncio.to_thread(dest.write_text, content, encoding="utf-8")
            await self.sandbox.copy_in(ws, Path(tmp), prepared.env.image)

    async def _own_releases(self, name: str) -> dict[Version, Release]:
        """被测包自己的发布记录；没发布到 PyPI 的项目（应用、内部库）当作没有发布过。"""
        try:
            return await self.pypi.releases(name)
        except PackageNotFound:
            return {}

    async def run_test(self, ws: str, prepared: SourcePrepared, *, timeout_s: int | None = None
                       ) -> ExecResult:
        return await self.sandbox.run(
            prepared.env.image, ws, pytest_argv(prepared.test_path),
            timeout_s=timeout_s or self.run_timeout_s, allowed=RUN_PREFIXES,
        )

    async def run_once(self, prepared: SourcePrepared, content: str) -> ExecResult:
        """全新工作区里放源码副本和这一个测试文件，跑一次。"""
        ws = await self.open_workspace(prepared, f"l2-{prepared.env.key[:8]}")
        try:
            await self.write_test(ws, prepared, content)
            return await self.run_test(ws, prepared)
        finally:
            await self.sandbox.remove_workspace(ws)

    async def evaluate(
        self,
        prepared: SourcePrepared,
        content: str,
        *,
        reported_traceback: str | None,
        issue: IssueContext | None = None,
    ) -> VersionRun:
        """在全新工作区里运行并判定（按需重跑）。"""
        ws = await self.open_workspace(prepared, f"l2eval-{prepared.env.key[:8]}")
        try:
            await self.write_test(ws, prepared, content)

            async def once() -> ExecResult:
                return await self.run_test(ws, prepared)

            first = await once()
            semantic: SemanticVerdict | None = None
            verdict = invalid_run(first)
            if verdict is None:
                if not reported_traceback and first.failed and self.judge and issue:
                    semantic = await self.judge.score(
                        issue_title=issue.title, issue_body=issue.body,
                        expected=issue.expected, actual=issue.actual,
                        script=content, output=first.output_tail(80),
                    )
                verdict = await assess(
                    first, once, reported_traceback=reported_traceback,
                    package=prepared.cfg.module,
                    llm_match=semantic.match if semantic else None,
                )
        finally:
            await self.sandbox.remove_workspace(ws)
        return VersionRun(
            version=prepared.tree.sha[:10], python=prepared.python, env_key=prepared.env.key,
            cache_hit=prepared.env.cache_hit, verdict=verdict, semantic=semantic,
            log_dir=first.log_dir, output_tail=first.output_tail(40),
        )


class L2Unsupported(RuntimeError):
    """这个环境里跑不了仓库的测试（预检失败、源码复制失败）。"""
