from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from failgate.replay.repro import outcome, render, select_issues, summarize
from failgate.replay.selection import load as load_selection
from failgate.repro.agent import AgentResult
from failgate.repro.evidence import EvidenceLevel
from failgate.repro.issue import IssueReproReport
from failgate.repro.judge import Verdict, VerdictKind
from failgate.repro.package import PackageRepro, VersionRun

EVAL = Path(__file__).resolve().parents[1] / "eval"


@dataclass
class Doc:
    number: int
    labels: list[str]
    state: str = "closed"
    state_reason: str | None = "completed"
    created_at: datetime | None = field(default_factory=lambda: datetime(2024, 1, 1))


def test_selection_rule():
    docs = [
        Doc(10, ["T: bug", "C: crash"]),
        Doc(9, ["T: bug", "C: packaging"]),  # 不在复现类别里
        Doc(8, ["T: bug", "C: parser", "R: duplicate"]),  # 带排除标签
        Doc(7, ["T: bug", "C: invalid code"], state_reason="not_planned"),
        Doc(6, ["T: bug", "C: crash"], created_at=datetime(2021, 5, 1)),  # 太早
        Doc(5, ["T: style", "C: crash"]),
        Doc(4, ["T: bug", "C: unstable formatting"]),
        Doc(3, ["T: bug", "C: parser"]),
    ]
    rule = load_selection("psf/black", EVAL)  # 提交在仓库里的 black 规则（ADR 0040）
    picked = select_issues(docs, rule=rule, since=datetime(2022, 1, 1), limit=2)
    assert [d.number for d in picked] == [10, 4]  # 从新到旧，取满 2 个就停
    held_out = select_issues(docs, rule=rule, since=datetime(2022, 1, 1), limit=2, offset=1)
    assert [d.number for d in held_out] == [4, 3]  # 跳过开发集用过的 #10


def run(kind: VerdictKind) -> VersionRun:
    return VersionRun(version="1", python="3.12", env_key="k", cache_hit=True,
                      verdict=Verdict(kind=kind, reason="r"))


def report(n: int, **kw) -> IssueReproReport:
    repro = kw.pop("repro")
    return IssueReproReport(
        repo="a/b", number=n, title=f"t{n}", intake_version="1", intake_python=None,
        has_traceback=kw.pop("tb", False), repro=repro, agent=kw.pop("agent", None),
        intake_cost_usd=0.001, judge_cost_usd=0.0,
    )


def test_outcomes_and_summary():
    ok = PackageRepro(package="p", module="p", reported=run(VerdictKind.REPRODUCED),
                      latest=run(VerdictKind.NOT_REPRODUCED), level=EvidenceLevel.L1)
    still = PackageRepro(package="p", module="p", reported=run(VerdictKind.REPRODUCED),
                         latest=run(VerdictKind.REPRODUCED), level=EvidenceLevel.L1)
    setup = PackageRepro(package="p", module="p", error="无法解析版本")
    miss = PackageRepro(package="p", module="p")
    reports = [
        report(1, repro=ok, tb=True, agent=AgentResult(status="reproduced", steps=10,
                                                      cost_usd=0.01)),
        report(2, repro=still, agent=AgentResult(status="reproduced", steps=20)),
        report(3, repro=setup),
        report(4, repro=miss, agent=AgentResult(status="gave_up", give_up_reason="要 GPU")),
    ]
    assert [outcome(r) for r in reports] == ["fb_pa", "still_fails_latest", "setup_failed",
                                             "gave_up"]
    s = summarize(reports)
    assert s["n"] == 4 and s["l1"] == 2 and s["fb_pa"] == 1 and s["l1_with_traceback"] == 1
    assert s["mean_steps"] == 10.0  # 只在有 Agent 的 3 个里平均
    md = render("a/b", reports, {"started": "x", "model": "m", "prompt": "1",
                                 "selection": "s", "max_steps": 40, "max_attempts": 4,
                                 "budget_usd": 0.5})
    assert "L1 复现率 | 2/4 = 50%" in md and "#4（gave_up）：要 GPU" in md
