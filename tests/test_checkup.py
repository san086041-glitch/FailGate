"""仓库体检（ADR 0043）：起草规则、可用题判断、配置建议、报告。不调网络。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from failgate.replay import checkup as cu


def test_draft_selection_from_real_label_sets():
    pylint = ["Bug :beetle:", "Crash 💥", "False Positive 🦟", "False Negative 🦋", "Regression",
              "Duplicate 🐫", "Invalid", "Won't fix/not planned", "Cannot reproduce 🤷",
              "Upstream Bug 🪲", "Enhancement ✨", "Question"]
    rule = cu.draft_selection(pylint)
    assert rule.bug_labels == ["Bug :beetle:", "Crash 💥", "False Positive 🦟",
                               "False Negative 🦋", "Regression"]
    assert "Upstream Bug 🪲" in rule.exclude_labels and "Upstream Bug 🪲" not in rule.bug_labels
    assert rule.repro_labels == ["Crash 💥", "False Positive 🦟", "False Negative 🦋",
                                 "Regression"]
    assert "--accept-rule" in rule.note
    # packaging：只有一个笼统的 bug 标签 → 复现不按类别筛
    rule = cu.draft_selection(["bug", "duplicate", "invalid", "wontfix", "question", "enhancement"])
    assert rule.bug_labels == ["bug"] and rule.repro_labels == []
    assert set(rule.exclude_labels) == {"duplicate", "invalid", "wontfix", "question"}
    # 只有 issue type、没有 bug 标签：选不出题（命令会报错让人处理）
    assert cu.draft_selection(["enhancement", "docs"]).bug_labels == []


def test_usable_pr_matches_the_fixset_rule():
    assert cu.usable_pr(["src/pkg/a.py", "tests/test_a.py", "CHANGES.md"])
    assert not cu.usable_pr(["tests/test_a.py"])  # 只改测试
    assert not cu.usable_pr(["src/pkg/a.py"])  # 没改测试
    assert not cu.usable_pr(["src/pkg/a.py", "tests/test_a.py", "pyproject.toml"])  # 改依赖


def test_survey_estimate_and_python_share():
    s = cu.Survey(repo="a/b", closed=300, sampled=200, usable=50, languages={"Python": 0.97})
    assert s.est_usable == 75 and s.python_share == 0.97
    assert cu.Survey(repo="a/b").est_usable == 0


def fix_row(n: int, **l3: Any) -> dict[str, Any]:
    return {"number": n, "kind": "fix", "expected": "VERIFIED", "verdict": "VERIFIED",
            "correct": True, "layer3": {"status": "pass", "reason": "pass", **l3}}


def test_suggestions_point_at_the_right_config():
    rows = [
        fix_row(1, status="none", reason="none"),
        fix_row(2, status="none", reason="not_run", not_run=["tests/t.py"],
                missing_modules=["pretend"]),
        fix_row(3),
        {"number": 4, "kind": "break_other", "expected": "REFUTED", "verdict": "VERIFIED",
         "correct": False, "layer3": {"status": "pass"}},
    ]
    tips = cu.suggestions(rows, {"outcomes": {"setup_failed": 2}})
    text = "\n".join(tips)
    assert "1/3 个真实修复上一个相关测试都没挑到" in text and "related_always" in text
    assert "`pretend`" in text and "test_deps" in text
    assert "仓库本身没测试覆盖" in text
    assert "L2 有 2 题环境没搭起来" in text
    assert cu.suggestions([fix_row(1)], None) == []


def test_render_has_numbers_with_intervals_and_lists_na():
    state = cu.Checkup(repo="a/b", package="b", since="2022-01-01", offset=12, limit=12,
                       survey=cu.Survey(repo="a/b", stars=5, closed=10, sampled=10, usable=4,
                                        languages={"Python": 0.5}, pypi=None),
                       rule_path="eval/datasets/a__b/selection.json", rule_drafted=True,
                       l2_run="eval/runs/x.json", verify_run="eval/runs/y.jsonl")
    rule = cu.draft_selection(["bug", "duplicate"])
    l2 = {"l2": 8, "n": 12, "fb_pa": 6, "fbpa_eligible": 6, "total_cost_usd": 0.3,
          "outcomes": {}}
    rows = [fix_row(1), {"number": 2, "kind": "break_other", "expected": "REFUTED",
                         "verdict": "N/A", "correct": None, "skipped": "no_candidate"}]
    md = cu.render(state, rule, l2, rows)
    assert "不是纯 Python" in md and "没找到（只能走源码模式）" in md
    assert "8/12 = 67%（39%–86%）" in md and "6/6 = 100%" in md
    assert "由体检起草、人确认后使用" in md
    assert "n/a（不计入）：#2 break_other：no_candidate" in md
    assert "没发现需要补的配置" in md


def test_state_round_trip(tmp_path):
    path = cu.state_path("a/b", tmp_path)
    assert cu.load_state(path) is None
    st = cu.Checkup(repo="a/b", package="b", since="2022-01-01", offset=12, limit=12,
                    l2_run="eval/runs/x.json")
    cu.save_state(st, path)
    got = cu.load_state(path)
    assert got is not None and got.l2_run == "eval/runs/x.json"
    assert path.as_posix().endswith("checkups/a__b/checkup.json")


class FakeGH:
    """survey 用到的 GitHubRest 部分。"""

    def __init__(self) -> None:
        self._http = SimpleNamespace(get=self._get)

    async def _get(self, url: str) -> Any:
        return SimpleNamespace(json=lambda: {"Python": 900, "Shell": 100})

    async def repo(self, name: str) -> dict[str, Any]:
        return {"stargazers_count": 42, "size": 2048, "archived": False,
                "pushed_at": "2026-10-01T00:00:00Z"}

    async def list_labels(self, name: str) -> list[dict[str, Any]]:
        return [{"name": "bug"}, {"name": "duplicate"}]

    async def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        def node(n: int, files: list[str] | None, merged: bool = True) -> dict[str, Any]:
            closer = None if files is None else {
                "__typename": "PullRequest", "merged": merged,
                "files": {"nodes": [{"path": f} for f in files]}}
            return {"number": n, "timelineItems": {"nodes": [{"closer": closer}]}}
        return {"search": {"issueCount": 30, "pageInfo": {"hasNextPage": False},
                           "nodes": [node(1, ["src/a.py", "tests/test_a.py"]),
                                     node(2, ["docs/x.rst"]),
                                     node(3, ["src/a.py", "tests/test_a.py"], merged=False),
                                     node(4, None)]}}


class FakeHTTP:
    async def get(self, url: str) -> Any:
        return SimpleNamespace(status_code=200, json=lambda: {"info": {"version": "1.2.3"}})


async def test_survey_counts_closers_and_usable_prs():
    s = await cu.survey(FakeGH(), FakeHTTP(), "a/b", "b", since="2022-01-01")
    assert (s.stars, s.size_mb, s.pypi, s.labels) == (42, 2.0, "1.2.3", ["bug", "duplicate"])
    assert (s.closed, s.sampled, s.closer_pr, s.usable) == (30, 4, 2, 1)
    assert s.est_usable == 8 and s.python_share == 0.9


def test_plain_summary_comes_first_with_a_verdict():
    state = cu.Checkup(repo="a/b", package="b", since="2022-01-01", offset=12, limit=12,
                       survey=cu.Survey(repo="a/b", closed=187, sampled=187, usable=91))
    l2 = {"l2": 10, "n": 12, "fb_pa": 7, "fbpa_eligible": 9, "outcomes": {}}
    cheats = [{"number": i, "kind": k, "expected": "REFUTED", "verdict": "REFUTED",
               "correct": True} for i in range(2) for k in ("revert_code", "unrelated")]
    md = cu.render(state, None, l2, [fix_row(1), *cheats])
    head = md.split("以下是详细数据")[0]
    assert "## 大白话总结" in head and "估计约 91 个能当考题" in head
    assert "AI 为其中 **10** 个写出了能抓住 bug 的测试" in head and "**7** 个确认合格" in head
    assert "真修复 1/1 正确放行，作弊 4/4 被拦住" in head
    assert "结论：FailGate 在这个仓库上能用" in head and "名词对照" in head
    # 冤枉了真修复 → 结论变成先看判错的案例
    bad = {**fix_row(2), "verdict": "REFUTED", "correct": False}
    assert "需要先看看判错的案例" in cu.render(state, None, l2, [bad, *cheats])
    # 只做了概况：说清楚还没跑
    assert "还没跑出题和阅卷" in cu.render(state, None, None, [])
