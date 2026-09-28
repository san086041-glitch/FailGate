from enum import StrEnum


class CaseState(StrEnum):
    """状态名表示 Case 当前所处的阶段：INTAKE / TRIAGING / DEDUPING 期间对应的能力模块正在运行。"""

    NEW = "NEW"
    INTAKE = "INTAKE"
    TRIAGING = "TRIAGING"
    DEDUPING = "DEDUPING"
    DUP_SUSPECTED = "DUP_SUSPECTED"
    ANSWERING = "ANSWERING"
    ANSWERED = "ANSWERED"
    REPRODUCING = "REPRODUCING"
    # 已复现，同时也是等待维护者 /failgate fix 的状态
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
