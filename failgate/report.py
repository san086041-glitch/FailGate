"""汇总评论的渲染（技术方案附录 A）。每个 Case 只维护一条汇总评论，后续阶段完成后更新它。"""

from __future__ import annotations

import re
from typing import Any

_MENTION = re.compile(r"(?<![\w`])@(?=[A-Za-z0-9])")

_MISSING_TEXT = {
    "zh": {
        "version": "出问题的版本号",
        "environment": "运行环境（操作系统、Python / Node 版本、相关依赖版本）",
        "repro_steps": "复现步骤或最小示例代码",
        "expected_behavior": "预期行为",
        "actual_behavior": "实际行为",
        "error_output": "完整的报错信息或堆栈",
    },
    "en": {
        "version": "the version where the problem occurs",
        "environment": "environment (OS, Python / Node version, relevant dependency versions)",
        "repro_steps": "steps to reproduce or a minimal code example",
        "expected_behavior": "expected behavior",
        "actual_behavior": "actual behavior",
        "error_output": "the full error message or traceback",
    },
}

_TEXT = {
    "zh": {
        "title": "🛡️ **FailGate 验收报告**",
        "triage": "分诊",
        "type": "类型",
        "labels": "标签",
        "priority": "优先级",
        "confidence": "置信度",
        "why": "依据",
        "need": "为了尽快定位问题，请补充以下信息：",
        "cost": "花费",
        "footer": "这条回复有帮助吗？请用 👍 / 👎 反馈。",
        "none": "无",
        "dup": "可能与 {ref} 重复（相似度 {score:.2f}）：{reason}",
        "dup_hint": "如确认重复，维护者可以直接关闭本 issue；FailGate 不会自动关闭。",
        "related": "相关 issue：",
        "answer": "回答",
        "answer_note": "（根据项目文档和维护者以往的回答自动整理，未经维护者确认）",
        "refs": "参考资料：",
        "no_answer": "暂时没有在项目文档和历史 issue 中找到可靠的依据，请等待维护者回复。",
        "repro": "复现",
        "repro_py": "（Python {py}）",
        "repro_ok": (
            "✅ 已在 {env}上复现（证据等级 {level}）：{runs} 次运行都出现与报告一致的失败。"
        ),
        "repro_flaky": (
            "⚠️ 已在 {env}上复现，但不稳定（证据等级 {level}）：{runs} 次运行中约 {rate} 失败。"
        ),
        "repro_sub": (
            "报告的版本 `{sub}` 无法从 PyPI 安装，这是在 issue 提交前最新的正式版上复现的。"
        ),
        "repro_fixed": "在最新版 `{latest}` 上没有复现，可能已经修复，建议升级后确认。",
        "repro_still": "在最新版 `{latest}` 上仍然存在。",
        "repro_script": "复现脚本",
        "repro_miss": "尝试在 {env}上自动复现，暂时没有成功。",
        "repro_retry": "补充可以直接运行的最小示例代码和完整报错后，FailGate 会重新尝试。",
        "repro_version": (
            "没能根据报告里的版本号安装对应的发布版本，"
            "请确认准确的版本号（例如 `pip show {pkg}` 的输出）。"
        ),
        "repro_internal": "暂时无法自动复现，维护者会跟进。",
        "repro_src_at": "（issue 创建时的源码{py}）",
        "repro_src_py": "，Python {py}",
        "repro_test": "测试文件 `{path}` 可以直接合进仓库：修复之前失败，修复之后应当通过。",
        "repro_test_title": "仓库内的失败测试",
        "repro_src_miss": (
            "尝试在 {env}上写一个会失败的测试，暂时没有成功：可能已经修复，也可能需要更多信息。"
        ),
    },
    "en": {
        "title": "🛡️ **FailGate acceptance report**",
        "triage": "Triage",
        "type": "type",
        "labels": "labels",
        "priority": "priority",
        "confidence": "confidence",
        "why": "Why",
        "need": "To help us reproduce this, please add:",
        "cost": "cost",
        "footer": "Was this helpful? React with 👍 / 👎.",
        "none": "none",
        "dup": "Possible duplicate of {ref} (similarity {score:.2f}): {reason}",
        "dup_hint": (
            "If confirmed, a maintainer can close this issue. FailGate never closes issues."
        ),
        "related": "Related issues:",
        "answer": "Answer",
        "answer_note": (
            " (compiled from the project docs and past maintainer replies; "
            "not yet confirmed by a maintainer)"
        ),
        "refs": "Sources:",
        "no_answer": (
            "I couldn't find a reliable source in the docs or past issues. "
            "A maintainer will follow up."
        ),
        "repro": "Reproduction",
        "repro_py": " with Python {py}",
        "repro_ok": (
            "✅ Reproduced on {env} (evidence level {level}): the reported failure occurred "
            "in all {runs} runs."
        ),
        "repro_flaky": (
            "⚠️ Reproduced on {env}, but flaky (evidence level {level}): about {rate} of "
            "{runs} runs failed."
        ),
        "repro_sub": (
            "The reported version `{sub}` is not installable from PyPI, so this was reproduced "
            "on the latest release published before the issue was opened."
        ),
        "repro_fixed": (
            "Not reproduced on the latest release `{latest}`: this may already be fixed; "
            "please try upgrading."
        ),
        "repro_still": "Still reproduces on the latest release `{latest}`.",
        "repro_script": "Reproduction script",
        "repro_miss": "Tried to reproduce this automatically on {env}, without success so far.",
        "repro_retry": (
            "If you add a minimal runnable example and the full error output, "
            "FailGate will try again."
        ),
        "repro_version": (
            "Could not install a release matching the reported version. Please confirm the "
            "exact version (e.g. the output of `pip show {pkg}`)."
        ),
        "repro_internal": (
            "Automatic reproduction is not available right now; a maintainer will follow up."
        ),
        "repro_src_at": " (source at the time this issue was opened{py})",
        "repro_src_py": ", Python {py}",
        "repro_test": (
            "The test file `{path}` can be added to the repository as is: it fails before a "
            "fix and should pass after it."
        ),
        "repro_test_title": "Failing test for the repository",
        "repro_src_miss": (
            "Tried to write a failing test on {env}, without success so far: this may already "
            "be fixed, or more information may be needed."
        ),
    },
}


