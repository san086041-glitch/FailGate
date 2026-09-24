"""标准答案与人工标注。

目录约定（提交进仓库）：
    eval/datasets/<owner>__<name>/dedup_gold.json   维护者确认的重复对
    eval/datasets/<owner>__<name>/labels.json       人工复核结论（对照组里被判为重复的配对）

不提交 issue 正文：语料通过 `warden index build` 重建，回放报告里记录语料指纹以便复现。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

EVAL_ROOT = Path("eval")


def repo_slug(repo: str) -> str:
    return repo.replace("/", "__")


def dataset_dir(repo: str, root: Path = EVAL_ROOT) -> Path:
    return root / "datasets" / repo_slug(repo)


class GoldPair(BaseModel):
    duplicate: int
    original: int
    # 维护者评论的链接和作者身份，便于人工抽查标准答案本身
    evidence_url: str | None = None
    association: str = ""


class GoldSet(BaseModel):
    repo: str
    source: str = "maintainer comments: 'Duplicate of #N'"
    mined_at: datetime
    search_hits: int = 0
    pairs: list[GoldPair] = Field(default_factory=list)

    def clusters(self) -> Clusters:
        c = Clusters()
        for p in self.pairs:
            c.union(p.duplicate, p.original)
        return c


class Clusters:
    """并查集：重复关系可传递（A 是 B 的重复，B 是 C 的重复 → A、B、C 同簇）。"""

    def __init__(self) -> None:
        self._parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self._parent.setdefault(x, x)
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:  # 路径压缩
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        self._parent[self.find(a)] = self.find(b)

    def same(self, a: int, b: int) -> bool:
        return a in self._parent and b in self._parent and self.find(a) == self.find(b)

    def __contains__(self, x: object) -> bool:
        return x in self._parent

    def members(self, x: int) -> set[int]:
        if x not in self._parent:
            return set()
        root = self.find(x)
        return {n for n in self._parent if self.find(n) == root}


Label = Literal["duplicate", "not_duplicate", "ambiguous"]


class PairLabel(BaseModel):
    label: Label
    note: str = ""


class Labels(BaseModel):
    """人工复核结论，键是 "issue->候选"，例如 "2757->2662"。"""

    pairs: dict[str, PairLabel] = Field(default_factory=dict)

    def get(self, issue: int, candidate: int | None) -> Label | None:
        if candidate is None:
            return None
        found = self.pairs.get(f"{issue}->{candidate}")
        return found.label if found else None


def load_gold(repo: str, root: Path = EVAL_ROOT) -> GoldSet:
    path = dataset_dir(repo, root) / "dedup_gold.json"
    return GoldSet.model_validate_json(path.read_text("utf-8"))


def save_gold(gold: GoldSet, root: Path = EVAL_ROOT) -> Path:
    path = dataset_dir(gold.repo, root) / "dedup_gold.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(gold.model_dump_json(indent=1), "utf-8")
    return path


def load_labels(repo: str, root: Path = EVAL_ROOT) -> Labels:
    path = dataset_dir(repo, root) / "labels.json"
    if not path.exists():
        return Labels()
    return Labels.model_validate(json.loads(path.read_text("utf-8")))
