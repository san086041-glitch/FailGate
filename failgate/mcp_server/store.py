"""MCP 本地状态：封存的考卷（证据）和长任务（ADR 0033）。

证据存在 FailGate 自己的目录（默认 ~/.failgate/evidence，FAILGATE_HOME 可改），
不写进用户的仓库：测试文件作为结果返回，要不要保存由用户决定。
文件写入后不再修改（只加不改，和服务端 evidence 表同一个约定），收据哈希可以随时核对。
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from failgate.verify.engine import Exam
from failgate.verify.receipt import check_receipt
from failgate.verify.store import exam_from_receipt


def default_home() -> Path:
    return Path(os.environ.get("FAILGATE_HOME") or Path.home() / ".failgate")


class LocalEvidence(BaseModel):
    """一份封存的考卷：收据 + 代码 + 出题时的上下文。"""

    evidence_id: str
    repo_path: str  # 出题时的仓库根目录
    base_sha: str  # 出题时 HEAD 的提交（核验的默认 base）
    dirty: bool  # 出题时工作区有没有未提交的修改
    title: str
    body: str
    import_name: str | None = None
    receipt: dict[str, Any]
    code: str
    created_at: str

    def exam(self) -> Exam:
        return exam_from_receipt(self.receipt, self.code, self.import_name)


class EvidenceStore:
    def __init__(self, home: Path | None = None) -> None:
        self.dir = (home or default_home()) / "evidence"

    def _path(self, evidence_id: str) -> Path:
        if not evidence_id or any(c not in "0123456789abcdef" for c in evidence_id):
            raise KeyError(f"不是合法的证据编号：{evidence_id!r}")
        return self.dir / f"{evidence_id}.json"

    def save(self, ev: LocalEvidence) -> None:
        path = self._path(ev.evidence_id)
        if path.exists():
            raise FileExistsError(f"证据 {ev.evidence_id} 已存在（只加不改）")
        self.dir.mkdir(parents=True, exist_ok=True)
        path.write_text(ev.model_dump_json(indent=1), encoding="utf-8")

    def get(self, evidence_id: str) -> LocalEvidence:
        """支持编号前缀（至少 8 位），和 failgate evidence show 一样。"""
        if len(evidence_id) < 8:
            raise KeyError("证据编号至少要给前 8 位")
        exact = self._path(evidence_id)
        if exact.exists():
            return LocalEvidence.model_validate_json(exact.read_text(encoding="utf-8"))
        hits = sorted(self.dir.glob(f"{evidence_id}*.json")) if self.dir.exists() else []
        if len(hits) != 1:
            raise KeyError(f"找不到证据 {evidence_id}" if not hits
                           else f"证据编号 {evidence_id} 不唯一，请多给几位")
        return LocalEvidence.model_validate_json(hits[0].read_text(encoding="utf-8"))

    def all(self, repo_path: str | None = None) -> list[LocalEvidence]:
        if not self.dir.exists():
            return []
        out = [LocalEvidence.model_validate_json(p.read_text(encoding="utf-8"))
               for p in self.dir.glob("*.json")]
        if repo_path is not None:
            want = str(Path(repo_path).resolve()).lower()
            out = [e for e in out if str(Path(e.repo_path).resolve()).lower() == want]
        return sorted(out, key=lambda e: e.created_at, reverse=True)

    @staticmethod
    def problems(ev: LocalEvidence) -> list[str]:
        """收据和代码对不上（文件被改过）时返回问题列表。"""
        return check_receipt(ev.receipt, ev.code)


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------- 长任务

JobStatus = Literal["running", "done", "failed"]
Progress = Callable[[str], None]


@dataclass
class Job:
    job_id: str
    kind: str
    status: JobStatus = "running"
    progress: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    task: asyncio.Task[None] | None = None

    def view(self) -> dict[str, Any]:
        end = self.finished or time.monotonic()
        return {"job_id": self.job_id, "kind": self.kind, "status": self.status,
                "elapsed_s": round(end - self.started, 1), "progress": self.progress[-10:],
                "result": self.result, "error": self.error}


class JobManager:
    """复现、核验要几分钟：工具立刻返回 job_id，结果用 get_job 取（可以等最多 N 秒）。

    任务只在这个 MCP 进程里，进程退出就没了；结果里的证据已经落盘，不会丢。"""

    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}

    def start(self, kind: str, run: Callable[[Progress], Awaitable[dict[str, Any]]]) -> Job:
        job = Job(job_id=uuid.uuid4().hex[:12], kind=kind)

        def progress(msg: str) -> None:
            job.progress.append(msg)

        async def wrapper() -> None:
            try:
                job.result = await run(progress)
                job.status = "done"
            except Exception as e:  # 任务失败要落到 get_job 里，不能静默消失
                job.error = f"{type(e).__name__}: {str(e)[:500]}"
                job.status = "failed"
            finally:
                job.finished = time.monotonic()

        job.task = asyncio.create_task(wrapper())
        self.jobs[job.job_id] = job
        return job

    async def wait(self, job_id: str, timeout_s: float) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(f"没有任务 {job_id}（MCP 进程重启后任务会丢失，证据不会）")
        if job.task is not None and timeout_s > 0 and not job.task.done():
            await asyncio.wait({job.task}, timeout=timeout_s)
        return job