def render_summary(
    intake: dict[str, Any],
    triage: dict[str, Any],
    cost_usd: float,
    dedup: dict[str, Any] | None = None,
    answer: dict[str, Any] | None = None,
    repro: dict[str, Any] | None = None,
) -> str:
    lang = "zh" if intake.get("language") == "zh" else "en"
    t = _TEXT[lang]
    labels = ", ".join(f"`{x}`" for x in triage.get("labels") or []) or t["none"]
    lines = [
        t["title"],
        "",
        f"**{t['triage']}**　{t['type']}: `{triage['type']}` · {t['labels']}: {labels} · "
        f"{t['priority']}: {triage['priority']} · {t['confidence']}: {triage['confidence']:.2f}",
        f"{t['why']}{'：' if lang == 'zh' else ': '}{triage['rationale']}",
    ]
    if dedup and dedup.get("verdict") in {"duplicate", "related"}:
        cands = dedup.get("candidates") or []
        if dedup["verdict"] == "duplicate":
            top = cands[0]
            dup = t["dup"].format(ref=f"#{top['number']}", score=top["score"], reason=top["reason"])
            lines += ["", f"**{dup}**", t["dup_hint"]]
            cands = cands[1:]
        related = [c for c in cands if c.get("level") in {"duplicate", "related"}][:3]
        if related:
            fmt = "#{n}（{s:.2f}）" if lang == "zh" else "#{n} ({s:.2f})"
            refs = " · ".join(fmt.format(n=c["number"], s=c["score"]) for c in related)
            lines += ["", f"{t['related']} {refs}"]
    if answer is not None:
        lines += ["", *_answer_lines(answer, t)]
    reproduced = repro is not None and repro.get("level") in PROVEN
    if repro is not None and repro.get("attempted"):
        lines += ["", *_repro_lines(repro, t)]
    missing = intake.get("missing") or []
    # 已经复现了就不用再向提问者要信息
    if triage["type"] == "bug" and missing and not reproduced:
        lines += ["", t["need"]]
        lines += [f"- [ ] {_MISSING_TEXT[lang].get(m, m)}" for m in missing]
    lines += ["", f"<sub>{t['cost']} ${cost_usd:.4f} · {t['footer']}</sub>"]
    return "\n".join(lines)


PROVEN = frozenset({"L1", "L2", "L3"})
MAX_SCRIPT_LINES = 80


