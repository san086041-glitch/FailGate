"""MCP 工具背后的引擎：出题、跑考卷、核验，全部复用服务端的同一套代码（ADR 0033）。

- 出题：本地工作区打成源码包 → Intake → reproduce_tree_l2（和 fixture、流水线同一个入口）→
  seal 成收据，存进本地证据库；
- 跑考卷：当前工作区 + 封存的考卷，在全新沙箱工作区里跑（SandboxWorkbench.run_exam）；
- 核验：base = 出题时的提交（或指定的 ref），head = 当前工作区（含未提交的修改），
  改动清单由两个源码包对比得出，交给 ClaimVerifier 三层核验（和 PR 核验同一个类）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from failgate.mcp_server import local
from failgate.mcp_server.store import EvidenceStore, LocalEvidence, Progress, now_iso
from failgate.repro.config import PackageConfig
from failgate.repro.source import SourceTree
from failgate.settings import Settings
from failgate.verify.engine import ClaimVerifier, PullRequest, classify_head
from failgate.verify.report import render_verification
from failgate.verify.workbench import SandboxWorkbench

LOCAL_ISSUE = 0  # 本地出题没有 issue 编号；收据里记 0


class Engine(Protocol):
    async def reproduce(self, repo_path: str, title: str, body: str, package: str,
                        import_name: str | None, python: str | None,
                        progress: Progress) -> dict[str, Any]: ...

    async def acceptance(self, repo_path: str, ev: LocalEvidence,
                         progress: Progress) -> dict[str, Any]: ...

    async def verify(self, repo_path: str, ev: LocalEvidence, base_ref: str | None,
                     strength: bool, progress: Progress) -> dict[str, Any]: ...


def _label(root: Path) -> str:
    return f"local/{root.name}"


@dataclass
class Runtime:
    """出题要 LLM，跑考卷和核验只要沙箱。"""

    settings: Settings
    llm: Any
    tester: Any

    @classmethod
    def from_settings(cls, settings: Settings) -> Runtime:
        from failgate.app import build_llm
        from failgate.cli import _env_cache, build_sandbox
        from failgate.repro.l2 import TestReproducer
        from failgate.repro.pypi import PyPIClient

        sandbox = build_sandbox(settings)
        tester = TestReproducer(sandbox, _env_cache(settings, sandbox),
                                PyPIClient(settings.pypi_url),
                                run_timeout_s=settings.sandbox_run_timeout_seconds)
        return cls(settings=settings, llm=build_llm(settings), tester=tester)


class FailGateEngine:
    def __init__(self, rt: Runtime, store: EvidenceStore) -> None:
        self.rt = rt
        self.store = store

    def _bench(self, trees: dict[str, SourceTree]) -> SandboxWorkbench:
        async def fetch(_repo: str, sha: str) -> SourceTree:
            return trees[sha]

        return SandboxWorkbench(fetch, self.rt.tester)

    async def reproduce(self, repo_path: str, title: str, body: str, package: str,
                        import_name: str | None, python: str | None,
                        progress: Progress) -> dict[str, Any]:
        from failgate.repro.issue import new_l2_report, reproduce_tree_l2
        from failgate.skills.base import IssueSnapshot, SkillContext
        from failgate.skills.intake import IntakeOutput, IntakeSkill
        from failgate.skills.repro import seal

        if self.rt.llm is None:
            raise RuntimeError("没有配置 LLM_API_KEY：出题需要 LLM（跑考卷和核验不需要）")
        s = self.rt.settings
        root = local.repo_root(repo_path)
        label = _label(root)
        base_sha, _ = local.resolve(root, "HEAD")
        dirty = local.is_dirty(root)
        tree = local.worktree_tree(root, label)
        progress(f"工作区打包完成（{len(tree.tarball) // 1024} KB，"
                 f"{'有' if dirty else '没有'}未提交的修改）")
        intake_res = await IntakeSkill().run(SkillContext(
            issue=IssueSnapshot(repo=label, number=LOCAL_ISSUE, title=title, body=body),
            llm=self.rt.llm, model=s.llm_model_small))
        intake = intake_res.output
        assert isinstance(intake, IntakeOutput)
        progress("Intake 完成，开始写失败测试（几分钟）")
        cfg = PackageConfig(name=package, import_name=import_name)
        report = new_l2_report(label, LOCAL_ISSUE, title, body, intake, cfg, label)
        report.intake_cost_usd = intake_res.cost_usd
        report = await reproduce_tree_l2(
            report, tree, title=title, body=body, created_at=None, intake=intake, cfg=cfg,
            llm=self.rt.llm, model=s.llm_model_large, tester=self.rt.tester,
            max_steps=s.repro_max_steps, max_attempts=s.repro_max_attempts,
            budget_usd=s.repro_budget_usd, artifacts_dir=Path(s.sandbox_artifacts_dir),
            python=python, judge_model=s.llm_model_judge or None)
        a = report.agent
        cost = round(report.intake_cost_usd + report.judge_cost_usd
                     + (a.cost_usd if a else 0.0), 6)
        sealed = seal(report)
        if sealed is None:
            return {"reproduced": False, "level": report.source.level.value,
                    "agent_status": a.status if a else None,
                    "reason": (a.give_up_reason or a.error) if a else report.source.error,
                    "cost_usd": cost}
        signed = sealed.signed()
        ev = LocalEvidence(
            evidence_id=signed["evidence_id"], repo_path=str(root), base_sha=base_sha,
            dirty=dirty, title=title, body=body, import_name=import_name, receipt=signed,
            code=sealed.code, created_at=now_iso())
        self.store.save(ev)
        progress(f"已封存考卷 {ev.evidence_id[:12]}")
        return {"reproduced": True, "evidence_id": ev.evidence_id,
                "level": signed["level"], "verdict": signed["verdict"],
                "test_path": signed["test_path"], "test_sha256": signed["test_sha256"],
                "receipt_sha256": signed["receipt_sha256"], "python": signed.get("python"),
                "test_code": sealed.code, "dirty_worktree": dirty, "cost_usd": cost}

    async def acceptance(self, repo_path: str, ev: LocalEvidence,
                         progress: Progress) -> dict[str, Any]:
        exam = ev.exam()
        root = local.repo_root(repo_path)
        head = local.worktree_tree(root, _label(root))
        bench = self._bench({head.sha: head})
        progress("准备当前工作区的环境")
        prepared = await bench.prepare(head.repo, head.sha, exam)
        res = await bench.run_exam(prepared, exam)
        run = classify_head(res, exam)
        return {"passed": run.outcome == "passed", "outcome": run.outcome,
                "exit_code": res.exit_code, "output_tail": res.output_tail(40),
                "worktree": head.sha}

    async def verify(self, repo_path: str, ev: LocalEvidence, base_ref: str | None,
                     strength: bool, progress: Progress) -> dict[str, Any]:
        exam = ev.exam()
        root = local.repo_root(repo_path)
        label = _label(root)
        base = local.ref_tree(root, base_ref or ev.base_sha, label)
        head = local.worktree_tree(root, label)
        files = local.tree_diff(base, head)
        progress(f"base {base.sha[:10]} → 当前工作区：改了 {len(files)} 个文件")
        pr = PullRequest(repo=label, number=0, title="local changes", base_sha=base.sha,
                         head_sha=head.sha, head_repo=label, files=files)
        verifier = ClaimVerifier(self._bench({base.sha: base, head.sha: head}),
                                 strength=strength)
        v = await verifier.verify(pr, [exam.issue], {exam.issue: exam})
        claim = v.claims[0]
        warnings = []
        if ev.dirty and base_ref is None:
            warnings.append("出题时工作区有未提交的修改，base 用的是当时的 HEAD 提交，"
                            "不含那些修改；需要时用 base_ref 指定")
        # 报告模板是给 PR 用的：复验命令换成本地的做法
        report = render_verification(v, "zh").replace(
            f"本地复验：`failgate verify {label}#0`",
            f"复验：再调用 MCP 工具 `verify_fix`（evidence_id `{ev.evidence_id}`）")
        return {"verdict": str(claim.verdict), "reasons": claim.reasons,
                "changed_files": local.changed_paths(files), "warnings": warnings,
                "report_markdown": report, "verification": v.model_dump(mode="json")}
