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
    # PR Case（ADR 0018）：核验中 → 通过验收 / 驳回 / 无法判定 / 没有声明；
    # 维护者 /failgate reseal 时先重新封存考卷，再回到核验中
    VERIFYING = "VERIFYING"
    RESEALING = "RESEALING"
    VERIFIED = "VERIFIED"
    REFUTED = "REFUTED"
    INCONCLUSIVE = "INCONCLUSIVE"
    NO_CLAIM = "NO_CLAIM"
    # 闭环（ADR 0029）：Fixer 开的 PR 被驳回 → 按理由重修 → 推了新提交、等 synchronize 再核验
    REFIXING = "REFIXING"
    REFIXED = "REFIXED"
    PR_OPENED = "PR_OPENED"
    CLOSED = "CLOSED"
    IGNORED = "IGNORED"
    FAILED = "FAILED"


TERMINAL = frozenset({CaseState.CLOSED, CaseState.IGNORED, CaseState.FAILED})
ACTIVE = frozenset(CaseState) - TERMINAL