def _repro_lines(repro: dict[str, Any], t: dict[str, str]) -> list[str]:
    if repro.get("mode") == "source":
        return _source_lines(repro, t)
    head = f"**{t['repro']}**　"
    pkg = repro.get("package") or ""
    env = f"`{pkg}=={repro.get('reported_version')}`"
    if repro.get("python"):
        env += t["repro_py"].format(py=repro["python"])
    if repro.get("level") in PROVEN:
        if repro.get("verdict") == "FLAKY":
            rate = f"{(repro.get('fail_rate') or 0) * 100:.0f}%"
            first = t["repro_flaky"].format(
                env=env, level=repro["level"], runs=repro.get("runs"), rate=rate
            )
        else:
            first = t["repro_ok"].format(env=env, level=repro["level"], runs=repro.get("runs"))
        lines = [head + first]
        if repro.get("substituted_for"):
            lines.append(t["repro_sub"].format(sub=_inline(repro["substituted_for"])))
        latest = repro.get("latest_version")
        if repro.get("fixed_in_latest") is True:
            lines.append(t["repro_fixed"].format(latest=latest))
        elif repro.get("fixed_in_latest") is False:
            lines.append(t["repro_still"].format(latest=latest))
        if repro.get("script"):
            lines += ["", *_script_block(repro["script"], t["repro_script"])]
        return lines
    if repro.get("public_error"):
        return [head + t["repro_version"].format(pkg=pkg)]
    if repro.get("reported_version") and repro.get("agent_status"):
        return [head + t["repro_miss"].format(env=env) + " " + t["repro_retry"]]
    return [head + t["repro_internal"]]


def _source_lines(repro: dict[str, Any], t: dict[str, str]) -> list[str]:
    """source 模式（L2）：在 issue 创建时的源码上写了仓库内的失败测试。"""
    head = f"**{t['repro']}**　"
    sha = (repro.get("source_sha") or "")[:7]
    py = t["repro_src_py"].format(py=repro["python"]) if repro.get("python") else ""
    env = f"`{_inline(repro.get('source_repo') or '')}@{sha}`" + t["repro_src_at"].format(py=py)
    if repro.get("level") in PROVEN:
        if repro.get("verdict") == "FLAKY":
            rate = f"{(repro.get('fail_rate') or 0) * 100:.0f}%"
            first = t["repro_flaky"].format(
                env=env, level=repro["level"], runs=repro.get("runs"), rate=rate
            )
        else:
            first = t["repro_ok"].format(env=env, level=repro["level"], runs=repro.get("runs"))
        lines = [head + first]
        if repro.get("test_path"):
            lines.append(t["repro_test"].format(path=_inline(repro["test_path"])))
        if repro.get("script"):
            lines += ["", *_script_block(repro["script"], t["repro_test_title"])]
        return lines
    if repro.get("agent_status"):
        return [head + t["repro_src_miss"].format(env=env) + " " + t["repro_retry"]]
    return [head + t["repro_internal"]]


def _script_block(script: str, title: str) -> list[str]:
    """折叠起来的代码块。围栏比脚本里最长的一串反引号还长，脚本内容就跑不出代码块。"""
    lines = script.rstrip().splitlines()
    if len(lines) > MAX_SCRIPT_LINES:
        lines = lines[:MAX_SCRIPT_LINES] + ["# …"]
    longest = max((len(m) for m in re.findall(r"`+", script)), default=0)
    fence = "`" * max(3, longest + 1)
    return [
        f"<details><summary>{title}</summary>", "", f"{fence}python", *lines, fence, "",
        "</details>",
    ]


def _inline(text: str) -> str:
    """放进行内代码里的短文本：去掉反引号和换行，限制长度。"""
    return text.replace("`", "'").replace("\n", " ")[:80]


def answer_section(answer: dict[str, Any], language: str) -> str:
    """单独渲染回答部分（CLI 试跑用）。"""
    return "\n".join(_answer_lines(answer, _TEXT["zh" if language == "zh" else "en"]))


def _answer_lines(answer: dict[str, Any], t: dict[str, str]) -> list[str]:
    """已回答：答案 + 参考资料列表；拒答或没通过检查：一句"等待维护者"，不给半成品答案。"""
    if answer.get("status") != "answered":
        return [f"**{t['answer']}**　{t['no_answer']}"]
    lines = [f"**{t['answer']}**<sub>{t['answer_note']}</sub>", "", answer["answer_md"], ""]
    lines.append(t["refs"])
    lines += [f"{r['n']}. [{_md_text(r['title'])}]({r['url']})" for r in answer["references"]]
    return lines


def _md_text(text: str) -> str:
    """资料标题可能来自别人写的 issue。

    转义方括号，免得破坏链接格式；让 @ 提及失效，免得通知到别人。
    """
    text = text.replace("[", "\\[").replace("]", "\\]")
    return _MENTION.sub("@\u200b", text)
