"""出题 → 答题 → 阅卷闭环里"答题"的两个能力模块（ADR 0029）。

- FixSkill（issue 上的 FIXING）：维护者 /failgate fix 之后，自带修复 Agent 在默认分支最新提交上修，
  考卷用封存的那份。补丁在全新工作区里过了考卷，才产出"推送请求"，由流水线交给 PolicyGate，
  执行器用 Fixer App 推到 failgate/fix-N 并开 PR（写 Fixes #N），PR 自动进入核验。
- RefixSkill（Fixer 开的 PR 上的 REFIXING）：PR 被 ClaimVerify 驳回时，把驳回理由交回修复 Agent，
  在上一版补丁的基础上改；第三层查出的新增失败要和考卷一起在全新工作区里通过，才推新提交。

两个模块都不直接写 GitHub：推送和评论都是流水线提议的写操作（Effect），影子模式下只记录。
真正干活的 runner 可以替换：线上是 GitHub + Docker 沙箱 + 数据库，测试里是假的。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, Field

from failgate.llm import Usage
from failgate.skills.base import SkillContext, SkillResult

if TYPE_CHECKING:
    from failgate.db import Database
    from failgate.fix.agent import FixResult
    from failgate.llm import LLMClient
    from failgate.settings import Settings


log = logging.getLogger(__name__)


class PushRequest(BaseModel):
    """交给 Fixer App 的推送：base 提交的树 + 这些文件（完整内容）。"""

    repo: str
    base_sha: str
    base_branch: str
    branch: str
    files: dict[str, str]
    message: str
    title: str
    body: str


class FixOutput(BaseModel):
    issue: int | None = None
    round: int = 0  # 0 = 第一次修复；1、2 = 按驳回理由重修的第几轮
    attempted: bool = False
    reason: str = ""  # 没有推送的原因：no_exam / not_passed / no_change / not_refuted / …
    agent_status: str | None = None
    passed: bool = False  # 考卷（和 must_pass）在全新工作区里通过
    files: list[str] = Field(default_factory=list)
    patch: str = ""
    steps: int = 0
    duration_s: float = 0.0
    feedback: str | None = None
    must_pass: list[str] = Field(default_factory=list)
    transcript_path: str | None = None
    push: PushRequest | None = None


class FixRunner(Protocol):
    async def fix(self, repo: str, issue: int, title: str, body: str,
                  budget_usd: float) -> tuple[FixOutput, FixResult | None]: ...

    async def refix(self, repo: str, pr: int, verification: dict[str, Any], round_: int,
                    budget_usd: float) -> tuple[FixOutput, FixResult | None]: ...


def _usage(res: FixResult | None) -> Usage:
    if res is None:
        return Usage()
    return Usage(res.prompt_tokens, res.completion_tokens, res.cached_tokens,
                 res.reasoning_tokens)


def _result(out: FixOutput, res: FixResult | None, model: str) -> SkillResult:
    return SkillResult(output=out, confidence=1.0, model=model, usage=_usage(res),
                       cost_usd=res.cost_usd if res else 0.0,
                       facts={"fix_ok": out.push is not None})


class FixSkill:
    name = "fix"
    version = "1"

    def __init__(self, runner: FixRunner, *, max_budget_usd: float) -> None:
        self.runner = runner
        self.max_budget_usd = max_budget_usd

    async def run(self, ctx: SkillContext) -> SkillResult:
        budget = min(self.max_budget_usd, ctx.budget_left_usd or self.max_budget_usd)
        i = ctx.issue
        try:
            out, res = await self.runner.fix(i.repo, i.number, i.title, i.body or "", budget)
        except Exception:
            # 出错也要让 Case 离开 FIXING（回到 REPRODUCED），维护者可以再发一次 /failgate fix
            log.exception("fix failed on %s#%s", i.repo, i.number)
            return _result(FixOutput(issue=i.number, reason="error"), None, ctx.model)
        return _result(out, res, ctx.model)


class RefixSkill:
    name = "refix"
    version = "1"

    def __init__(self, runner: FixRunner, *, max_budget_usd: float) -> None:
        self.runner = runner
        self.max_budget_usd = max_budget_usd

    async def run(self, ctx: SkillContext) -> SkillResult:
        verification = (ctx.prior.get("verify") or {}).get("verification")
        if verification is None:
            return _result(FixOutput(reason="no_verification"), None, ctx.model)
        done = int((ctx.prior.get("refix") or {}).get("round", 0))
        budget = min(self.max_budget_usd, ctx.budget_left_usd or self.max_budget_usd)
        try:
            out, res = await self.runner.refix(ctx.issue.repo, ctx.issue.number, verification,
                                               done + 1, budget)
        except Exception:
            log.exception("refix failed on %s#%s", ctx.issue.repo, ctx.issue.number)
            return _result(FixOutput(round=done, reason="error"), None, ctx.model)
        if not out.attempted:
            out.round = done  # 没动手修：轮次编号不前进（和流水线的轮数统计一致）
        return _result(out, res, ctx.model)


# ---------------------------------------------------------------- PR 文案


def pr_texts(issue: int, issue_title: str, summary: str, *, exam_path: str, exam_sha: str,
             evidence_id: str, lang: str) -> tuple[str, str]:
    if lang == "zh":
        title = f"修复 #{issue}：{issue_title}"[:200]
        body = (
            f"Fixes #{issue}\n\n"
            f"由 FailGate 自带修复 Agent 生成。{summary}\n\n"
            f"- 验收测试：`{exam_path}`（在修复之前封存，sha256 `{exam_sha[:12]}`，证据 "
            f"`{evidence_id[:8]}`），原样加进了这个 PR，修复 Agent 不能改它\n"
            "- 这个 PR 会由 FailGate 自动核验：考卷修复前后、防篡改、相关测试有没有新增失败；"
            "被驳回时修复 Agent 会按理由自动再改（有轮数上限）\n"
        )
    else:
        title = f"Fix #{issue}: {issue_title}"[:200]
        body = (
            f"Fixes #{issue}\n\n"
            f"Generated by the FailGate fix agent. {summary}\n\n"
            f"- Acceptance test: `{exam_path}` (sealed before the fix, sha256 `{exam_sha[:12]}`, "
            f"evidence `{evidence_id[:8]}`), added unchanged; the fix agent cannot modify it\n"
            "- FailGate verifies this PR automatically (acceptance test before/after, tampering, "
            "regressions in related tests); if refuted, the fix agent revises it (bounded rounds)\n"
        )
    return title, body


def fix_comment(out: FixOutput, lang: str) -> str:
    """修复（或重修）结束后在 issue / PR 上留的一条说明。"""
    zh = lang == "zh"
    # 没动手修的回复（例如理由不可操作）不带轮次
    n = out.round if out.attempted else 0
    if zh:
        head = "FailGate 修复 Agent" + (f"（第 {n} 轮重修）" if n else "")
    else:
        head = f"FailGate fix agent (revision {n})" if n else "FailGate fix agent"
    if out.push is not None:
        if zh:
            msg = (f"补丁在全新工作区里通过了封存的考卷"
                   f"{'和上一轮被查出的相关测试' if out.must_pass else ''}，"
                   f"改动 {len(out.files)} 个文件；由 Fixer App 推到 `{out.push.branch}`，"
                   "PR 会自动进入核验。")
        else:
            msg = (f"The patch passes the sealed acceptance test"
                   f"{' and the previously failing related tests' if out.must_pass else ''} "
                   f"in a fresh workspace ({len(out.files)} file(s)); the Fixer App pushes it to "
                   f"`{out.push.branch}` and the PR will be verified automatically.")
    else:
        reasons_zh = {
            "no_exam": "这个 issue 没有封存的考卷，先要复现出 L2 测试",
            "not_passed": "补丁没能在全新工作区里通过验收，没有推送",
            "no_change": "按驳回理由没能改出新的补丁，停止重修",
            "not_actionable": "驳回理由是修复 Agent 改不了的（例如测试被改动），交给维护者",
            "not_refuted": "核验没有驳回这个 PR，不需要重修",
            "no_verification": "没有找到核验结果",
            "error": "修复过程出错（内部错误，详见日志）",
        }
        reasons_en = {
            "no_exam": "this issue has no sealed acceptance test yet",
            "not_passed": "the patch did not pass acceptance in a fresh workspace; nothing pushed",
            "no_change": "could not produce a new patch from the refutation; stopping",
            "not_actionable": "the refutation is not something the fix agent can change",
            "not_refuted": "the PR was not refuted",
            "no_verification": "no verification result found",
            "error": "internal error during the fix (see logs)",
        }
        table = reasons_zh if zh else reasons_en
        msg = table.get(out.reason, out.reason)
    return f"**{head}**：{msg}" if zh else f"**{head}**: {msg}"


# ---------------------------------------------------------------- 线上 runner


class SandboxFixRunner:
    """线上用的 runner：GitHub 取代码、数据库取考卷、Docker 沙箱跑修复 Agent。

    第一次调用时才初始化。"""

    def __init__(self, settings: Settings, llm: LLMClient, db: Database) -> None:
        self.settings = settings
        self.llm = llm
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

    async def _exam(self, repo: str, issue: int) -> Any:
        from failgate.verify.store import latest_exam

        async with self.db.session() as s:
            return await latest_exam(s, repo, issue)

    async def _run(self, repo: str, issue: int, title: str, body: str, tree: Any, exam: Any,
                   budget_usd: float, *, feedback: str | None = None,
                   must_pass: list[str] | None = None,
                   initial: dict[str, str] | None = None) -> FixResult:
        from failgate.fix.agent import FixTask
        from failgate.fix.run import fix_tree
        from failgate.repro.config import PackageConfig
        from failgate.repro.package import IssueContext

        _, _, tester = self._parts()
        s = self.settings
        task = FixTask(repo=repo, number=issue, issue=IssueContext(title=title, body=body),
                       test_path=exam.test_path, test_code=exam.code, feedback=feedback,
                       must_pass=must_pass or [])
        return await fix_tree(
            self.llm, s.llm_model_large, tester,
            PackageConfig(name=exam.package, import_name=exam.module), tree, task,
            python=exam.python, version=exam.version, pytest=exam.pytest,
            max_rounds=s.fix_agent_rounds, budget_usd=budget_usd,
            artifacts_dir=Path(s.sandbox_artifacts_dir), initial_edits=initial,
        )

    async def fix(self, repo: str, issue: int, title: str, body: str,
                  budget_usd: float) -> tuple[FixOutput, FixResult | None]:
        from failgate.platforms.github_fixer import fix_branch
        from failgate.repro.source import fetch_github_tree
        from failgate.verify.report import language_of

        gh, _, _ = self._parts()
        exam = await self._exam(repo, issue)
        if exam is None:
            return FixOutput(issue=issue, reason="no_exam"), None
        info = await gh.repo(repo)
        branch = info["default_branch"]
        base_sha = (await gh.commit(repo, branch))["sha"]
        tree = await fetch_github_tree(gh, repo, base_sha)
        res = await self._run(repo, issue, title, body, tree, exam, budget_usd)
        out = _output(issue, 0, res)
        if res.passed and res.edits:
            ptitle, pbody = pr_texts(issue, title, _summary(res), exam_path=exam.test_path,
                                     exam_sha=exam.test_sha256, evidence_id=exam.evidence_id,
                                     lang=language_of(title, body))
            out.push = PushRequest(
                repo=repo, base_sha=base_sha, base_branch=branch, branch=fix_branch(issue),
                files={**res.edits, exam.test_path: exam.code},
                message=f"Fix #{issue} (FailGate fix agent)", title=ptitle, body=pbody)
        else:
            out.reason = "not_passed"
        return out, res

    async def refix(self, repo: str, pr: int, verification: dict[str, Any], round_: int,
                    budget_usd: float) -> tuple[FixOutput, FixResult | None]:
        from failgate.fix.feedback import feedback_from
        from failgate.fix.guard import GuardError, WriteGuard
        from failgate.repro.source import fetch_github_tree
        from failgate.verify.claims import parse_claims
        from failgate.verify.engine import Verification

        gh, _, _ = self._parts()
        data = await gh.pull(repo, pr)
        claims = parse_claims(data.get("title") or "", data.get("body"), repo)
        if not claims:
            return FixOutput(round=round_, reason="no_claim"), None
        issue = claims[0]
        fb = feedback_from(Verification.model_validate(verification), issue)
        if fb is None:
            return FixOutput(issue=issue, round=round_, reason="not_refuted"), None
        if not fb.actionable:
            return FixOutput(issue=issue, round=round_, reason="not_actionable",
                             feedback=fb.text), None
        exam = await self._exam(repo, issue)
        if exam is None:
            return FixOutput(issue=issue, round=round_, reason="no_exam"), None
        base_sha = verification["base_sha"]
        head_sha = data["head"]["sha"]
        tree = await fetch_github_tree(gh, repo, base_sha)
        guard = WriteGuard([exam.test_path])
        prev: dict[str, str] = {}
        for f in await gh.pull_files(repo, pr):
            if f["status"] == "removed":
                continue
            try:
                path = guard.check(f["filename"])
            except GuardError:
                continue
            content = await gh.file_at(repo, path, head_sha)
            if content is not None:
                prev[path] = content
        issue_data = await gh.issue(repo, issue)
        res = await self._run(repo, issue, issue_data.get("title") or "",
                              issue_data.get("body") or "", tree, exam, budget_usd,
                              feedback=fb.text, must_pass=fb.must_pass, initial=prev)
        out = _output(issue, round_, res)
        out.feedback, out.must_pass = fb.text, fb.must_pass
        if not (res.passed and res.edits):
            out.reason = "not_passed"
        elif res.edits == prev:
            out.reason = "no_change"
        else:
            # 上一轮改过、这一轮改回去的文件：以 base 的内容推，树才等于"base + 这一轮的改动"
            reverted = [p for p in prev if p not in res.edits]
            originals = tree.read_files(lambda p: p in set(reverted), max_bytes=2_000_000)
            files = {**{p: originals.get(p, "") for p in reverted}, **res.edits,
                     exam.test_path: exam.code}
            out.push = PushRequest(
                repo=repo, base_sha=base_sha, base_branch=data["base"]["ref"],
                branch=data["head"]["ref"], files=files,
                message=f"Revise fix for #{issue} after FailGate refutation (round {round_})",
                title=data.get("title") or "", body=data.get("body") or "")
        return out, res

    async def aclose(self) -> None:
        if self._ready is not None:
            gh, pypi, _ = self._ready
            await gh.aclose()
            await pypi.aclose()


def _summary(res: FixResult) -> str:
    last = res.attempts[-1] if res.attempts else None
    return (last.summary if last and last.summary else "").strip()[:600]


def _output(issue: int, round_: int, res: FixResult) -> FixOutput:
    return FixOutput(
        issue=issue, round=round_, attempted=True, agent_status=res.status, passed=res.passed,
        files=res.files, patch=res.patch[:20000], steps=res.steps, duration_s=res.duration_s,
        transcript_path=res.transcript_path,
    )
