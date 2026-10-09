"""回放选题规则按仓库配置（ADR 0040）。

以前 replay repro / l2 / fixset 的选题写死了 psf/black 的标签（`T: bug`、`C: crash` 这些），
换一个仓库就选不出题。现在每个仓库在 `eval/datasets/<owner>__<name>/selection.json`
写一份规则，和数据集放在一起提交：规则本身也是评测的一部分，改了要能在 diff 里看到。

    已完成关闭、since 之后创建
      │
    至少带一个 bug_labels ─── 否 → 不选
      │
    带任何 exclude_labels ─── 是 → 不选（重复、不是 bug、上游的 bug……）
      │
    replay repro / l2 另外要求至少带一个 repro_labels（为空就不加这一条）
      │
    按编号从新到旧
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from failgate.replay.dataset import dataset_dir

FILENAME = "selection.json"


class Selection(BaseModel):
    bug_labels: list[str]  # 至少带一个才算 bug
    repro_labels: list[str] = Field(default_factory=list)  # repro / l2 额外要求；空 = 不要求
    exclude_labels: list[str] = Field(default_factory=list)
    # monorepo（ADR 0045）：上游修复必须改到这个前缀下的源码才计入严格 FB/PA，
    # 如 "libs/core/langchain_core/"；空 = 不限制
    source_prefix: str = ""
    note: str = ""  # 为什么这样选（写进报告）

    def is_bug(self, labels: Iterable[str]) -> bool:
        got = set(labels)
        return bool(got & set(self.bug_labels)) and not got & set(self.exclude_labels)

    def is_repro(self, labels: Iterable[str]) -> bool:
        got = set(labels)
        return self.is_bug(got) and (not self.repro_labels or bool(got & set(self.repro_labels)))

    def describe(self, *, repro: bool = False) -> str:
        parts = ["带 " + " / ".join(f"`{x}`" for x in self.bug_labels) + " 之一"]
        if repro and self.repro_labels:
            parts.append("类别为 " + " / ".join(f"`{x}`" for x in self.repro_labels) + " 之一")
        if self.exclude_labels:
            parts.append("排除 " + " / ".join(f"`{x}`" for x in self.exclude_labels))
        if self.source_prefix:
            parts.append(f"修复须改到 `{self.source_prefix}` 下的源码")
        return "，".join(parts)


class SelectionMissing(Exception):
    pass


def path_for(repo: str, root: Path) -> Path:
    return dataset_dir(repo, root) / FILENAME


def load(repo: str, root: Path) -> Selection:
    p = path_for(repo, root)
    if not p.exists():
        raise SelectionMissing(
            f"{repo} 没有选题规则：先写 {p}（bug_labels / repro_labels / exclude_labels，"
            f"可参考 eval/datasets/psf__black/{FILENAME}）")
    return Selection.model_validate_json(p.read_text(encoding="utf-8"))


def closed_since(d: Any, since: datetime) -> bool:
    """已完成关闭、since 之后创建。"""
    if d.state != "closed" or d.state_reason != "completed":
        return False
    return d.created_at is not None and d.created_at.replace(tzinfo=None) >= since
