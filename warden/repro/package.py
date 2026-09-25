"""package 模式的复现流程（技术方案 8.2 节）：

    报告的版本 ──PyPI 确认存在──→ 选 Python ──→ 环境（缓存 / 构建）──→ 跑脚本 ──→ 判定
                                                                        │ 复现了
                                                                        ▼
    最新正式版 ──同一个 Python（不支持时再选）──→ 环境 ──→ 跑同一个脚本 ──→ 判定
        没复现 → "可能已在 X 修复"；复现了 → "最新版仍存在"

复现脚本由调用方提供（下一步由复现 Agent 生成）。这里只负责环境和执行，不调用 LLM。
在最新版上复查时尽量沿用同一个 Python：两次运行只差包版本这一个变量，结论才站得住。
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from pydantic import BaseModel

from warden.repro.config import PackageConfig
from warden.repro.envcache import EnvBuildError, EnvCache
from warden.repro.evidence import EvidenceLevel
from warden.repro.judge import Verdict, VerdictKind, assess
from warden.repro.pypi import PyPIClient, PyPIError, Release, pick_python
from warden.repro.sandbox import DockerSandbox, ExecResult, SandboxError

SCRIPT_NAME = "repro.py"


class VersionRun(BaseModel):
    version: str
    python: str
    env_key: str
    cache_hit: bool
    verdict: Verdict
    log_dir: str | None = None


class PackageRepro(BaseModel):
    package: str
    module: str
    reported_version: str | None = None
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
        assert self.reported is not None
        v = self.reported.verdict
        head = (
            f"{self.package}=={self.reported_version}（Python {self.reported.python}）："
            f"{v.kind}，{v.reason}"
        )
        if self.latest is None:
            if v.reproduced and self.reported_version == self.latest_version:
                return head + "；报告的就是最新版"
            return head
        fixed = self.fixed_in_latest
        tail = {
            True: f"；最新版 {self.latest_version} 上没有复现，可能已经修复",
            False: f"；最新版 {self.latest_version} 上仍然复现",
            None: f"；最新版 {self.latest_version} 上无法判断（{self.latest.verdict.kind}）",
        }[fixed]
        return head + tail


class PackageReproducer:
    def __init__(
        self,
        sandbox: DockerSandbox,
        cache: EnvCache,
        pypi: PyPIClient,
        *,
        run_timeout_s: int = 120,
    ) -> None:
        self.sandbox = sandbox
        self.cache = cache
        self.pypi = pypi
        self.run_timeout_s = run_timeout_s

    async def reproduce(
        self,
        cfg: PackageConfig,
        *,
        reported_version: str | None,
        script: str,
        reported_traceback: str | None,
        env_python: str | None = None,
        preferred_python: str | None = None,
        llm_match: float | None = None,
        check_latest: bool = True,
    ) -> PackageRepro:
        out = PackageRepro(package=cfg.name, module=cfg.module)
        try:
            resolved = await self.pypi.resolve(cfg.name, reported_version)
            out.reported_version, out.latest_version = str(resolved.version), str(resolved.latest)
            python = pick_python(resolved.release, env_python, preferred_python)
            out.reported = await self._run_version(
                cfg, resolved.release, python, script, reported_traceback, llm_match
            )
        except (PyPIError, EnvBuildError, SandboxError) as e:
            out.error = str(e)
            return out

        if out.reported.verdict.reproduced:
            out.level = EvidenceLevel.L1
            if check_latest and resolved.latest != resolved.version:
                try:
                    latest_rel = (await self.pypi.releases(cfg.name))[resolved.latest]
                    # 尽量同一个 Python，只让包版本这一个变量变化
                    py_latest = pick_python(latest_rel, python)
                    out.latest = await self._run_version(
                        cfg, latest_rel, py_latest, script, reported_traceback, llm_match
                    )
                except (PyPIError, EnvBuildError, SandboxError) as e:
                    # 最新版装不上不影响报告版本上的证据
                    out.latest_version = f"{resolved.latest}（复查失败：{e}）"
        return out

    async def _run_version(
        self,
        cfg: PackageConfig,
        release: Release,
        python: str,
        script: str,
        reported_traceback: str | None,
        llm_match: float | None,
    ) -> VersionRun:
        argv = cfg.install_argv(str(release.version))
        env = await self.cache.get(python=python, install_argv=argv)
        ws = await self.sandbox.create_workspace(f"repro-{env.key[:8]}")
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
            verdict = await assess(
                first, once, reported_traceback=reported_traceback,
                package=cfg.module, llm_match=llm_match,
            )
        finally:
            await self.sandbox.remove_workspace(ws)
        return VersionRun(
            version=str(release.version), python=python, env_key=env.key,
            cache_hit=env.cache_hit, verdict=verdict, log_dir=first.log_dir,
        )
