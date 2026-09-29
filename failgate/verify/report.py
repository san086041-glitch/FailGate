"""核验报告：PR 上的一条评论（每个 PR 一条，之后只编辑），命令行也打印它。

引擎和收据里的理由都是代码（语言无关），这里按 PR 的语言翻译成中文或英文。
措辞刻意保守：通过验收 = 通过验收测试 + 未发现篡改 + 无新增回归，不说"修复正确"。
"""

from __future__ import annotations

import json
import re
from typing import Any

from .engine import ClaimResult, ClaimVerdict, ExamRun, Layer1, Layer3, Verification
from .strength import StrengthReport
from .tamper import Signal

_CJK = re.compile(r"[一-鿿]")

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
        "why": "原因：",
        "sep": "；",
        "exam": "① 封存考卷 `{path}`（sha256 `{sha}`）：{reason}",
        "runs": "合并基点 `{base}`：{b}；PR `{head}`：{h}",
        "tamper_none": "② 篡改：未发现",
        "tamper": "② 篡改：",
        "related": "③ 相关测试：{reason}",
        "new_fail": "新出现的失败：",
        "local": "本地复验：`failgate verify {repo}#{pr}`",
        "receipt": "核验收据 `{short}`",
        "reseal_hint": (
            "如果是 issue 的期望本身写错了、PR 里的测试才是对的，有写权限的维护者可以评论 "
            "`/failgate reseal`，用 PR 里的版本重新封存考卷。"
        ),
        "high": "高危",
        "medium": "需要维护者留意",
        # 理由
        "no_exam": "#{issue} 没有封存的考卷（L2 测试），无法核验；可以先在 #{issue} 上复现",
        "l1.pass": "合并基点上 {nb}/{nb} 次出现封存的失败，PR 上 {nh}/{nh} 次通过",
        "l1.head_failed": "考卷在 PR 的代码上仍然失败",
        "l1.head_invalid": "考卷在 PR 的代码上跑不起来（收集出错）",
        "l1.head_skipped": "考卷在 PR 的代码上被跳过或标成了 xfail，没有真正执行",
        "l1.head_flaky": "考卷在 PR 的代码上时过时不过",
        "l1.base_passed": "考卷在合并基点上就通过了：可能已经在主分支上修好，或者代码变了",
        "l1.base_other": "考卷在合并基点上失败了，但不是封存时的那个失败",
        "l1.base_invalid": "考卷在合并基点上跑不起来（收集出错）",
        "l1.base_mixed": "考卷在合并基点上的结果不一致",
        "l1.infra": "有运行超时或内存超限，不能作为证据",
        "l1.setup": "环境搭不起来（{detail}）",
        "l3.pass": "{n} 个相关测试文件在 PR 的代码上没有新增失败",
        "l3.new_failures": "相关测试里有 {n} 个在 PR 的代码上新出现失败",
        "l3.none": "没找到和改动相关的已有测试",
        "l3.base_infra": "相关测试在合并基点上超时或内存超限",
        "l3.head_infra": "相关测试在 PR 的代码上超时或内存超限",
        "l3.setup": "环境搭不起来，没有跑相关测试",
        "t.exam_removed": "PR 删除了封存的考卷",
        "t.exam_renamed": "PR 把考卷改名为 `{note}`",
        "t.exam_modified": "PR 里的考卷和封存的版本不一致",
        "t.conftest": "改动了 conftest.py（可能影响测试的收集和运行）",
        "t.pytest_config": "改动了 pytest 配置",
        "t.test_removed": "删除了测试文件",
        "t.skip_added": "给测试加了 skip / xfail",
        # 考卷强度
        "s.ok": ("④ 考卷强度：**{grade}**　把修复改动过的代码悄悄改坏 {valid} 次，"
                 "考卷发现了 {killed} 次（{rate}；断言失败 {a}、崩溃 {c}、超时 {t}）"),
        "s.scope": "  修复改动了 {changed} 行源码，考卷执行到其中 {executed} 行",
        "s.unexec": "  考卷没有执行到的改动：{lines}",
        "s.survivors": "  考卷察觉不到的改动：",
        "s.weak": "  考卷偏弱：通过验收只能说明 issue 里描述的症状消失了",
        "s.caveat": ("  强度只看修复改动过的行：看不出修复漏掉了哪些情况，"
                     "等价变异体也会让杀死率偏低"),
        "s.na": "④ 考卷强度：没有评估（{reason}）",
        "g.strong": "强", "g.medium": "中", "g.weak": "弱",
        "s.no_source_change": "PR 没有改动源码文件",
        "s.not_executed": "考卷没有执行到修复改动的任何一行",
        "s.shadow": "测试导入的不是工作区里的源码副本，无法注入变异体",
        "s.baseline_failed": "带 coverage 的环境里考卷没有通过",
        "s.parse_error": "源码解析失败",
        "s.no_mutants": "改动的行上没有可以变异的代码",
        "s.setup": "带 coverage 的环境搭不起来",
        # 隐藏考卷
        "h.ok": "⑤ 隐藏考卷（sha256 `{sha}`）：{total} 道全部通过",
        "h.suspicious": ("⑤ 隐藏考卷（sha256 `{sha}`）：**{failed}/{total} 道没有通过**——"
                         "通过了公开考卷，但同一个 bug 换个输入就不行，疑似只迎合了公开考卷"),
        "h.hint": ("  隐藏考卷是封存考卷时只根据 issue 出的变体题，题目不公开；维护者可以用 "
                   "`failgate hidden show` 在本地查看。它可能出错，所以只作提示，不改变结论"),
        "h.na": "⑤ 隐藏考卷：没有跑成（{reason}）",
        "h.infra": "超时或内存超限", "h.invalid": "跑不起来",
    },
    "en": {
        "title": "🛡️ **FailGate verification: PR #{pr}**",
        "no_claim": (
            "This PR does not claim to fix any issue (no `fixes #N` in the title or "
            "description), so there is no acceptance test to check it against."
        ),
        "claim": "**Claims to fix #{issue}**　{badge}",
        "VERIFIED": (
            "✅ Accepted: passes the acceptance test, no tampering found, no new failures "
            "(this does not mean the fix is necessarily correct)"
        ),
        "REFUTED": "❌ Rejected",
        "INCONCLUSIVE": "⚪ Inconclusive",
        "why": "Reasons: ",
        "sep": "; ",
        "exam": "① Sealed acceptance test `{path}` (sha256 `{sha}`): {reason}",
        "runs": "merge base `{base}`: {b}; PR `{head}`: {h}",
        "tamper_none": "② Tampering: none found",
        "tamper": "② Tampering:",
        "related": "③ Related tests: {reason}",
        "new_fail": "New failures:",
        "local": "Re-run locally: `failgate verify {repo}#{pr}`",
        "receipt": "Verification receipt `{short}`",
        "reseal_hint": (
            "If the expectation in the issue was wrong and the test in this PR is the right "
            "one, a maintainer with write access can comment `/failgate reseal` to seal the "
            "PR's version as the new acceptance test."
        ),
        "high": "high risk",
        "medium": "for the maintainer to review",
        "no_exam": "#{issue} has no sealed acceptance test (L2), so it cannot be verified yet",
        "l1.pass": "the sealed failure occurred {nb}/{nb} times on the merge base; the test "
                   "passed {nh}/{nh} times on the PR",
        "l1.head_failed": "the acceptance test still fails on the PR",
        "l1.head_invalid": "the acceptance test cannot run on the PR (collection error)",
        "l1.head_skipped": "the acceptance test was skipped or marked xfail on the PR, so it "
                           "never actually ran",
        "l1.head_flaky": "the acceptance test passes only intermittently on the PR",
        "l1.base_passed": "the acceptance test already passes on the merge base: the bug may "
                          "have been fixed on the main branch, or the code changed",
        "l1.base_other": "the acceptance test fails on the merge base, but not with the sealed "
                         "failure",
        "l1.base_invalid": "the acceptance test cannot run on the merge base (collection error)",
        "l1.base_mixed": "the acceptance test gives inconsistent results on the merge base",
        "l1.infra": "a run timed out or ran out of memory, so it cannot count as evidence",
        "l1.setup": "the environment could not be built ({detail})",
        "l3.pass": "{n} related test file(s) show no new failures on the PR",
        "l3.new_failures": "{n} related test(s) newly fail on the PR",
        "l3.none": "no existing tests related to the change were found",
        "l3.base_infra": "related tests timed out or ran out of memory on the merge base",
        "l3.head_infra": "related tests timed out or ran out of memory on the PR",
        "l3.setup": "the environment could not be built, so related tests were not run",
        "t.exam_removed": "the PR deletes the sealed acceptance test",
        "t.exam_renamed": "the PR renames the acceptance test to `{note}`",
        "t.exam_modified": "the acceptance test in the PR differs from the sealed version",
        "t.conftest": "changes conftest.py (may affect how tests are collected and run)",
        "t.pytest_config": "changes pytest configuration",
        "t.test_removed": "deletes a test file",
        "t.skip_added": "adds skip / xfail to tests",
        "s.ok": ("④ Test strength: **{grade}**. The changed code was quietly broken {valid} "
                 "times and the acceptance test noticed {killed} ({rate}; assertion {a}, "
                 "crash {c}, timeout {t})"),
        "s.scope": ("  The fix changes {changed} source line(s); the test executes "
                    "{executed} of them"),
        "s.unexec": "  Changed lines the test never executes: {lines}",
        "s.survivors": "  Changes the test cannot detect:",
        "s.weak": ("  Weak test: passing it only shows that the symptom described in the issue "
                   "is gone"),
        "s.caveat": ("  Strength only looks at the lines the fix changed: it cannot see cases the "
                     "fix misses, and equivalent mutants make the score a lower bound"),
        "s.na": "④ Test strength: not assessed ({reason})",
        "g.strong": "strong", "g.medium": "medium", "g.weak": "weak",
        "s.no_source_change": "the PR changes no source files",
        "s.not_executed": "the acceptance test executes none of the changed lines",
        "s.shadow": "tests import an installed copy, not the workspace source, so mutants "
                    "cannot be injected",
        "s.baseline_failed": "the acceptance test did not pass in the coverage environment",
        "s.parse_error": "the source could not be parsed",
        "s.no_mutants": "no mutable code on the changed lines",
        "s.setup": "the coverage environment could not be built",
        "h.ok": "⑤ Hidden tests (sha256 `{sha}`): all {total} passed",
        "h.suspicious": ("⑤ Hidden tests (sha256 `{sha}`): **{failed}/{total} failed**: the "
                         "PR passes the public acceptance test but not the same bug with other "
                         "inputs, so it may only fit the public test"),
        "h.hint": ("  Hidden tests are variants written from the issue alone when the acceptance "
                   "test was sealed; they are not published (maintainers can run "
                   "`failgate hidden show` locally). They can be wrong, so this is only a hint "
                   "and does not change the verdict"),
        "h.na": "⑤ Hidden tests: could not run ({reason})",
        "h.infra": "timeout or out of memory", "h.invalid": "could not run",
    },
}

