from datetime import datetime
from typing import Any

import pytest

from warden.llm import LLMClient
from warden.report import render_summary
from warden.skills.base import IssueSnapshot, SkillContext
from warden.skills.repro import ReproOutput, ReproSkill

TRIAGE = {"type": "bug", "labels": ["bug"], "priority": "P2", "confidence": 0.9,
          "rationale": "crash"}


def render(repro: dict[str, Any], lang: str = "zh", missing: list[str] | None = None) -> str:
    intake = {"language": lang, "missing": missing or []}
    return render_summary(intake, TRIAGE, 0.02, repro=repro)


BASE = ReproOutput(
    level="L1", package="black", reported_version="23.11.0", python="3.12",
    latest_version="26.5.1", fixed_in_latest=True, verdict="REPRODUCED", runs=4,
    script="import black\nblack.format_file_contents('x', fast=False, mode=black.Mode())",
    agent_status="reproduced",
).model_dump()


def test_english_summary_has_no_chinese_repro_text():
    body = render(BASE, "en")
    assert "Reproduced on `black==23.11.0` with Python 3.12 (evidence level L1)" in body
    assert "may already be fixed" in body and "Reproduction script" in body
    assert "复现" not in body


def test_flaky_and_substituted_and_still_broken():
    body = render({**BASE, "verdict": "FLAKY", "fail_rate": 0.4, "runs": 30,
                   "substituted_for": "main@abc`\nevil", "fixed_in_latest": False})
    assert "不稳定" in body and "30 次运行中约 40% 失败" in body
    assert "报告的版本 `main@abc' evil` 无法从 PyPI 安装" in body  # 反引号和换行被清理
    assert "在最新版 `26.5.1` 上仍然存在" in body


def test_script_cannot_escape_code_fence_and_is_truncated():
    script = "x = '''\n```\n@maintainer see http://evil\n```'''\n" + "\n".join(
        f"line{i}" for i in range(200)
    )
    body = render({**BASE, "script": script})
    fence_lines = [ln for ln in body.splitlines() if ln.startswith("````")]
    assert fence_lines == ["````python", "````"]  # 比脚本里最长的 ``` 多一个
    # 前面 4 行 + line0..line75 = 80 行，之后截断
    assert "line75" in body and "line76" not in body and "# …" in body


def test_not_attempted_renders_nothing_and_missing_info_listed():
    body = render(ReproOutput(attempted=False, error="没配置").model_dump(),
                  missing=["repro_steps"])
    assert "复现" not in body.split("请补充")[0] and "请补充以下信息" in body


def ctx(**kw: Any) -> SkillContext:
    base: dict[str, Any] = {
        "issue": IssueSnapshot(repo="a/b", number=1, title="t", body="b", author="alice",
                               created_at=datetime(2024, 1, 1)),
        "llm": LLMClient("http://x", "k"), "model": "deepseek-flash",
        "prior": {"intake": {"verifiability": 0.8}},
        "repo_config": {"repro_package": "mylib"},
    }
    base.update(kw)
    return SkillContext(**base)


@pytest.mark.parametrize(
    ("kw", "why"),
    [
        ({"repo_config": {}}, "没有配置"),
        ({"prior": {}}, "没有 Intake"),
        ({"repo_config": {"repro_package": "bad name!"}}, "不合法"),
    ],
)
async def test_skip_without_calling_runner(kw, why):
    async def runner(req):  # pragma: no cover - 不应该被调用
        raise AssertionError("runner called")

    result = await ReproSkill(runner, max_budget_usd=0.5).run(ctx(**kw))
    out = result.output
    assert isinstance(out, ReproOutput) and not out.attempted and why in (out.error or "")
    assert result.facts == {"evidence_level": "NONE"} and result.cost_usd == 0


async def test_comment_fetch_failure_does_not_block_repro():
    from test_repro_pipeline import FakeRunner, reproduced

    async def broken(repo: str, number: int):
        raise RuntimeError("403")

    runner = FakeRunner(reproduced())
    result = await ReproSkill(runner, max_budget_usd=0.5).run(ctx(comments=broken))
    assert result.facts == {"evidence_level": "L1"} and runner.requests[0].body == "b"


async def test_budget_is_min_of_repro_cap_and_case_budget_left():
    from test_repro_pipeline import FakeRunner, reproduced

    runner = FakeRunner(reproduced(), reproduced())
    skill = ReproSkill(runner, max_budget_usd=0.5)
    await skill.run(ctx(budget_left_usd=0.07))
    await skill.run(ctx(budget_left_usd=None))
    assert [r.budget_usd for r in runner.requests] == [0.07, 0.5]
