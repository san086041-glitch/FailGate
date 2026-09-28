"""fixture 仓库验收（技术方案第 19、22 节："fixture 仓库达到 L2"）。

    fixtures/repos/<名字>/
        repo/          有 bug 的小项目（自带测试和 pytest 配置）
        fix/           覆盖到 repo/ 上就修好的文件
        issue.md       第一行是标题，其余是正文
        fixture.json   包名、版本、Python、issue 编号、期望的判定

每个 fixture：Intake → 在 repo/ 上让 Agent 写 L2 测试 → 把测试放到"有 bug 的代码"和
"打上 fix 的代码"上各跑几次（复用 fbpa.evaluate_case，fix 就是那次"修复提交"）。

和真实仓库回放的区别：代码、bug、修复都是自己写的、已知的，不依赖 GitHub，也没有
"关闭 PR 不等于修复"这类标准答案噪声；缺点是规模小、可能比真实 bug 简单。
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from failgate.llm import LLMClient
from failgate.replay.fbpa import FbpaCase, candidate_from_l2, evaluate_case, note_for
from failgate.replay.fixes import FixCommit
from failgate.repro.config import PackageConfig
from failgate.repro.issue import L2IssueReport, new_l2_report, reproduce_tree_l2
from failgate.repro.l2 import L2Unsupported, TestReproducer
from failgate.repro.package import SETUP_ERRORS
from failgate.repro.sandbox import ExecResult
from failgate.repro.source import SourceError, SourceTree, pack_dir
from failgate.skills.base import IssueSnapshot, SkillContext
from failgate.skills.intake import IntakeOutput, IntakeSkill

FIXTURES_DIR = Path("fixtures/repos")
BUGGY, FIXED = "buggy", "fixed"


@dataclass
class Fixture:
    name: str
    title: str
    body: str
    package: str
    version: str
    python: str
    number: int
    kind: str
    expect: list[str]
    tree: SourceTree  # 有 bug 的代码
    fixed_tree: SourceTree  # 打上 fix/ 之后的代码

    @property
    def repo(self) -> str:
        return f"fixture/{self.name}"

    @property
    def cfg(self) -> PackageConfig:
        return PackageConfig(name=self.package)


def _tree(name: str, variant: str, tarball: bytes) -> SourceTree:
    # fixture 没有真正的提交号；环境缓存 key 里另有源码包摘要，内容一变就会换环境
    return SourceTree(repo=f"fixture/{name}", sha=variant, committed_at=None, tarball=tarball)


def load_fixture(path: Path) -> Fixture:
    meta = json.loads((path / "fixture.json").read_text(encoding="utf-8"))
    title, _, body = (path / "issue.md").read_text(encoding="utf-8").partition("\n")
    with tempfile.TemporaryDirectory() as tmp:
        merged = Path(tmp, "repo")
        shutil.copytree(path / "repo", merged)
        shutil.copytree(path / "fix", merged, dirs_exist_ok=True)
        fixed = pack_dir(merged)
    return Fixture(
        name=path.name, title=title.strip(), body=body.strip(), package=meta["package"],
        version=meta["version"], python=meta["python"], number=int(meta["number"]),
        kind=meta.get("kind", ""), expect=list(meta.get("expect", ["REPRODUCED"])),
        tree=_tree(path.name, BUGGY, pack_dir(path / "repo")),
        fixed_tree=_tree(path.name, FIXED, fixed),
    )


def load_all(root: Path = FIXTURES_DIR, only: Sequence[str] = ()) -> list[Fixture]:
    dirs = sorted(p for p in root.iterdir() if (p / "fixture.json").is_file())
    return [load_fixture(p) for p in dirs if not only or p.name in only]


class FixtureResult(BaseModel):
    name: str
    kind: str
    expect: list[str]
    report: L2IssueReport
    fbpa: FbpaCase | None = None

    @property
    def verdict(self) -> str | None:
        run = self.report.source.run
        return run.verdict.kind.value if run else None

    @property
    def l2(self) -> bool:
        return self.report.source.level.value == "L2"

    @property
    def passed(self) -> bool:
        """验收：达到 L2，并且判定结果在期望之内（偶发 bug 允许 FLAKY）。"""
        return self.l2 and self.verdict in self.expect


async def run_intake(fx: Fixture, llm: LLMClient, model: str) -> tuple[IntakeOutput, float]:
    res = await IntakeSkill().run(SkillContext(
        issue=IssueSnapshot(repo=fx.repo, number=fx.number, title=fx.title, body=fx.body),
        llm=llm, model=model,
    ))
    assert isinstance(res.output, IntakeOutput)
    return res.output, res.cost_usd


async def reproduce_fixture(
    fx: Fixture,
    intake: IntakeOutput,
    *,
    llm: LLMClient,
    model: str,
    tester: TestReproducer,
    max_steps: int,
    max_attempts: int,
    budget_usd: float,
    artifacts_dir: Path | None,
) -> L2IssueReport:
    report = new_l2_report(fx.repo, fx.number, fx.title, fx.body, intake, fx.cfg, fx.repo)
    return await reproduce_tree_l2(
        report, fx.tree, title=fx.title, body=fx.body, created_at=None, intake=intake,
        cfg=fx.cfg, llm=llm, model=model, tester=tester, max_steps=max_steps,
        max_attempts=max_attempts, budget_usd=budget_usd, artifacts_dir=artifacts_dir,
        python=fx.python, version=fx.version,
    )


async def fixture_fbpa(
    fx: Fixture, report: L2IssueReport, tester: TestReproducer, *, runs: int = 2
) -> FbpaCase:
    """把 L2 测试放到有 bug 的代码（父提交）和打上 fix 的代码（修复提交）上各跑 runs 次。"""
    trees = {BUGGY: fx.tree, FIXED: fx.fixed_tree}
    pin = report.source.pytest

    async def find_fix(_: int) -> FixCommit:
        return FixCommit(pr=None, sha=FIXED, parent=BUGGY)

    async def pretend(_: FixCommit) -> str:
        return fx.version

    async def run_at(sha: str, python: str, version: str, code: str) -> list[ExecResult]:
        prepared = await tester.prepare(fx.cfg, trees[sha], number=fx.number, python=python,
                                        version=version, pytest=pin)
        return [await tester.run_once(prepared, code) for _ in range(runs)]

    return await evaluate_case(
        candidate_from_l2(report), find_fix=find_fix, pretend=pretend, run_at=run_at,
        setup_errors=(*SETUP_ERRORS, SourceError, L2Unsupported),
    )


def render(results: Sequence[FixtureResult], meta: dict[str, Any]) -> str:
    ok = sum(r.passed for r in results)
    fb = sum(1 for r in results if r.fbpa and r.fbpa.outcome == "fb_pa")
    cost = sum(r.report.total_cost_usd for r in results)
    lines = [
        "# fixture 仓库验收（L2）",
        "",
        f"- 时间：{meta['started']}；模型：{meta['model']}；提示词 repro_test_v{meta['prompt']}",
        f"- 验收标准：达到 L2，且判定结果在 fixture.json 的 expect 之内；"
        f"另把测试放到有 bug / 打上 fix 的代码上各跑 {meta['runs']} 次（严格 FB/PA）",
        "",
        f"**结果：{ok}/{len(results)} 通过验收；严格 FB/PA {fb}/{len(results)}；"
        f"花费 ${cost:.4f}**",
        "",
        "| fixture | 类型 | 期望 | L2 | 判定 | 复现概率 | 步数 / 提交 | 严格 FB/PA | 花费 | 验收 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        a, run = r.report.agent, r.report.source.run
        rate = run.verdict.fail_rate if run else None
        lines.append(
            f"| {r.name} | {r.kind} | {'/'.join(r.expect)} | {'✅' if r.l2 else '❌'} | "
            f"{r.verdict or (r.report.source.error or '—')[:40]} | "
            f"{f'{rate:.0%}' if rate is not None else '—'} | "
            f"{a.steps if a else 0} / {len(a.attempts) if a else 0} | "
            f"{r.fbpa.outcome if r.fbpa else '—'} | ${r.report.total_cost_usd:.4f} | "
            f"{'✅' if r.passed else '❌'} |"
        )
    lines += ["", "## 测试代码", ""]
    for r in results:
        a = r.report.agent
        if r.l2 and a and a.final_script:
            lines += [f"### {r.name}（`{r.report.source.test_path}`）", "", "```python",
                      a.final_script.rstrip(), "```", ""]
        elif a is not None:
            why = a.give_up_reason or (a.attempts[-1].reason if a.attempts else a.status)
            lines += [f"### {r.name}：没有达到 L2", "", f"{why}", ""]
    notes = [r for r in results if r.fbpa and r.fbpa.outcome not in ("fb_pa", "no_script")]
    if notes:
        lines += ["## 严格 FB/PA 的说明", ""]
        lines += [f"- {r.name}（{r.fbpa.outcome}）：{note_for(r.fbpa)[:200]}"
                  for r in notes if r.fbpa]
    return "\n".join(lines) + "\n"


def dump(results: Sequence[FixtureResult], meta: dict[str, Any]) -> str:
    return json.dumps(
        {"meta": meta, "results": [
            {**r.model_dump(mode="json"), "passed": r.passed, "verdict": r.verdict}
            for r in results
        ]},
        ensure_ascii=False, indent=2,
    )
