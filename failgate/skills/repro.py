"""Repro 能力模块：REPRODUCING 阶段在沙箱里复现 bug（技术方案第 7、8 节）。

它只是把第 8 节的复现子系统（repro/ 包）接进流水线：
- 仓库级配置（包名、源码仓库）从 ctx.repo_config 来，由 `failgate repo repro` 设置；
- 报告的是 PyPI 上装不到的版本、并且配置了源码仓库时，改走 source 模式（L2：仓库内的
  失败测试，ADR 0013），否则走 package 模式（L1，ADR 0010）；
- Intake 已经跑过，直接用它的结果（版本、Python、预期 / 实际行为）；
- 提问者后来补充的评论会拼到正文后面，NEED_INFO → 作者回复 → 重新复现时就能用上；
- 预算取"复现上限"和"这个 Case 剩下的预算"里较小的那个；
- 真正干活的 runner 可以替换：线上是 Docker + LLM，测试里是假的。

facts 里的 evidence_level 决定下一个状态：L1 及以上（含 L2）→ REPRODUCED，否则 → NEED_INFO。
复现成功时同时生成证据收据（ADR 0016）：收据进评论，收据 + 完整代码由流水线封存进 evidence 表。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, ValidationError

from failgate.llm import Usage
from failgate.repro.config import PackageConfig
from failgate.repro.evidence import EvidenceLevel
from failgate.repro.issue import IssueReproReport, L2IssueReport
from failgate.skills.base import SkillContext, SkillResult
from failgate.skills.intake import IntakeOutput
from failgate.verify.receipt import SealedTest, build_receipt

if TYPE_CHECKING:
    from failgate.llm import LLMClient
    from failgate.platforms.github_rest import GitHubRest
    from failgate.repro.l2 import TestReproducer
    from failgate.repro.package import PackageReproducer
    from failgate.settings import Settings

log = logging.getLogger(__name__)

MAX_FOLLOWUPS = 5
MAX_FOLLOWUP_CHARS = 4000
MAX_SCRIPT_CHARS = 6000
# 这些错误说明"报告里的版本有问题"，可以直接告诉提问者；其余（Docker、网络）属于内部问题
_VERSION_ERRORS = ("无法从", "PyPI 上没有", "装不到", "没有正式发布")


@dataclass
class ReproRequest:
    repo: str
    number: int
    title: str
    body: str
    created_at: datetime | None
    intake: IntakeOutput
    cfg: PackageConfig
    budget_usd: float
    source_repo: str | None = None  # 配置了才可能走 source 模式


ReproRunner = Callable[[ReproRequest], Awaitable[IssueReproReport | L2IssueReport]]


class ReproOutput(BaseModel):
    """写进 runs 表、供汇总评论渲染的复现结果。"""

    attempted: bool = True
    mode: str = "package"  # package（L1）/ source（L2）
    level: str = EvidenceLevel.NONE.value
    package: str | None = None
    reported_version: str | None = None
    substituted_for: str | None = None
    python: str | None = None
    latest_version: str | None = None
    fixed_in_latest: bool | None = None
    verdict: str | None = None
    verdict_reason: str | None = None
    match: float | None = None
    match_method: str | None = None
    runs: int | None = None
    fail_rate: float | None = None
    script: str | None = None
    agent_status: str | None = None
    steps: int = 0
    submits: int = 0
    give_up_reason: str | None = None
    suspect: str | None = None
    error: str | None = None
    # 可以公开写进评论的错误说明；None 表示不对外说具体原因
    public_error: str | None = None
    followup_comments: int = 0
    duration_s: float = 0.0
    transcript_path: str | None = None
    # source 模式：在哪个仓库的哪个提交上写的测试，测试文件在仓库里的路径
    source_repo: str | None = None
    source_sha: str | None = None
    test_path: str | None = None
    # 证据收据（带 receipt_sha256）；只有复现成功时才有
    evidence_id: str | None = None
    receipt: dict[str, Any] | None = None

    @classmethod
    def from_any(cls, report: IssueReproReport | L2IssueReport, followups: int) -> ReproOutput:
        if isinstance(report, L2IssueReport):
            return cls.from_l2_report(report, followups)
        return cls.from_report(report, followups)

    @classmethod
    def from_l2_report(cls, report: L2IssueReport, followups: int) -> ReproOutput:
        src, a = report.source, report.agent
        v = src.run.verdict if src.run else None
        err = src.error or (a.error if a else None)
        code = a.final_script if a and a.final_script else (
            a.attempts[-1].script if a and a.attempts else None
        )
        return cls(
            mode="source", level=src.level.value, package=src.package,
            reported_version=report.intake_version, python=src.python,
            verdict=v.kind.value if v else None, verdict_reason=v.reason if v else None,
            match=v.match if v else None, match_method=v.match_method if v else None,
            runs=v.runs if v else None, fail_rate=v.fail_rate if v else None,
            script=code[:MAX_SCRIPT_CHARS] if code else None,
            agent_status=a.status if a else None, steps=a.steps if a else 0,
            submits=len(a.attempts) if a else 0,
            give_up_reason=a.give_up_reason if a else None, suspect=a.suspect if a else None,
            # 源码拿不到、环境搭不起来都是内部问题，不对外说具体原因
            error=err, public_error=None, followup_comments=followups,
            duration_s=a.duration_s if a else 0.0,
            transcript_path=a.transcript_path if a else None,
            source_repo=src.repo, source_sha=src.sha, test_path=src.test_path,
        )

    @classmethod
    def from_report(cls, report: IssueReproReport, followups: int) -> ReproOutput:
        r, a = report.repro, report.agent
        v = r.reported.verdict if r.reported else None
        err = r.error or (a.error if a else None)
        public = err if err and err.startswith(_VERSION_ERRORS) else None
        script = a.final_script if a and a.final_script else (
            a.attempts[-1].script if a and a.attempts else None
        )
        return cls(
            level=r.level.value, package=r.package, reported_version=r.reported_version,
            substituted_for=r.substituted_for,
            python=r.reported.python if r.reported else None,
            latest_version=r.latest_version, fixed_in_latest=r.fixed_in_latest,
            verdict=v.kind.value if v else None, verdict_reason=v.reason if v else None,
            match=v.match if v else None, match_method=v.match_method if v else None,
            runs=v.runs if v else None, fail_rate=v.fail_rate if v else None,
            script=script[:MAX_SCRIPT_CHARS] if script else None,
            agent_status=a.status if a else None, steps=a.steps if a else 0,
            submits=len(a.attempts) if a else 0,
            give_up_reason=a.give_up_reason if a else None, suspect=a.suspect if a else None,
            error=err, public_error=public, followup_comments=followups,
            duration_s=a.duration_s if a else 0.0,
            transcript_path=a.transcript_path if a else None,
        )


class ReproSkill:
    name = "repro"
    version = "1"

    def __init__(self, runner: ReproRunner, *, max_budget_usd: float) -> None:
        self.runner = runner
        self.max_budget_usd = max_budget_usd

    async def run(self, ctx: SkillContext) -> SkillResult:
        skipped = self._skip_reason(ctx)
        if skipped is not None:
            return _result(ReproOutput(attempted=False, error=skipped), ctx.model)
        cfg = PackageConfig(
            name=ctx.repo_config["repro_package"],
            import_name=ctx.repo_config.get("repro_import_name"),
        )
        intake = IntakeOutput.model_validate(ctx.prior["intake"])
        budget = self.max_budget_usd
        if ctx.budget_left_usd is not None:
            budget = min(budget, ctx.budget_left_usd)
        body, followups = await self._body_with_followups(ctx)
        issue = ctx.issue
        report = await self.runner(ReproRequest(
            repo=issue.repo, number=issue.number, title=issue.title, body=body,
            created_at=issue.created_at, intake=intake, cfg=cfg, budget_usd=budget,
            source_repo=ctx.repo_config.get("repro_source") or None,
        ))
        out = ReproOutput.from_any(report, followups)
        sealed = seal(report)
        if sealed is not None:
            out.evidence_id = sealed.receipt.evidence_id
            out.receipt = sealed.signed()
        return SkillResult(
            output=out,
            confidence=1.0 if out.level != EvidenceLevel.NONE.value else 0.0,
            model=ctx.model,
            usage=report.repro_usage(),
            cost_usd=report.total_cost_usd,
            facts={"evidence_level": out.level},
            evidence=sealed,
        )

    @staticmethod
    def _skip_reason(ctx: SkillContext) -> str | None:
        if not ctx.repo_config.get("repro_package"):
            return "仓库没有配置复现用的包（failgate repo repro）"
        if "intake" not in ctx.prior:
            return "没有 Intake 结果"
        try:
            PackageConfig(name=ctx.repo_config["repro_package"])
            IntakeOutput.model_validate(ctx.prior["intake"])
        except ValidationError as e:
            return f"配置或 Intake 结果不合法：{e.errors()[0]['msg']}"
        return None

    @staticmethod
    async def _body_with_followups(ctx: SkillContext) -> tuple[str, int]:
        """把提问者后来补充的评论拼到正文后面（NEED_INFO 之后重新复现时用得上）。"""
        issue = ctx.issue
        if ctx.comments is None or not issue.author:
            return issue.body, 0
        try:
            comments = await ctx.comments(issue.repo, issue.number)
        except Exception:
            # 读评论失败（权限、网络）不影响复现，只是用不上补充信息
            log.warning("failed to load comments for %s#%s", issue.repo, issue.number,
                        exc_info=True)
            return issue.body, 0
        own = [c for c in comments if c.author.login == issue.author and not c.author.is_bot]
        own = own[-MAX_FOLLOWUPS:]
        if not own:
            return issue.body, 0
        extra = "\n\n".join(c.body for c in own)[:MAX_FOLLOWUP_CHARS]
        return f"{issue.body}\n\n---\n（提问者后来补充的评论）\n\n{extra}", len(own)


PROVEN = (EvidenceLevel.L1, EvidenceLevel.L2)


def seal(report: IssueReproReport | L2IssueReport) -> SealedTest | None:
    """复现成功（L1 / L2）时生成收据；没复现、或拿不到最终代码和判定时返回 None。"""
    from failgate.repro.l2 import pytest_argv
    from failgate.repro.package import SCRIPT_NAME

    a = report.agent
    code = a.final_script if a else None
    if not code:
        return None
    if isinstance(report, L2IssueReport):
        src = report.source
        run = src.run
        if src.level not in PROVEN or run is None or not src.test_path:
            return None
        receipt = build_receipt(
            repo=report.repo, issue=report.number, level=src.level.value, mode="source",
            test_path=src.test_path, code=code, package=src.package,
            command=pytest_argv(src.test_path), verdict=run.verdict, version=src.version,
            source_repo=src.repo, source_sha=src.sha, python=src.python, pytest=src.pytest,
        )
    else:
        r = report.repro
        run = r.reported
        if r.level not in PROVEN or run is None:
            return None
        receipt = build_receipt(
            repo=report.repo, issue=report.number, level=r.level.value, mode="package",
            test_path=SCRIPT_NAME, code=code, package=r.package,
            command=["python", SCRIPT_NAME], verdict=run.verdict, version=run.version,
            python=run.python,
        )
    if not run.verdict.reproduced:
        return None
    return SealedTest(receipt=receipt, code=code)


def _result(out: ReproOutput, model: str) -> SkillResult:
    return SkillResult(
        output=out, confidence=0.0, model=model, usage=Usage(), cost_usd=0.0,
        facts={"evidence_level": out.level},
    )


class SandboxReproRunner:
    """线上用的 runner：Docker 沙箱 + 环境缓存 + PyPI + 复现 Agent。第一次调用时才初始化。

    先决定模式：报告的版本 PyPI 上装不到、并且仓库配置了源码仓库 → source 模式（L2）；
    否则 → package 模式（L1）。
    """

    def __init__(self, settings: Settings, llm: LLMClient) -> None:
        self.settings = settings
        self.llm = llm
        self._reproducer: PackageReproducer | None = None
        self._tester: TestReproducer | None = None
        self._gh: GitHubRest | None = None

    def _get_reproducer(self) -> PackageReproducer:
        from failgate.repro.envcache import EnvCache
        from failgate.repro.package import PackageReproducer
        from failgate.repro.pypi import PyPIClient
        from failgate.repro.sandbox import DockerSandbox, SandboxLimits

        if self._reproducer is None:
            s = self.settings
            sandbox = DockerSandbox(
                s.docker_bin,
                limits=SandboxLimits(memory=s.sandbox_memory, cpus=s.sandbox_cpus),
                install_network=s.sandbox_install_network,
                artifacts_dir=Path(s.sandbox_artifacts_dir),
            )
            cache = EnvCache(
                sandbox, Path(s.sandbox_artifacts_dir) / "envcache.json",
                max_bytes=int(s.sandbox_env_cache_gb * 1024**3), index_url=s.pip_index_url,
            )
            self._reproducer = PackageReproducer(
                sandbox, cache, PyPIClient(s.pypi_url),
                run_timeout_s=s.sandbox_run_timeout_seconds,
            )
        return self._reproducer

    def _get_tester(self) -> TestReproducer:
        from failgate.repro.l2 import TestReproducer

        if self._tester is None:
            p = self._get_reproducer()
            self._tester = TestReproducer(p.sandbox, p.cache, p.pypi,
                                          run_timeout_s=self.settings.sandbox_run_timeout_seconds)
        return self._tester

    async def _use_source(self, req: ReproRequest) -> bool:
        from failgate.repro.config import needs_source
        from failgate.repro.pypi import PyPIError

        if not req.source_repo:
            return False
        try:
            released = await self._get_reproducer().pypi.releases(req.cfg.name)
        except (PyPIError, httpx.HTTPError):
            return False  # 查不到发布记录就按原来的 package 模式走，由它报出具体错误
        return needs_source(req.intake.reported_version, req.cfg.name, released)

    async def __call__(self, req: ReproRequest) -> IssueReproReport | L2IssueReport:
        from failgate.repro.issue import reproduce_after_intake, reproduce_l2_after_intake

        s = self.settings
        if await self._use_source(req):
            from failgate.platforms.github_rest import GitHubRest

            if self._gh is None:
                self._gh = GitHubRest(s.github_token)
            assert req.source_repo is not None
            return await reproduce_l2_after_intake(
                repo=req.repo, number=req.number, title=req.title, body=req.body,
                created_at=req.created_at, intake=req.intake, cfg=req.cfg,
                source_repo=req.source_repo, gh=self._gh, llm=self.llm,
                model=s.llm_model_large, tester=self._get_tester(),
                max_steps=s.repro_max_steps, max_attempts=s.repro_max_attempts,
                budget_usd=req.budget_usd, artifacts_dir=Path(s.sandbox_artifacts_dir),
            )
        return await reproduce_after_intake(
            repo=req.repo, number=req.number, title=req.title, body=req.body,
            created_at=req.created_at, intake=req.intake, cfg=req.cfg, llm=self.llm,
            model=s.llm_model_large, reproducer=self._get_reproducer(),
            max_steps=s.repro_max_steps, max_attempts=s.repro_max_attempts,
            budget_usd=req.budget_usd, artifacts_dir=Path(s.sandbox_artifacts_dir),
        )

    async def aclose(self) -> None:
        if self._reproducer is not None:
            await self._reproducer.pypi.aclose()
        if self._gh is not None:
            await self._gh.aclose()
