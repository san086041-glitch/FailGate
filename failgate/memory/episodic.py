"""情景记忆（W9，ADR 0032）：这个仓库以前合并过的修改，给修复 Agent 当"前人怎么修的"参考。

W9 先量发现：修复 Agent 中位第 5 步就读到了该改的文件，但改到对的文件的 37 次里 15 次改法不对，
第一次动手中位在第 55 步。缺的不是"bug 在哪"，是"这段代码该怎么改"。所以记的是
维护者合并过的修改本身：

    一条情景 = 一个合并的 PR
      标题、描述、关闭的 issue（标题 + 正文开头）
      改了哪些文件；源码文件的 diff（截断）和 diff 里出现的函数名

三条规则：
- 时间旅行：只能看到 issue 创建之前合并的 PR（检索时按 before 过滤，BM25 也只在这些 PR 上建，
  连词频统计都不用到"未来"的数据）；
- 只记被验证过的：来源是维护者合并的 PR，不是 Agent 自己的猜测；
- 按需取：Agent 用 recall_fixes 工具查，每次最多几条、diff 截断，不整段塞进上下文。

检索用 BM25（failgate.index.bm25，和查重同一套分词：代码标识符整体和拆开都保留）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from failgate.index.bm25 import BM25
from failgate.index.text import tokenize

PATCH_KEEP_LINES = 60  # 存的时候每个文件 diff 最多留多少行
_HUNK = re.compile(r"^@@ [^@]* @@ ?(.*)$", re.M)
_DEF = re.compile(r"\b(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")


class IssueRef(BaseModel):
    number: int
    title: str
    body: str = ""


class FileChange(BaseModel):
    path: str
    functions: list[str] = Field(default_factory=list)
    patch: str = ""


class Episode(BaseModel):
    pr: int
    title: str
    body: str = ""
    merged_at: datetime
    commit: str | None = None
    issues: list[IssueRef] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)  # 全部改动文件
    changes: list[FileChange] = Field(default_factory=list)  # 源码文件（带 diff）

    def text(self) -> str:
        """建索引用的文本：标题、描述、issue、路径、函数名。diff 本身不进索引（噪声太多）。"""
        parts = [self.title, self.body]
        for i in self.issues:
            parts += [i.title, i.body]
        for c in self.changes:
            parts += [c.path, " ".join(c.functions)]
        return "\n".join(parts)

    def touches(self, path: str) -> bool:
        return path in self.files or any(c.path == path for c in self.changes)


def hunk_functions(patch: str) -> list[str]:
    """diff 里 @@ 行后面的上下文（git 给出的所在函数 / 类）和新增、删除行里定义的函数名。"""
    names: list[str] = []
    for ctx in _HUNK.findall(patch):
        names += _DEF.findall(ctx)
    for line in patch.splitlines():
        if line[:1] in "+-" and not line.startswith(("+++", "---")):
            names += _DEF.findall(line)
    return list(dict.fromkeys(names))


def trim_patch(patch: str, keep: int = PATCH_KEEP_LINES) -> str:
    lines = patch.splitlines()
    if len(lines) <= keep:
        return patch
    return "\n".join(lines[:keep] + [f"…（还有 {len(lines) - keep} 行）"])


class EpisodicMemory:
    def __init__(self, episodes: Iterable[Episode]) -> None:
        self.episodes = sorted(episodes, key=lambda e: e.merged_at)
        self._tokens = [tokenize(e.text()) for e in self.episodes]
        self._cache: dict[datetime, tuple[list[int], BM25]] = {}

    def __len__(self) -> int:
        return len(self.episodes)

    @classmethod
    def load(cls, path: Path) -> EpisodicMemory:
        lines = path.read_text(encoding="utf-8").splitlines()
        return cls(Episode.model_validate_json(ln) for ln in lines if ln.strip())

    @staticmethod
    def dump(episodes: Sequence[Episode], path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(e.model_dump_json() + "\n" for e in episodes), encoding="utf-8")

    def _index(self, before: datetime) -> tuple[list[int], BM25]:
        """只在 before 之前合并的 PR 上建 BM25（同一个截止时间缓存）。"""
        if before not in self._cache:
            idx = [i for i, e in enumerate(self.episodes) if e.merged_at < before]
            self._cache[before] = (idx, BM25([self._tokens[i] for i in idx]))
        return self._cache[before]

    def visible(self, before: datetime) -> int:
        return len(self._index(before)[0])

    def recall(self, query: str, *, before: datetime, path: str | None = None, k: int = 3,
               exclude_prs: Iterable[int] = ()) -> list[Episode]:
        idx, bm25 = self._index(before)
        excluded = set(exclude_prs)
        scores = bm25.scores(tokenize(query))
        ranked = sorted(range(len(idx)), key=lambda j: -scores[j])
        out: list[Episode] = []
        for j in ranked:
            e = self.episodes[idx[j]]
            if scores[j] <= 0 or e.pr in excluded:
                continue
            if path is not None and not e.touches(path):
                continue
            out.append(e)
            if len(out) >= k:
                break
        return out


def render(episodes: Sequence[Episode], *, patch_lines: int = 40, max_changes: int = 2,
           with_patch: bool = True) -> str:
    """给 Agent 看的样子。不带 diff 时只列标题、关联 issue、改了哪些函数（用在任务开头）。"""
    if not episodes:
        return "（没有找到相关的历史修改）"
    blocks = []
    for e in episodes:
        head = f"### PR #{e.pr}（{e.merged_at:%Y-%m-%d} 合并）：{e.title}"
        lines = [head]
        for i in e.issues[:2]:
            lines.append(f"- 关闭的 issue #{i.number}：{i.title}")
        for c in e.changes[:max_changes]:
            fn = f"（{', '.join(c.functions[:6])}）" if c.functions else ""
            lines.append(f"- 改了 `{c.path}`{fn}")
            if with_patch and c.patch:
                lines.append("```diff\n" + trim_patch(c.patch, patch_lines) + "\n```")
        if len(e.changes) > max_changes:
            lines.append(f"- 另外还改了 {len(e.changes) - max_changes} 个源码文件")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
