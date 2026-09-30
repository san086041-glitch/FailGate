"""把修复 Agent 接到源码树上：准备环境（含预检）→ 工作区 → 图 → 补丁。

CLI（failgate fix run）、离线回放（W7–8 的提升实验）和测试共用这一个入口。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from pathlib import Path

from failgate.fix.agent import FixAgent, FixResult, FixTask
from failgate.fix.guard import WriteGuard
from failgate.fix.workspace import FixWorkspace
from failgate.llm import LLMClient
from failgate.repro.config import PackageConfig
from failgate.repro.l2 import TestReproducer
from failgate.repro.source import SourceTree


async def fix_tree(
    llm: LLMClient,
    model: str,
    tester: TestReproducer,
    cfg: PackageConfig,
    tree: SourceTree,
    task: FixTask,
    *,
    python: str | None = None,
    version: str | None = None,
    pytest: str | None = None,
    extra_protected: Sequence[str] = (),
    max_rounds: int = 3,
    plan_steps: int = 20,
    edit_steps: int = 40,
    budget_usd: float = 0.5,
    thinking: str | None = None,
    artifacts_dir: Path | None = None,
) -> FixResult:
    """在 tree（修复前的代码）上修复。task.test_code 为 None 就是对照组（没有考卷）。"""
    prepared = await tester.prepare(cfg, tree, number=task.number, python=python,
                                    version=version, pytest=pytest)
    if task.test_path:
        prepared = dataclasses.replace(prepared, test_path=task.test_path)
    guard = WriteGuard([prepared.test_path, *extra_protected])
    ws = FixWorkspace(tester, prepared, guard, exam_code=task.test_code)
    await ws.open(f"fix-{prepared.env.key[:8]}")
    try:
        agent = FixAgent(
            llm, model, ws, task, max_rounds=max_rounds, plan_steps=plan_steps,
            edit_steps=edit_steps, budget_usd=budget_usd, thinking=thinking,
            artifacts_dir=artifacts_dir,
        )
        return await agent.run()
    finally:
        await ws.close()
