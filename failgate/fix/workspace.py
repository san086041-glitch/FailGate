"""修复 Agent 的工作区：宿主机上的改动清单是唯一的"真相"。

    源码树（SourceTree，宿主机内存里）  ← 原文件内容从这里读
        │ edit_file 只改宿主机上的 edits 字典（先过 WriteGuard）
        ▼
    sync：把变了的文件复制进沙箱工作区卷 /workspace/src，Agent 读到的、跑测试用到的都是它
        ▼
    verify_acceptance：**全新**工作区 = 源码副本 + edits + 封存的考卷，再跑一次

为什么最终验收要另开工作区：run 阶段的工作区是可写的（ADR 0008 的已知局限），Agent 在沙箱里
运行的脚本理论上能直接改工作区里的文件。补丁只取 edits，验收只用全新工作区，
所以这类"绕过工具层"的改动既进不了补丁，也过不了验收。
"""

from __future__ import annotations

import asyncio
import difflib
import re
import tempfile
from pathlib import Path
from typing import Any

from failgate.fix.guard import GuardError, WriteGuard
from failgate.repro.codetools import CodeTools
from failgate.repro.l2 import (
    RUN_PREFIXES,
    WORK_SRC,
    SourcePrepared,
    TestReproducer,
    pytest_argv,
)
from failgate.repro.sandbox import ExecResult

SCRATCH_FILE = ".failgate/snippet.py"
TEST_TARGET = re.compile(r"[\w./-]+(?:::[\w\[\]./,=-]+)*")
FIX_PYTEST_ARGS = ["-q", "--tb=short", "-p", "no:cacheprovider", "--rootdir=src", "--maxfail=5"]
RUN_TIMEOUT_S = 120


def unified(path: str, old: str | None, new: str) -> str:
    return "".join(difflib.unified_diff(
        [] if old is None else old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile="/dev/null" if old is None else f"a/{path}", tofile=f"b/{path}",
    ))


def workspace_pythonpath(prepared: SourcePrepared) -> str:
    """让测试导入工作区里的源码副本（遮住 site-packages 里的安装版）；src 布局多一层 /src。"""
    module = prepared.cfg.module
    src_layout = bool(prepared.tree.read_files(lambda p: p.startswith(f"src/{module}/"), limit=1))
    return f"/workspace/{WORK_SRC}" + ("/src" if src_layout else "")


def count_changed(old: str | None, new: str) -> int:
    n = 0
    for ln in difflib.unified_diff([] if old is None else old.splitlines(), new.splitlines(),
                                   lineterm="", n=0):
        if ln[:1] in "+-" and not ln.startswith(("+++", "---")):
            n += 1
    return n


