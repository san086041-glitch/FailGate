"""PR 上的两个能力模块（ADR 0018）：核验（VERIFYING）和重新封存考卷（RESEALING）。

它们只是把 verify/ 的引擎接进流水线：
- VerifySkill：取 PR 的最新 head，用封存的考卷做三层核验；结论作为事实交给状态机，核验收据
  交给流水线写进 verifications 表，报告在流水线停下时写成 PR 上的一条评论；
- ResealSkill：维护者 /failgate reseal 时，用 PR 里的考卷版本重新封存，旧考卷标成被取代，
  然后回到核验。谁发的命令记进新收据的 sealed_by。

真正干活的 runner 可以替换：线上是 GitHub + Docker 沙箱 + 数据库，测试里是假的。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel

from failgate.llm import Usage
from failgate.skills.base import SkillContext, SkillResult
from failgate.verify.engine import Verification
from failgate.verify.receipt import SealedTest

if TYPE_CHECKING:
    from failgate.db import Database
    from failgate.settings import Settings


class ResealOutcome(BaseModel):
    sealed: list[SealedTest] = []
    # 没有重新封存的原因：no_claim / no_exam:#N / missing:#N（PR 上没有这个文件）/
    # unchanged:#N（和封存的版本一样）
    notes: list[str] = []


class VerifyRunner(Protocol):
    async def verify(self, repo: str, number: int) -> Verification: ...
    async def reseal(self, repo: str, number: int, actor: str) -> ResealOutcome: ...


class VerifyOutput(BaseModel):
    verdict: str | None
    claims: list[int]
    head_sha: str
    verification: dict[str, Any]


class ResealOutput(BaseModel):
    resealed: list[dict[str, Any]]  # issue、新证据、取代的旧证据、谁封存的
    notes: list[str]


def _result(output: BaseModel, model: str, **kw: Any) -> SkillResult:
    return SkillResult(output=output, confidence=1.0, model=model, usage=Usage(),
                       cost_usd=0.0, **kw)


class VerifySkill:
    name = "verify"
    version = "1"

    def __init__(self, runner: VerifyRunner) -> None:
        self.runner = runner

    async def run(self, ctx: SkillContext) -> SkillResult:
        v = await self.runner.verify(ctx.issue.repo, ctx.issue.number)
        out = VerifyOutput(
            verdict=v.verdict.value if v.verdict else None, claims=[c.issue for c in v.claims],
            head_sha=v.head_sha, verification=v.model_dump(mode="json"),
        )
        # 核验不调用 LLM：判定全部由程序完成
        return _result(out, "-", facts={"verdict": out.verdict}, verification=v)


class ResealSkill:
    name = "reseal"
    version = "1"

    def __init__(self, runner: VerifyRunner) -> None:
        self.runner = runner

    async def run(self, ctx: SkillContext) -> SkillResult:
        actor = ctx.actor or "unknown"
        outcome = await self.runner.reseal(ctx.issue.repo, ctx.issue.number, actor)
        out = ResealOutput(
            resealed=[{"issue": s.receipt.issue, "evidence_id": s.receipt.evidence_id,
                       "supersedes": s.receipt.supersedes, "sealed_by": s.receipt.sealed_by}
                      for s in outcome.sealed],
            notes=outcome.notes,
        )
        return _result(out, "-", evidence=list(outcome.sealed))


class SandboxVerifyRunner:
    """线上用的 runner：GitHub 读 PR、数据库取考卷、Docker 沙箱跑测试。第一次调用时才初始化。"""

    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self._ready: tuple[Any, Any, Any] | None = None

    def _parts(self) -> tuple[Any, Any, Any]:
        from failgate.platforms.github_rest import GitHubRest
        from failgate.repro.envcache import EnvCache
        from failgate.repro.l2 import TestReproducer
        from failgate.repro.pypi import PyPIClient
        from failgate.repro.sandbox import DockerSandbox

        if self._ready is None:
            s = self.settings
            gh = GitHubRest(s.github_token, base_url=s.github_api_url)
            sandbox = DockerSandbox.from_settings(s)
            cache = EnvCache(sandbox, Path(s.sandbox_artifacts_dir) / "envcache.json",
                             max_bytes=int(s.sandbox_env_cache_gb * 1024**3),
                             index_url=s.pip_index_url)
            pypi = PyPIClient(s.pypi_url)
            tester = TestReproducer(sandbox, cache, pypi,
                                    run_timeout_s=s.sandbox_run_timeout_seconds)
            self._ready = (gh, pypi, tester)
        return self._ready

    async def _exams(self, repo: str, claims: list[int]) -> dict[int, Any]:
        from failgate.verify.store import latest_exam

        async with self.db.session() as s:
            return {n: await latest_exam(s, repo, n) for n in claims}

    async def verify(self, repo: str, number: int) -> Verification:
        from failgate.verify.claims import parse_claims
        from failgate.verify.engine import ClaimVerifier
        from failgate.verify.workbench import SandboxWorkbench, fetch_pull

        gh, _, tester = self._parts()
        pr = await fetch_pull(gh, repo, number)
        claims = parse_claims(pr.title, pr.body, repo)
        exams = await self._exams(repo, claims)
        verifier = ClaimVerifier(SandboxWorkbench.for_github(gh, tester),
                                 strength=self.settings.verify_strength,
                                 max_mutants=self.settings.strength_max_mutants)
        return await verifier.verify(pr, claims, exams)

    async def reseal(self, repo: str, number: int, actor: str) -> ResealOutcome:
        from failgate.verify.claims import parse_claims
        from failgate.verify.receipt import code_sha256, reseal_receipt
        from failgate.verify.workbench import fetch_pull

        gh, _, _ = self._parts()
        pr = await fetch_pull(gh, repo, number)
        claims = parse_claims(pr.title, pr.body, repo)
        if not claims:
            return ResealOutcome(notes=["no_claim"])
        out = ResealOutcome()
        for n, exam in (await self._exams(repo, claims)).items():
            if exam is None:
                out.notes.append(f"no_exam:#{n}")
                continue
            code = await gh.file_at(repo, exam.test_path, pr.head_sha)
            if code is None:
                out.notes.append(f"missing:#{n}")
            elif code_sha256(code) == exam.test_sha256:
                out.notes.append(f"unchanged:#{n}")
            else:
                receipt = reseal_receipt(exam.receipt, code, sealed_by=actor,
                                         source_sha=pr.head_sha)
                out.sealed.append(SealedTest(receipt=receipt, code=code))
        return out

    async def aclose(self) -> None:
        if self._ready is not None:
            gh, pypi, _ = self._ready
            await gh.aclose()
            await pypi.aclose()
