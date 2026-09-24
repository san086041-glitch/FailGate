import pytest

from warden.orchestrator.commands import Command, parse_command
from warden.orchestrator.states import TERMINAL, CaseState
from warden.orchestrator.transitions import TABLE, GuardContext, resolve
from warden.platforms.base import User

S = CaseState
MAINT = User(login="maint", association="MEMBER")
STRANGER = User(login="eve", association="NONE")


def test_new_issue_goes_to_intake():
    t = resolve(S.NEW, "issue.opened", GuardContext())
    assert t is not None and t.to is S.INTAKE


def test_intake_then_triage_then_dedup():
    t1 = resolve(S.INTAKE, "skill.done", GuardContext())
    t2 = resolve(S.TRIAGING, "skill.done", GuardContext())
    assert t1 is not None and t1.to is S.TRIAGING
    assert t2 is not None and t2.to is S.DEDUPING


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        ({"dup_high": True, "type": "bug", "repro_enabled": True}, S.DUP_SUSPECTED),
        ({"type": "question"}, S.ANSWERING),
        ({"type": "bug", "repro_enabled": True}, S.REPRODUCING),
        ({"type": "bug", "repro_enabled": True, "budget_ok": False}, S.TRIAGE_ONLY),
        ({"type": "bug", "repro_enabled": False}, S.TRIAGE_ONLY),
        ({"type": "feature"}, S.TRIAGE_ONLY),
    ],
)
def test_route_after_dedup(facts, expected):
    t = resolve(S.DEDUPING, "skill.done", GuardContext(facts=facts))
    assert t is not None and t.to is expected


@pytest.mark.parametrize(
    ("level", "expected"),
    [("L1", S.REPRODUCED), ("L3", S.REPRODUCED), ("L0", S.NEED_INFO), ("delegated", S.NEED_INFO)],
)
def test_repro_outcome_depends_on_evidence_level(level, expected):
    t = resolve(S.REPRODUCING, "skill.done", GuardContext(facts={"evidence_level": level}))
    assert t is not None and t.to is expected


def test_fix_requires_write_permission():
    assert resolve(S.REPRODUCED, "cmd.fix", GuardContext(actor=STRANGER)) is None
    t = resolve(S.REPRODUCED, "cmd.fix", GuardContext(actor=MAINT))
    assert t is not None and t.to is S.FIXING


def test_only_issue_author_can_resume_need_info():
    facts = {"author": "alice"}
    stranger_ctx = GuardContext(actor=STRANGER, facts=facts)
    assert resolve(S.NEED_INFO, "comment.created", stranger_ctx) is None
    alice = User(login="alice")
    t = resolve(S.NEED_INFO, "comment.created", GuardContext(actor=alice, facts=facts))
    assert t is not None and t.to is S.REPRODUCING


def test_close_from_any_active_state_but_not_terminal():
    for state in S:
        t = resolve(state, "issue.closed", GuardContext())
        if state in TERMINAL:
            assert t is None
        else:
            assert t is not None and t.to is S.CLOSED


def test_every_state_is_reachable():
    assert set(S) <= {t.to for t in TABLE} | {S.NEW}


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("/warden fix", Command("fix")),
        ("thanks!\n/warden retry py3.12 pandas2.3", Command("retry", "py3.12 pandas2.3")),
        ("  /warden budget 2.00  ", Command("budget", "2.00")),
        ("/warden merge", None),
        ("please /warden fix", None),
        ("no command here", None),
    ],
)
def test_parse_command(body, expected):
    assert parse_command(body) == expected
