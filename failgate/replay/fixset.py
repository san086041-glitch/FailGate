"""修复评测集 v2（W9，ADR 0031）：给记忆实验用的一批按时间排序的真实 bug。

旧的修复实验题（ADR 0028）来自 L2 留出集：只收"崩溃类"、要先复现成功，只剩 8 题，
而且 W9 先量用它们的 transcript 做过分析，已经算开发集。新集合换一套规则：

    回放库里的 issue：已完成关闭、符合仓库的选题规则（selection.json）、创建于 since 之后、
    不在排除名单里
      │ 按编号从新到旧
      ▼
    找修复提交（GraphQL ClosedEvent.closer）──没有 → 跳过
      │
    上游改动：要有源码改动、要有测试改动、不能改依赖 ──否则跳过（记原因）
      │
    在父提交的环境里算金标准（ADR 0028 的 F2P / 子集判定）──F2P 为空 / 跑不起来 → 跳过
      │
    收下，直到凑够 target 题

不需要我们的考卷：记忆实验比的是"有 / 没有记忆"，两组都不给考卷（提升实验里给不给考卷
修好率一样，ADR 0028），所以也不依赖复现成功，不会因为复现难度筛掉题。

整个过程不调 LLM。结果是一个 JSON：收下的题（含环境、金标准、上游测试文件）和跳过的原因。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from failgate.replay.fix_eval import Gold, deps_changed, is_test_change
from failgate.replay.fixes import FixCommit
from failgate.replay.selection import Selection, closed_since
from failgate.replay.verify_eval import EvalCase
from failgate.verify.engine import Exam
from failgate.verify.tamper import PullFile

KIND = "failgate.fixset/v1"
DOC_FILES = ("CHANGES.md", "AUTHORS.md", "README.md")
# 文档、更新日志片段、CI 配置：不算源码改动（pylint 用 doc/whatsnew/fragments/，
# 很多项目用 changelog.d/ 或 news/；ADR 0040 补充）
DOC_DIRS = ("docs/", "doc/", ".github/", "changelog.d/", "news/")
DOC_SUFFIXES = (".md", ".rst")


class FixsetCase(BaseModel):
    number: int
    title: str
    created_at: datetime
    labels: list[str] = Field(default_factory=list)
    fix: FixCommit
    src_files: list[str]  # 上游修复改的非测试文件（分析用，不给 Agent 看）
    package: str
    module: str
    python: str
    version: str
    pytest: str
    test_path: str  # 我们约定的考卷路径；这里没有考卷，只用来让 WriteGuard 保护它
    gold: Gold
    test_overlay: dict[str, str]


class Skipped(BaseModel):
    number: int
    reason: str


class Fixset(BaseModel):
    kind: str = KIND
    repo: str
    since: str
    target: int
    exclude: list[int] = Field(default_factory=list)
    rule: Selection | None = None  # 生成时的选题规则；旧文件没有（当时是写死的 black 规则）
    started: str = ""
    cases: list[FixsetCase] = Field(default_factory=list)
    skipped: list[Skipped] = Field(default_factory=list)

    def seen(self) -> set[int]:
        return {c.number for c in self.cases} | {s.number for s in self.skipped}


def candidates(
    docs: Iterable[Any], *, rule: Selection, since: datetime, exclude: set[int]
) -> list[Any]:
    """已完成关闭、符合仓库选题规则（rule.is_bug）、since 之后创建、不在排除名单里；
    按编号从新到旧。"""
    return [d for d in sorted(docs, key=lambda d: d.number, reverse=True)
            if d.number not in exclude and closed_since(d, since) and rule.is_bug(d.labels or [])]


def source_changes(files: Sequence[PullFile], test_dir: str) -> list[str]:
    """上游修复改动的非测试文件，去掉更新日志、作者名单、文档目录这类不是代码的改动。"""
    return [f.filename for f in files
            if not is_test_change(f.filename, test_dir)
            and f.filename not in DOC_FILES
            and not f.filename.startswith(DOC_DIRS)
            and not f.filename.endswith(DOC_SUFFIXES)]


def skip_reason(files: Sequence[PullFile], test_dir: str) -> str | None:
    """在跑金标准之前就能判断的排除理由。"""
    if not any(is_test_change(f.filename, test_dir) for f in files):
        return "no_test_change"
    if not source_changes(files, test_dir):
        return "no_source_change"
    if deps := deps_changed(files):
        return "deps_changed:" + ",".join(deps)
    return None


def exclude_from_runs(paths: Iterable[Path]) -> set[int]:
    """之前的回放记录里用过的题（开发集、留出集）：reports 里的编号。"""
    out: set[int] = set()
    for p in paths:
        data = json.loads(p.read_text(encoding="utf-8"))
        out |= {int(r["number"]) for r in data.get("reports", [])}
    return out


def load(path: Path) -> Fixset:
    return Fixset.model_validate_json(path.read_text(encoding="utf-8"))


def save(fs: Fixset, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fs.model_dump_json(indent=1), encoding="utf-8")


def is_fixset(data: dict[str, Any]) -> bool:
    return data.get("kind") == KIND


def eval_cases(fs: Fixset) -> list[EvalCase]:
    """转成 replay fix 用的 EvalCase。没有考卷：code 为空，只能跑 control 组。"""
    out = []
    for c in fs.cases:
        if c.gold.status != "ok":
            continue
        exam = Exam(evidence_id=f"fixset-{c.number}", issue=c.number, test_path=c.test_path,
                    code="", test_sha256="-", receipt_sha256="-", package=c.package,
                    module=c.module, python=c.python, pytest=c.pytest, version=c.version)
        out.append(EvalCase(number=c.number, title=c.title, exam=exam, parent=c.fix.parent,
                            fix=c.fix.sha, upstream_pr=c.fix.pr))
    return out


def gold_rows(fs: Fixset) -> list[dict[str, Any]]:
    """replay fix 的 gold 行（直接复用，不再重算）。"""
    return [{"type": "gold", "number": c.number, "gold": c.gold.model_dump(),
             "test_overlay": c.test_overlay} for c in fs.cases]


def render(fs: Fixset) -> str:
    ok = [c for c in fs.cases if c.gold.status == "ok"]
    rule = fs.rule.describe() if fs.rule else "`T: bug`，不带排除标签"
    lines = [
        f"# 修复评测集 v2：{fs.repo}",
        "",
        f"- 规则：{fs.since} 之后创建、已完成关闭，{rule}，按编号从新到旧；"
        f"上游修复要有源码和测试改动、不改依赖，金标准 F2P 非空；凑够 {fs.target} 题为止",
        f"- 排除（之前的开发集 / 留出集）：{len(fs.exclude)} 个",
        f"- 收下 {len(ok)} 题，跳过 {len(fs.skipped)} 个；开始于 {fs.started}",
        "",
        "## 收下的题",
        "",
        "| issue | 创建 | 修复 PR | Python | F2P | 上游改的源码 | 标题 |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in sorted(ok, key=lambda c: -c.number):
        pr = f"#{c.fix.pr}" if c.fix.pr else c.fix.sha[:8]
        lines.append(f"| #{c.number} | {c.created_at:%Y-%m-%d} | {pr} | {c.python} | "
                     f"{len(c.gold.f2p)} | {', '.join(c.src_files)[:80]} | {c.title[:60]} |")
    reasons: dict[str, int] = {}
    for s in fs.skipped:
        key = s.reason.split(":", 1)[0]
        reasons[key] = reasons.get(key, 0) + 1
    lines += ["", "## 跳过的原因", "", "| 原因 | 个数 |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])]
    years: dict[int, int] = {}
    for c in ok:
        years[c.created_at.year] = years.get(c.created_at.year, 0) + 1
    lines += ["", "## 按年份", "", "、".join(f"{y}：{n}" for y, n in sorted(years.items())), ""]
    return "\n".join(lines)
