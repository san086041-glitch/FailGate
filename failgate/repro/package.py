"""package 模式的复现流程（技术方案 8.2 节）：

    报告的版本 ──PyPI 确认存在──→ 选 Python ──→ 环境（缓存 / 构建）──→ 跑脚本 ──→ 判定
                                                                        │ 复现了
                                                                        ▼
    最新正式版 ──同一个 Python（不支持时再选）──→ 环境 ──→ 跑同一个脚本 ──→ 判定
        没复现 → "可能已在 X 修复"；复现了 → "最新版仍存在"

脚本可以由调用方直接提供（failgate repro package），也可以由复现 Agent 生成
（agent.py 通过 prepare / evaluate 复用这里的环境和判定）。
在最新版上复查时尽量沿用同一个 Python：两次运行只差包版本这一个变量，结论才站得住。
每次评估都在一个只放了这份脚本的全新工作区里运行，不受 Agent 草稿区里其他文件的影响。
"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from failgate.repro.config import PackageConfig
from failgate.repro.envcache import Env, EnvBuildError, EnvCache
from failgate.repro.evidence import EvidenceLevel
from failgate.repro.judge import Verdict, VerdictKind, assess
from failgate.repro.pypi import PyPIClient, PyPIError, Release, ResolvedVersion, pick_python
from failgate.repro.sandbox import DockerSandbox, ExecResult, SandboxError
from failgate.repro.semantic import SemanticJudge, SemanticVerdict

SCRIPT_NAME = "repro.py"
SETUP_ERRORS = (PyPIError, EnvBuildError, SandboxError)


class IssueContext(BaseModel):
    """没有堆栈时，LLM 评委需要对照的 issue 描述。"""

    title: str
    body: str
    expected: str | None = None
    actual: str | None = None


class VersionRun(BaseModel):
    version: str
    python: str
    env_key: str
    cache_hit: bool
    verdict: Verdict
    semantic: SemanticVerdict | None = None
    log_dir: str | None = None
    output_tail: str = ""


class PackageRepro(BaseModel):
    package: str
    module: str
    reported_version: str | None = None
    # 不为空时：报告的版本装不到，reported_version 是 issue 创建前最新的正式版（替代版本）
    substituted_for: str | None = None
    latest_version: str | None = None
    reported: VersionRun | None = None
    latest: VersionRun | None = None
    level: EvidenceLevel = EvidenceLevel.NONE
    error: str | None = None

    @property
    def fixed_in_latest(self) -> bool | None:
        """True：最新版上没复现；False：最新版仍然复现；None：没法判断。"""
        if self.reported is None or not self.reported.verdict.reproduced or self.latest is None:
            return None
        kind = self.latest.verdict.kind
        if kind == VerdictKind.NOT_REPRODUCED:
            return True
        if self.latest.verdict.reproduced:
            return False
        return None

    def summary(self) -> str:
        if self.error:
            return f"未能复现：{self.error}"
        if self.reported is None:
            return "未能复现：没有可评估的脚本"
        v = self.reported.verdict
        sub = (
            f"；报告的是 {self.substituted_for}，装不到，改用 issue 创建前最新的正式版"
            if self.substituted_for else ""
        )
        head = (
            f"{self.package}=={self.reported_version}（Python {self.reported.python}{sub}）："
            f"{v.kind}，{v.reason}"
        )
        if self.latest is None:
            if v.reproduced and self.reported_version == self.latest_version:
                return head + "；报告的就是最新版"
            return head
        tail = {
            True: f"；最新版 {self.latest_version} 上没有复现，可能已经修复",
            False: f"；最新版 {self.latest_version} 上仍然复现",
            None: f"；最新版 {self.latest_version} 上无法判断（{self.latest.verdict.kind}）",
        }[self.fixed_in_latest]
        return head + tail


@dataclass
class Prepared:
    cfg: PackageConfig
    resolved: ResolvedVersion
    python: str
    env: Env


class PackageReproducer:
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

    async def prepare(
        self,
        cfg: PackageConfig,
        *,
        reported_version: str | None,
        env_python: str | None = None,
        preferred_python: str | None = None,
        fallback_before: datetime | None = None,
    ) -> Prepared:
        """解析版本、选 Python、准备环境。失败时抛 PyPIError / EnvBuildError / SandboxError。

        fallback_before（一般是 issue 的创建时间）：报告的版本装不到时，改用这之前最新的正式版。
        """
        resolved = await self.pypi.resolve(
            cfg.name, reported_version, fallback_before=fallback_before
        )
        python = pick_python(resolved.release, env_python, preferred_python)
        env = await self._env(cfg, resolved.release, python)
        return Prepared(cfg=cfg, resolved=resolved, python=python, env=env)

    async def _env(self, cfg: PackageConfig, release: Release, python: str) -> Env:
        return await self.cache.get(
            python=python, install_argv=cfg.install_argv(str(release.version))
        )

    async def evaluate(
        self,
        cfg: PackageConfig,
        env: Env,
        version: str,
        script: str,
        *,
        reported_traceback: str | None,
        issue: IssueContext | None = None,
    ) -> VersionRun:
        """在只放了这份脚本的全新工作区里运行并判定（按需重跑）。"""
        ws = await self.sandbox.create_workspace(f"eval-{env.key[:8]}")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                await asyncio.to_thread(
                    Path(tmp, SCRIPT_NAME).write_text, script, encoding="utf-8"
                )
                await self.sandbox.copy_in(ws, Path(tmp), env.image)

            async def once() -> ExecResult:
                return await self.sandbox.run(
                    env.image, ws, ["python", SCRIPT_NAME], timeout_s=self.run_timeout_s
                )

            first = await once()
            semantic: SemanticVerdict | None = None
            if not reported_traceback and first.failed and self.judge and issue:
                semantic = await self.judge.score(
                    issue_title=issue.title, issue_body=issue.body, expected=issue.expected,
                    actual=issue.actual, script=script, output=first.output_tail(80),
                )
            verdict = await assess(
                first, once, reported_traceback=reported_traceback, package=cfg.module,
                llm_match=semantic.match if semantic else None,
            )
        finally:
            await self.sandbox.remove_workspace(ws)
        return VersionRun(
            version=version, python=env.python, env_key=env.key, cache_hit=env.cache_hit,
            verdict=verdict, semantic=semantic, log_dir=first.log_dir,
            output_tail=first.output_tail(40),
        )

    async def check_latest(
        self,
        prepared: Prepared,
        script: str,
        *,
        reported_traceback: str | None,
        issue: IssueContext | None = None,
    ) -> tuple[VersionRun | None, str]:
        """用同一个脚本在最新正式版上复查。返回 (结果, 最新版本号说明)。"""
        resolved = prepared.resolved
        if resolved.latest == resolved.version:
            return None, str(resolved.latest)
        try:
            latest_rel = (await self.pypi.releases(prepared.cfg.name))[resolved.latest]
            # 尽量同一个 Python，只让包版本这一个变量变化
            py_latest = pick_python(latest_rel, prepared.python)
            env = await self._env(prepared.cfg, latest_rel, py_latest)
            run = await self.evaluate(
                prepared.cfg, env, str(resolved.latest), script,
                reported_traceback=reported_traceback, issue=issue,
            )
            return run, str(resolved.latest)
        except SETUP_ERRORS as e:
            # 最新版装不上不影响报告版本上的证据
            return None, f"{resolved.latest}（复查失败：{e}）"

    async def reproduce(
        self,
        cfg: PackageConfig,
        *,
        reported_version: str | None,
        script: str,
        reported_traceback: str | None,
        issue: IssueContext | None = None,
        env_python: str | None = None,
        preferred_python: str | None = None,
        check_latest: bool = True,
        fallback_before: datetime | None = None,
    ) -> PackageRepro:
        out = PackageRepro(package=cfg.name, module=cfg.module)
        try:
            prepared = await self.prepare(
                cfg, reported_version=reported_version, env_python=env_python,
                preferred_python=preferred_python, fallback_before=fallback_before,
            )
            out.reported_version = str(prepared.resolved.version)
            out.substituted_for = prepared.resolved.substituted_for
            out.latest_version = str(prepared.resolved.latest)
            out.reported = await self.evaluate(
                cfg, prepared.env, out.reported_version, script,
                reported_traceback=reported_traceback, issue=issue,
            )
        except SETUP_ERRORS as e:
            out.error = str(e)
            return out
        await self.finish(out, prepared, script, reported_traceback=reported_traceback,
                          issue=issue, check_latest=check_latest)
        return out

    async def finish(
        self,
        out: PackageRepro,
        prepared: Prepared,
        script: str,
        *,
        reported_traceback: str | None,
        issue: IssueContext | None,
        check_latest: bool,
    ) -> None:
        """报告版本上复现了：定证据等级，并到最新版上复查。"""
        if out.reported is None or not out.reported.verdict.reproduced:
            return
        out.level = EvidenceLevel.L1
        if check_latest:
            out.latest, out.latest_version = await self.check_latest(
                prepared, script, reported_traceback=reported_traceback, issue=issue
            )
