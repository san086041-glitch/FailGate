"""FailGate 的本地 stdio MCP 服务（ADR 0033）。

推荐用法（写在 instructions 里给客户端的 Agent 看）：
    reproduce_issue → get_job（拿到 evidence_id 和失败测试）→ 修代码
    → run_acceptance_test（可选，快速自查）→ verify_fix（三层核验）→ get_job

出题、跑考卷、核验都要几分钟，工具立刻返回 job_id；get_job 可以最多等 wait_seconds 秒。
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from failgate.mcp_server.engine import Engine
from failgate.mcp_server.store import EvidenceStore, Job, JobManager, LocalEvidence

MAX_WAIT_S = 50  # 不要超过客户端常见的读超时
INSTRUCTIONS = """FailGate verifies bug fixes against a sealed failing test ("exam").
Typical flow for a bug in a local Python git repository:
1. reproduce_issue(repo_path, title, body, package) -> job_id; poll get_job(job_id, wait_seconds=50)
   until status is done. The result has evidence_id and test_code (a pytest test that fails now).
2. Fix the code. Do NOT edit the sealed test; FailGate checks its sha256.
3. Optionally run_acceptance_test(repo_path, evidence_id) to check the exam on the current worktree.
4. verify_fix(repo_path, evidence_id) runs three layers: exam fails before / passes after,
   tamper checks on tests and pytest config, and related tests for regressions.
   VERIFIED means "passes the acceptance test, no tampering, no new regressions",
   not "proven correct".
get_fix_task(evidence_id) returns a task card you can hand to another agent or a human.
All code runs in a Docker sandbox; nothing is written into the repository."""


def _job_reply(job: Job) -> dict[str, Any]:
    return {"job_id": job.job_id, "status": job.status,
            "next": f"call get_job(job_id='{job.job_id}', wait_seconds={MAX_WAIT_S})"}


def _evidence_summary(ev: LocalEvidence) -> dict[str, Any]:
    r = ev.receipt
    return {"evidence_id": ev.evidence_id, "title": ev.title, "repo_path": ev.repo_path,
            "test_path": r["test_path"], "level": r["level"], "verdict": r["verdict"],
            "created_at": ev.created_at}


def fix_task(ev: LocalEvidence) -> str:
    """给外部 Agent 或人的修复任务单（Markdown）。"""
    r = ev.receipt
    path = r["test_path"]
    return "\n".join([
        f"# 修复任务：{ev.title}",
        "",
        "## issue",
        ev.body.strip() or "（无正文）",
        "",
        "## 验收测试（封存的考卷）",
        f"- 路径：`{path}`（相对仓库根）；在出题时的代码上失败",
        f"- sha256：`{r['test_sha256']}`；证据编号：`{ev.evidence_id}`",
        f"- 本地运行：`python -m pytest {path}`（先把下面的代码存到这个路径）",
        "",
        "```python",
        ev.code.rstrip(),
        "```",
        "",
        "## 规则",
        "- 修改被测源码，让验收测试通过；**不要修改这份测试**，也不要改 conftest、pytest 配置"
        "或加 skip / xfail：核验会比对哈希和这些文件。",
        "- 不要写只对这个输入有效的特判：验收测试只是判断标准的一部分，核验还会跑相关的已有测试。",
        f"- 改完调用 FailGate 的 `verify_fix(repo_path, evidence_id='{ev.evidence_id}')`，"
        "结论为 VERIFIED 才算通过验收。",
    ])


def build_server(engine: Engine, store: EvidenceStore,
                 jobs: JobManager | None = None) -> MCPServer:
    jobs = jobs or JobManager()
    server = MCPServer(name="failgate", title="FailGate", instructions=INSTRUCTIONS)

    def _get(evidence_id: str) -> LocalEvidence:
        """找不到、编号不唯一、文件被改过：ToolError 的消息会原样交给客户端。"""
        try:
            ev = store.get(evidence_id)
        except KeyError as e:
            raise ToolError(str(e.args[0] if e.args else e)) from e
        if problems := store.problems(ev):
            raise ToolError("证据文件和收据对不上，不能用：" + "；".join(problems))
        return ev

    @server.tool()
    async def reproduce_issue(repo_path: str, title: str, body: str, package: str,
                              import_name: str | None = None,
                              python: str | None = None,
                              subdir: str | None = None) -> dict[str, Any]:
        """Write a failing pytest test for a bug in a local git repo and seal it as the exam.

        repo_path: the repository (any directory inside it). title/body: the bug report;
        include the traceback and expected vs actual behaviour if you have them.
        package: the distribution name to install from the repo (e.g. "black").
        subdir: for a monorepo, the directory holding that package (e.g. "libs/core").
        Calls an LLM (about $0.01-0.05) and takes a few minutes; returns a job_id.
        """
        job = jobs.start("reproduce", lambda p: engine.reproduce(
            repo_path, title, body, package, import_name, python, p, subdir=subdir))
        return _job_reply(job)

    @server.tool()
    async def run_acceptance_test(repo_path: str, evidence_id: str) -> dict[str, Any]:
        """Run the sealed exam on the current worktree (including uncommitted changes).

        A quick self-check while fixing; it is not a verification. Returns a job_id.
        """
        ev = _get(evidence_id)
        job = jobs.start("acceptance", lambda p: engine.acceptance(repo_path, ev, p))
        return _job_reply(job)

    @server.tool()
    async def verify_fix(repo_path: str, evidence_id: str, base_ref: str | None = None,
                         strength: bool = False) -> dict[str, Any]:
        """Verify the current worktree against the sealed exam (FailGate ClaimVerify).

        Compares base_ref (default: the commit the exam was sealed on) with the current
        worktree: exam fails on base and passes on the worktree, no tampering with the test
        or pytest config, no new failures in related tests. strength=True also runs mutation
        testing on the changed lines (slower). Returns a job_id.
        """
        ev = _get(evidence_id)
        job = jobs.start("verify", lambda p: engine.verify(repo_path, ev, base_ref, strength, p))
        return _job_reply(job)

    @server.tool()
    async def get_job(job_id: str, wait_seconds: float = 0) -> dict[str, Any]:
        """Status, progress and result of a job. wait_seconds (max 50) waits for it to finish."""
        try:
            job = await jobs.wait(job_id, max(0.0, min(float(wait_seconds), MAX_WAIT_S)))
        except KeyError as e:
            raise ToolError(str(e.args[0])) from e
        return job.view()

    @server.tool()
    async def get_fix_task(evidence_id: str) -> str:
        """A Markdown task card for fixing the bug: issue, the sealed test, rules, how to verify."""
        return fix_task(_get(evidence_id))

    @server.tool()
    async def list_evidence(repo_path: str | None = None) -> list[dict[str, Any]]:
        """Sealed exams on this machine, newest first; repo_path filters by repository root."""
        return [_evidence_summary(e) for e in store.all(repo_path)]

    @server.resource("failgate://evidence/{evidence_id}")
    def evidence_resource(evidence_id: str) -> str:
        """The signed receipt and the full test code of one sealed exam."""
        ev = _get(evidence_id)
        return json.dumps({"receipt": ev.receipt, "test_code": ev.code}, ensure_ascii=False,
                          indent=1)

    return server
