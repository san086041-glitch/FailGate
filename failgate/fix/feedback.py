"""把 ClaimVerify 的驳回变成修复 Agent 能照着改的反馈（ADR 0029）。

阅卷（ClaimVerify）→ 答题（修复 Agent）的回传通道：
  · 理由用核验报告里同一套中文说法（verify/report.py），不另起一套；
  · 第三层查出的新增失败变成 must_pass：下一轮验收时在全新工作区里和考卷一起重跑，
    不靠 Agent 自觉去跑（ADR 0028：9 次回归里 7 次 Agent 没跑相关测试）；
  · 篡改类理由（改了考卷、加 skip）修复 Agent 本来就做不到（工具层禁止），原样告知即可。
"""

from __future__ import annotations

from dataclasses import dataclass

from failgate.verify.engine import ClaimResult, ClaimVerdict, Verification
from failgate.verify.report import _TEXT, reason_text

MAX_MUST_PASS = 20


@dataclass(frozen=True)
class Feedback:
    text: str
    must_pass: list[str]
    actionable: bool  # 有修复 Agent 能改的理由（考卷没过、第三层新增失败）


def feedback_from(v: Verification, issue: int | None = None) -> Feedback | None:
    """被驳回的那条声明 → 反馈；没有被驳回的声明返回 None。"""
    refuted = [c for c in v.claims if c.verdict == ClaimVerdict.REFUTED
               and (issue is None or c.issue == issue)]
    if not refuted:
        return None
    return feedback_from_claim(refuted[0])


def feedback_from_claim(c: ClaimResult) -> Feedback:
    t = _TEXT["zh"]
    lines = [f"- {reason_text(code, c, t)}（{code}）" for code in c.reasons]
    must_pass: list[str] = []
    if c.layer3 is not None and c.layer3.new_failures:
        must_pass = list(c.layer3.new_failures[:MAX_MUST_PASS])
        lines.append("- 第三层在补丁上新出现失败的已有测试（修复前是通过的）：")
        lines += [f"  - {n}" for n in must_pass]
    if c.layer1 is not None and c.layer1.status == "fail":
        lines.append(f"- 封存的考卷 {c.test_path} 在补丁上没有通过（{c.layer1.reason}）")
    actionable = bool(must_pass) or any(r.startswith("layer1:") for r in c.reasons)
    return Feedback(text="\n".join(lines), must_pass=must_pass, actionable=actionable)