_OUTCOME = {
    "zh": {"failed_same": "封存的失败", "failed_other": "别的失败", "passed": "通过",
           "skipped": "被跳过", "invalid": "跑不起来", "infra": "超时 / 内存超限"},
    "en": {"failed_same": "the sealed failure", "failed_other": "a different failure",
           "passed": "passed", "skipped": "skipped", "invalid": "could not run",
           "infra": "timeout / OOM"},
}


def language_of(*texts: str | None) -> str:
    """PR 标题和正文里有中文就用中文，否则英文。"""
    return "zh" if any(t and _CJK.search(t) for t in texts) else "en"


def _l1(layer: Layer1, t: dict[str, str]) -> str:
    return t[f"l1.{layer.reason}"].format(nb=len(layer.base), nh=len(layer.head),
                                          detail=layer.detail)


def _l3(layer: Layer3, t: dict[str, str]) -> str:
    n = len(layer.new_failures) if layer.reason == "new_failures" else len(layer.files)
    return t[f"l3.{layer.reason}"].format(n=n)


def _signal(s: Signal, t: dict[str, str]) -> str:
    return t[f"t.{s.kind}"].format(note=s.note)


def reason_text(code: str, c: ClaimResult, t: dict[str, str]) -> str:
    if code == "no_exam":
        return t["no_exam"].format(issue=c.issue)
    layer, _, name = code.partition(":")
    if layer == "tamper" and c.layer2 is not None:
        sig = next((s for s in c.layer2.signals if s.kind == name), None)
        return _signal(sig, t) if sig else name
    if layer == "layer1" and c.layer1 is not None:
        return _l1(c.layer1, t)
    if layer == "layer3" and c.layer3 is not None:
        return _l3(c.layer3, t)
    return code


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
        lines.append(t["why"] + t["sep"].join(reason_text(r, c, t) for r in c.reasons))
    if c.layer1 is not None:
        lines.append("- " + t["exam"].format(path=c.test_path, sha=(c.test_sha256 or "")[:12],
                                             reason=_l1(c.layer1, t)))
        if c.layer1.base or c.layer1.head:
            lines.append("  " + t["runs"].format(base=v.base_sha[:7], head=v.head_sha[:7],
                                                 b=_runs(c.layer1.base, lang),
                                                 h=_runs(c.layer1.head, lang)))
    if c.layer2 is not None:
        if not c.layer2.signals:
            lines.append("- " + t["tamper_none"])
        else:
            lines.append("- " + t["tamper"])
            lines += [f"  - `{s.path}`: {_signal(s, t)} ({t[s.level]})"
                      for s in c.layer2.signals]
    if c.layer3 is not None:
        lines.append("- " + t["related"].format(reason=_l3(c.layer3, t)))
        if c.layer3.new_failures:
            lines.append("  " + t["new_fail"] + " " + ", ".join(
                f"`{n}`" for n in c.layer3.new_failures[:10]))
    if c.strength is not None:
        lines += _strength_lines(c.strength, t)
    if c.hidden is not None:
        h = c.hidden
        sha = (h.test_sha256 or "")[:12]
        if h.status != "ok":
            lines.append("- " + t["h.na"].format(reason=t.get(f"h.{h.reason}", h.reason)))
        elif h.suspicious:
            lines += ["- " + t["h.suspicious"].format(sha=sha, failed=h.failed, total=h.total),
                      t["h.hint"]]
        else:
            lines.append("- " + t["h.ok"].format(sha=sha, total=h.total))
    if any(r == "tamper:exam_modified" for r in c.reasons):
        lines.append(t["reseal_hint"])
    return lines


