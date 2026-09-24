from enum import StrEnum


class CaseState(StrEnum):
    NEW = "NEW"
    INTAKE = "INTAKE"
    TRIAGED = "TRIAGED"
    DEDUPED = "DEDUPED"
    DUP_SUSPECTED = "DUP_SUSPECTED"
    ANSWERING = "ANSWERING"
    ANSWERED = "ANSWERED"
    REPRODUCING = "REPRODUCING"
    # 已复现，同时也是等待维护者 /warden fix 的状态
    REPRODUCED = "REPRODUCED"
    NEED_INFO = "NEED_INFO"
    TRIAGE_ONLY = "TRIAGE_ONLY"
    FIXING = "FIXING"
    PR_OPENED = "PR_OPENED"
    CLOSED = "CLOSED"
    IGNORED = "IGNORED"
    FAILED = "FAILED"


TERMINAL = frozenset({CaseState.CLOSED, CaseState.IGNORED, CaseState.FAILED})
ACTIVE = frozenset(CaseState) - TERMINAL
