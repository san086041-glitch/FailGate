"""回放选题规则按仓库配置（ADR 0040）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from failgate.replay import selection
from failgate.replay.fixset import Fixset, render
from failgate.replay.selection import Selection

EVAL = Path(__file__).resolve().parents[1] / "eval"


def test_committed_rules_load():
    for repo in ("psf/black", "pylint-dev/pylint", "pypa/packaging"):
        rule = selection.load(repo, EVAL)
        assert rule.bug_labels and rule.note


def test_black_rule_matches_old_hardcoded_rule():
    rule = selection.load("psf/black", EVAL)
    assert rule.is_repro(["T: bug", "C: crash"])
    assert rule.is_bug(["T: bug", "C: packaging"]) and not rule.is_repro(["T: bug", "C: packaging"])
    assert not rule.is_bug(["T: bug", "R: duplicate"])
    assert not rule.is_bug(["T: style", "C: crash"])


def test_pylint_false_positive_without_bug_label_counts():
    rule = selection.load("pylint-dev/pylint", EVAL)
    assert rule.is_repro(["False Positive 🦟"])
    assert rule.is_bug(["Bug :beetle:"]) and not rule.is_repro(["Bug :beetle:"])
    # 修在 astroid 里的：这个仓库找不到修复提交
    assert not rule.is_bug(["Crash 💥", "Needs astroid update"])


def test_empty_repro_labels_means_every_bug():
    rule = Selection(bug_labels=["bug"], exclude_labels=["duplicate"])
    assert rule.is_repro(["bug", "packaging.version"])
    assert not rule.is_repro(["bug", "duplicate"])
    assert not rule.is_repro(["enhancement"])


def test_missing_rule_names_the_file(tmp_path):
    with pytest.raises(selection.SelectionMissing, match="selection.json"):
        selection.load("someone/else", tmp_path)


def test_describe_and_fixset_report_use_rule():
    rule = Selection(bug_labels=["bug"], repro_labels=["crash"], exclude_labels=["wontfix"])
    assert "`crash`" in rule.describe(repro=True) and "`crash`" not in rule.describe()
    text = render(Fixset(repo="a/b", since="2022-01-01", target=3, rule=rule))
    assert "带 `bug` 之一" in text
    # 旧文件没有 rule：照旧写 black 的规则
    assert "`T: bug`" in render(Fixset(repo="psf/black", since="2022-01-01", target=3))