def _strength_lines(s: StrengthReport, t: dict[str, str]) -> list[str]:
    if s.status != "ok" or s.kill_rate is None:
        return ["- " + t["s.na"].format(reason=t.get(f"s.{s.reason}", s.reason))]
    lines = ["- " + t["s.ok"].format(
        grade=t[f"g.{s.grade}"], valid=s.killed + s.survived, killed=s.killed,
        rate=f"{s.kill_rate:.0%}", a=s.killed_assert, c=s.killed_crash, t=s.killed_timeout)]
    lines.append(t["s.scope"].format(changed=s.changed_lines, executed=s.executed_lines))
    if s.unexecuted:
        lines.append(t["s.unexec"].format(lines=", ".join(f"`{x}`" for x in s.unexecuted[:5])))
    if s.survivors:
        lines.append(t["s.survivors"])
        lines += [f"  - `{m.path}:{m.line}` `{_code(m.before)}` → `{_code(m.after)}`"
                  for m in s.survivors[:3]]
    if s.grade == "weak":
        lines.append(t["s.weak"])
    lines.append(t["s.caveat"])
    return lines


def _code(text: str) -> str:
    """放进行内代码的一行源码：去掉反引号（防止逃出代码格式），过长截断。"""
    text = text.replace("`", "'")
    return text if len(text) <= 80 else text[:77] + "..."


def render_verification(v: Verification, lang: str = "en") -> str:
    lang = "zh" if lang == "zh" else "en"
    t = _TEXT[lang]
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