class FixWorkspace:
    def __init__(self, tester: TestReproducer, prepared: SourcePrepared, guard: WriteGuard,
                 *, exam_code: str | None = None) -> None:
        self.tester = tester
        self.prepared = prepared
        self.guard = guard
        self.exam_code = exam_code  # 封存的考卷（对照组为 None）
        self.volume = ""
        self.edits: dict[str, str] = {}
        self._synced: dict[str, str] = {}
        self._originals: dict[str, str | None] = {}
        self.pythonpath = workspace_pythonpath(prepared)

    # ---------------------------------------------------------------- 生命周期

    async def open(self, key: str) -> None:
        self.volume = await self.tester.open_workspace(self.prepared, key)
        if self.exam_code is not None:
            await self.tester.write_test(self.volume, self.prepared, self.exam_code)

    async def close(self) -> None:
        if self.volume:
            await self.tester.sandbox.remove_workspace(self.volume)
            self.volume = ""

    # ---------------------------------------------------------------- 读

    def original(self, path: str) -> str | None:
        """这个提交上的原文件内容；不存在为 None。"""
        if path not in self._originals:
            found = self.prepared.tree.read_files(lambda p: p == path, max_bytes=2_000_000,
                                                  limit=1)
            self._originals[path] = found.get(path)
        return self._originals[path]

    def current(self, path: str) -> str | None:
        return self.edits[path] if path in self.edits else self.original(path)

    def _tools(self) -> CodeTools:
        return CodeTools(self.tester.sandbox, self.prepared.env.image, self.volume,
                         f"/workspace/{WORK_SRC}")

    async def list_files(self, path: str = "") -> dict[str, Any]:
        await self.sync()
        return await self._tools().list_files(path)

    async def search(self, pattern: str, path: str = "") -> dict[str, Any]:
        await self.sync()
        return await self._tools().search(pattern, path)

    async def read(self, path: str, start: int = 1, end: int | None = None) -> dict[str, Any]:
        await self.sync()
        return await self._tools().read(path, start, end)

    # ---------------------------------------------------------------- 写（先过 guard）

    def edit(self, path: str, old: str, new: str) -> str:
        """字符串替换式编辑；old 为空表示新建文件。返回给模型的说明，失败抛 GuardError。"""
        norm = self.guard.check(path)
        cur = self.current(norm)
        if cur is None:
            if old:
                raise GuardError(f"{norm} 不存在。新建文件时 old 传空字符串。")
            updated = new
        else:
            if not old:
                raise GuardError(f"{norm} 已存在，old 不能为空：写出要替换的原文片段。")
            n = cur.count(old)
            if n == 0:
                raise GuardError(f"在 {norm} 里没有找到 old 片段。先 read_file 确认原文"
                                 "（缩进、空格要一字不差）。")
            if n > 1:
                raise GuardError(f"old 片段在 {norm} 里出现了 {n} 次，请带上更多上下文使它唯一。")
            updated = cur.replace(old, new, 1)
        self.guard.check_size(norm, updated)
        trial = {**self.edits, norm: updated}
        files, lines = self._totals(trial)
        self.guard.check_totals(files, lines)
        self.edits = trial
        return f"已修改 {norm}（累计改动 {files} 个文件、{lines} 行）"

    async def revert(self) -> None:
        """撤销所有改动：宿主机清单清空，工作区里被改过的文件写回原文（新建的文件写成空文件）。"""
        touched = set(self.edits) | set(self._synced)
        self.edits = {}
        restore = {p: (self.original(p) or "") for p in touched}
        if restore:
            with tempfile.TemporaryDirectory() as tmp:
                await asyncio.to_thread(self._write_tree, Path(tmp), restore)
                await self.tester.sandbox.copy_in(self.volume, Path(tmp), self.prepared.env.image)
        self._synced = {}

    def _totals(self, edits: dict[str, str]) -> tuple[int, int]:
        files = lines = 0
        for p, new in edits.items():
            old = self.original(p)
            if new == old:
                continue
            files += 1
            lines += count_changed(old, new)
        return files, lines

    def patch(self) -> str:
        return "".join(unified(p, self.original(p), new)
                       for p, new in sorted(self.edits.items()) if new != self.original(p))

    def changed_files(self) -> list[str]:
        return sorted(p for p, new in self.edits.items() if new != self.original(p))

    # ---------------------------------------------------------------- 沙箱里执行

    async def sync(self) -> None:
        """把有变化的改动复制进工作区卷。"""
        todo = {p: c for p, c in self.edits.items() if self._synced.get(p) != c}
        if not todo:
            return
        with tempfile.TemporaryDirectory() as tmp:
            await asyncio.to_thread(self._write_tree, Path(tmp), todo)
            await self.tester.sandbox.copy_in(self.volume, Path(tmp), self.prepared.env.image)
        self._synced.update(todo)

    @staticmethod
    def _write_tree(root: Path, files: dict[str, str]) -> None:
        for p, c in files.items():
            dest = root / WORK_SRC / p
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(c, encoding="utf-8")

    def _env(self) -> list[str]:
        return [f"PYTHONPATH={self.pythonpath}"]

    async def run_tests(self, targets: list[str]) -> ExecResult:
        """按仓库自己的 pytest 配置跑指定的测试文件 / 节点。只接受路径，不接受任何选项。"""
        argv_paths: list[str] = []
        for t in targets:
            file_part = t.split("::", 1)[0]
            if (not TEST_TARGET.fullmatch(t) or t.startswith("-") or ".." in file_part.split("/")
                    or file_part.startswith("/")):
                raise GuardError(f"测试目标不合法：{t!r}（只接受相对仓库根的路径或 路径::节点）。")
            argv_paths.append(f"{WORK_SRC}/{t}")
        if not argv_paths:
            raise GuardError("至少给一个测试目标。")
        await self.sync()
        return await self.tester.sandbox.run(
            self.prepared.env.image, self.volume,
            ["python", "-m", "pytest", *argv_paths, *FIX_PYTEST_ARGS],
            timeout_s=RUN_TIMEOUT_S, allowed=RUN_PREFIXES, env=self._env(),
        )

    async def run_python(self, code: str) -> ExecResult:
        """写一个临时脚本并运行，观察代码的实际行为（不是交付物，不进补丁）。"""
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp, SCRATCH_FILE)
            await asyncio.to_thread(dest.parent.mkdir, parents=True)
            await asyncio.to_thread(dest.write_text, code, encoding="utf-8")
            await self.tester.sandbox.copy_in(self.volume, Path(tmp), self.prepared.env.image)
        await self.sync()
        return await self.tester.sandbox.run(
            self.prepared.env.image, self.volume, ["python", SCRATCH_FILE], timeout_s=60,
            env=self._env(),
        )

    async def verify_acceptance(self) -> ExecResult:
        """全新工作区 = 源码副本 + 改动清单 + 封存的考卷，跑一次考卷。"""
        assert self.exam_code is not None
        p = self.prepared
        ws = await self.tester.open_workspace(p, f"fixverify-{p.env.key[:8]}")
        try:
            if self.edits:
                with tempfile.TemporaryDirectory() as tmp:
                    await asyncio.to_thread(self._write_tree, Path(tmp), self.edits)
                    await self.tester.sandbox.copy_in(ws, Path(tmp), p.env.image)
            await self.tester.write_test(ws, p, self.exam_code)
            return await self.tester.sandbox.run(
                p.env.image, ws, [*pytest_argv(p.test_path), "-rA"],
                timeout_s=RUN_TIMEOUT_S, allowed=RUN_PREFIXES, env=self._env(),
            )
        finally:
            await self.tester.sandbox.remove_workspace(ws)
