"""复现判定：怎么确认"真的复现了"，而不是 Agent 自己造出来一个失败（技术方案 8.6 节）。

    第一次运行 ──通过──────────────→ NOT_REPRODUCED
        │ 超时 / OOM ─────────────→ INCONCLUSIVE（环境问题，不能当证据）
        │ 失败
        ▼
    和报告比对（有堆栈按签名，没堆栈由 LLM 按细则打分）
        │ 报告在包内抛出、这次没经过包 → UNRELATED_FAILURE（脚本自己 raise 的）
        │ < 0.6 ──────────────────→ UNRELATED_FAILURE（失败了，但不是这个 bug）
        ▼
    再跑 3 次，失败且签名一致才算"又失败了一次"
        │ 全部一致 ───────────────→ REPRODUCED
        │ 有通过的 → 追加到 30 次 → FLAKY（报告复现概率）

judge() 是纯函数，输入所有运行结果；assess() 负责按需重跑，只在需要时才多花时间。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum
from functools import partial

from pydantic import BaseModel

from warden.index.trace import TraceSignature
from warden.repro.sandbox import ExecResult
from warden.repro.signature import failure_signature, match_score

MATCH_THRESHOLD = 0.6
STABILITY_RUNS = 3
FLAKY_RUNS = 30


class VerdictKind(StrEnum):
    REPRODUCED = "REPRODUCED"
    FLAKY = "FLAKY"
    NOT_REPRODUCED = "NOT_REPRODUCED"
    UNRELATED_FAILURE = "UNRELATED_FAILURE"
    INCONCLUSIVE = "INCONCLUSIVE"


class Verdict(BaseModel):
    kind: VerdictKind
    reason: str
    match: float | None = None
    match_method: str | None = None  # "signature" | "llm"
    fail_rate: float | None = None
    runs: int = 1
    observed: TraceSignature | None = None
    reported: TraceSignature | None = None

    @property
    def reproduced(self) -> bool:
        return self.kind in (VerdictKind.REPRODUCED, VerdictKind.FLAKY)


def _output(run: ExecResult) -> str:
    return run.stdout + "\n" + run.stderr


def score_first_run(run: ExecResult) -> Verdict | None:
    """第一次运行的判定。返回 None 表示"失败且和报告一致"，需要继续做稳定性重跑。"""
    if run.infra_failure:
        what = "超时" if run.timed_out else "内存超限被杀"
        return Verdict(kind=VerdictKind.INCONCLUSIVE, reason=f"运行{what}，不能作为复现证据")
    if not run.failed:
        return Verdict(kind=VerdictKind.NOT_REPRODUCED, reason="脚本正常退出，没有出现报告的失败")
    return None


def judge(
    runs: list[ExecResult],
    *,
    reported_traceback: str | None,
    package: str | None,
    llm_match: float | None = None,
    threshold: float = MATCH_THRESHOLD,
) -> Verdict:
    """runs[0] 是候选复现的第一次运行，其余是稳定性重跑。

    llm_match：issue 没有堆栈时，由 LLM 按评分细则给出的一致度（0–1）；没有堆栈也没有
    llm_match 时无法判定，返回 INCONCLUSIVE，调用方应先去做 LLM 判定。
    """
    if not runs:
        raise ValueError("至少需要一次运行结果")
    first = runs[0]
    early = score_first_run(first)
    if early is not None:
        return early

    observed = failure_signature(_output(first), package)
    reported = failure_signature(reported_traceback, package)
    if reported is not None and reported.frames and not (observed and observed.frames):
        # 报告的失败发生在目标包里，这次的失败却一帧都没经过目标包：多半是复现脚本
        # 自己 raise 的（类型、消息都能抄，单看签名分数会有 0.7）
        return Verdict(
            kind=VerdictKind.UNRELATED_FAILURE,
            reason="报告的异常从目标包内抛出，这次的失败没有经过目标包的代码",
            match=0.0, match_method="signature", observed=observed, reported=reported,
        )
    if reported is not None:
        match, method = match_score(observed, reported), "signature"
    elif llm_match is not None:
        match, method = llm_match, "llm"
    else:
        return Verdict(
            kind=VerdictKind.INCONCLUSIVE,
            reason="报告里没有堆栈，需要 LLM 对照预期和实际行为判定",
            observed=observed,
        )
    base = {"match": match, "match_method": method, "observed": observed, "reported": reported}
    if match < threshold:
        return Verdict(
            kind=VerdictKind.UNRELATED_FAILURE,
            reason=f"失败了，但和报告不一致（一致度 {match:.2f} < {threshold}）",
            **base,
        )

    same = sum(1 for r in runs if same_failure(r, observed, package))
    rate = same / len(runs)
    if rate < 1.0:
        return Verdict(
            kind=VerdictKind.FLAKY,
            reason=f"{len(runs)} 次运行中 {same} 次出现同样的失败",
            fail_rate=round(rate, 4),
            runs=len(runs),
            **base,
        )
    return Verdict(
        kind=VerdictKind.REPRODUCED,
        reason=f"{len(runs)} 次运行都出现与报告一致的失败",
        fail_rate=1.0,
        runs=len(runs),
        **base,
    )


def same_failure(run: ExecResult, first: TraceSignature | None, package: str | None) -> bool:
    """重跑时"又失败了一次"的标准：失败，且签名与第一次一致（换了一种失败不算）。
    严格 FB/PA 也用它判断"修复前的失败"是不是 L1 复现时的那个失败。"""
    if not run.failed:
        return False
    if first is None:
        return True
    sig = failure_signature(_output(run), package)
    # 先比完全相等：签名信息很少时（比如没有消息），自己和自己比打分也可能过不了阈值
    return sig == first or match_score(sig, first) >= MATCH_THRESHOLD


async def assess(
    first: ExecResult,
    rerun: Callable[[], Awaitable[ExecResult]],
    *,
    reported_traceback: str | None,
    package: str | None,
    llm_match: float | None = None,
    stability_runs: int = STABILITY_RUNS,
    flaky_runs: int = FLAKY_RUNS,
) -> Verdict:
    """按需重跑后给出最终判定：第一次就没对上的不重跑；3 次重跑全一致就停；否则追加到 30 次。"""
    decide = partial(
        judge, reported_traceback=reported_traceback, package=package, llm_match=llm_match
    )
    verdict = decide([first])
    if verdict.kind != VerdictKind.REPRODUCED:
        return verdict
    runs = [first]
    for _ in range(stability_runs):
        runs.append(await rerun())
    verdict = decide(runs)
    if verdict.kind == VerdictKind.FLAKY:
        while len(runs) < flaky_runs:
            runs.append(await rerun())
        verdict = decide(runs)
    return verdict
