"""核验报告：PR 上的一条评论（PR B 接入流水线后发出），命令行也打印它。

措辞刻意保守：通过验收 = 通过验收测试 + 未发现篡改 + 无新增回归，不说"修复正确"。
"""

from __future__ import annotations

import json
import re
from typing import Any

from .engine import ClaimResult, ClaimVerdict, ExamRun, Verification

_TEXT: dict[str, dict[str, str]] = {
    "zh": {
        "title": "🛡️ **FailGate 核验：PR #{pr}**",
        "no_claim": (
            "这个 PR 没有声明修复任何 issue（标题或正文里没有 `fixes #N` 这类写法），"
            "没有可核验的考卷。"
        ),
        "claim": "**声称修复 #{issue}**　{badge}",
        "VERIFIED": "✅ 通过验收：通过验收测试、未发现篡改、无新增回归（这不等于\"修复一定正确\"）",
        "REFUTED": "❌ 驳回",
        "INCONCLUSIVE": "⚪ 无法判定",
        "exam": "① 封存考卷 `{path}`（sha256 `{sha}`）：{reason}",
        "runs": "合并基点 `{base}`：{b}；PR `{head}`：{h}",
        "tamper_none": "② 篡改：未发现",
        "tamper": "② 篡改：",
        "related": "③ 相关测试：{reason}",
        "new_fail": "新出现的失败：",
        "why": "原因：",
        "local": "本地复验：`failgate verify {repo}#{pr}`",
        "receipt": "核验收据 `{short}`",
        "high": "高危",
        "medium": "需要维护者留意",
    },
    "en": {
        "title": "🛡️ **FailGate verification: PR #{pr}**",
        "no_claim": "This PR does not claim to fix any issue (no `fixes #N` in the title or "
                    "description), so there is no acceptance test to check it against.",
        "claim": "**Claims to fix #{issue}**　{badge}",
        "VERIFIED": "✅ Accepted: passes the acceptance test, no tampering found, no new "
                    "failures (this does not mean the fix is necessarily correct)",
        "REFUTED": "❌ Rejected",
        "INCONCLUSIVE": "⚪ Inconclusive",
        "exam": "① Sealed acceptance test `{path}` (sha256 `{sha}`): {reason}",
        "runs": "merge base `{base}`: {b}; PR `{head}`: {h}",
        "tamper_none": "② Tampering: none found",
        "tamper": "② Tampering:",
        "related": "③ Related tests: {reason}",
        "new_fail": "New failures:",
        "why": "Reasons:",
        "local": "Re-run locally: `failgate verify {repo}#{pr}`",
        "receipt": "Verification receipt `{short}`",
        "high": "high risk",
        "medium": "for the maintainer to review",
    },
}

_OUTCOME = {
    "zh": {"failed_same": "封存的失败", "failed_other": "别的失败", "passed": "通过",
           "skipped": "被跳过", "invalid": "跑不起来", "infra": "超时 / 内存超限"},
    "en": {"failed_same": "the sealed failure", "failed_other": "a different failure",
           "passed": "passed", "skipped": "skipped", "invalid": "could not run",
           "infra": "timeout / OOM"},
}


def _runs(runs: list[ExamRun], lang: str) -> str:
    if not runs:
        return "—"
    counts: dict[str, int] = {}
    for r in runs:
        counts[r.outcome] = counts.get(r.outcome, 0) + 1
    sep = "，" if lang == "zh" else ", "
    return sep.join(f"{n}/{len(runs)} {_OUTCOME[lang][o]}" for o, n in counts.items())


def _claim_lines(c: ClaimResult, v: Verification, t: dict[str, str], lang: str) -> list[str]:
    lines = [t["claim"].format(issue=c.issue, badge=t[c.verdict.value])]
    if c.verdict != ClaimVerdict.VERIFIED:
        lines.append(t["why"] + ("；" if lang == "zh" else "; ").join(c.reasons))
    if c.layer1 is not None:
        lines.append("- " + t["exam"].format(path=c.test_path, sha=(c.test_sha256 or "")[:12],
                                             reason=c.layer1.reason))
        if c.layer1.base or c.layer1.head:
            lines.append("  " + t["runs"].format(base=v.base_sha[:7], head=v.head_sha[:7],
                                                 b=_runs(c.layer1.base, lang),
                                                 h=_runs(c.layer1.head, lang)))
    if c.layer2 is not None:
        if not c.layer2.signals:
            lines.append("- " + t["tamper_none"])
        else:
            lines.append("- " + t["tamper"])
            lines += [f"  - `{s.path}`: {s.detail} ({t[s.level]})" for s in c.layer2.signals]
    if c.layer3 is not None:
        lines.append("- " + t["related"].format(reason=c.layer3.reason))
        if c.layer3.new_failures:
            lines.append("  " + t["new_fail"] + " " + ", ".join(
                f"`{n}`" for n in c.layer3.new_failures[:10]))
    return lines


def render_verification(v: Verification, lang: str = "en") -> str:
    t = _TEXT["zh" if lang == "zh" else "en"]
    lang = "zh" if lang == "zh" else "en"
    lines = [t["title"].format(pr=v.pr), ""]
    if not v.claims:
        return "\n".join([*lines, t["no_claim"]])
    for c in v.claims:
        lines += [*_claim_lines(c, v, t, lang), ""]
    lines.append(t["local"].format(repo=v.repo, pr=v.pr))
    lines += _receipt_block(v.receipt(), t)
    return "\n".join(lines)


def _receipt_block(receipt: dict[str, Any], t: dict[str, str]) -> list[str]:
    body = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True)
    longest = max((len(m) for m in re.findall(r"`+", body)), default=0)
    fence = "`" * max(3, longest + 1)
    short = str(receipt["receipt_sha256"])[:12]
    return ["", f"<details><summary>{t['receipt'].format(short=short)}</summary>", "",
            f"{fence}json", body, fence, "", "</details>"]
