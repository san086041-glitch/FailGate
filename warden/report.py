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
        "title": "🛡️ **RepoWarden 值班报告**",
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
        "dup_hint": "如确认重复，维护者可以直接关闭本 issue；RepoWarden 不会自动关闭。",
        "related": "相关 issue：",
        "answer": "回答",
        "answer_note": "（根据项目文档和维护者以往的回答自动整理，未经维护者确认）",
        "refs": "参考资料：",
        "no_answer": "暂时没有在项目文档和历史 issue 中找到可靠的依据，请等待维护者回复。",
    },
    "en": {
        "title": "🛡️ **RepoWarden triage report**",
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
            "If confirmed, a maintainer can close this issue. RepoWarden never closes issues."
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
    },
}


def render_summary(
    intake: dict[str, Any],
    triage: dict[str, Any],
    cost_usd: float,
    dedup: dict[str, Any] | None = None,
    answer: dict[str, Any] | None = None,
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
    missing = intake.get("missing") or []
    if triage["type"] == "bug" and missing:
        lines += ["", t["need"]]
        lines += [f"- [ ] {_MISSING_TEXT[lang].get(m, m)}" for m in missing]
    lines += ["", f"<sub>{t['cost']} ${cost_usd:.4f} · {t['footer']}</sub>"]
    return "\n".join(lines)


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
