"""汇总评论的渲染（技术方案附录 A）。每个 Case 只维护一条汇总评论，后续阶段完成后更新它。"""

from __future__ import annotations

from typing import Any

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
    },
}


def render_summary(intake: dict[str, Any], triage: dict[str, Any], cost_usd: float) -> str:
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
    missing = intake.get("missing") or []
    if triage["type"] == "bug" and missing:
        lines += ["", t["need"]]
        lines += [f"- [ ] {_MISSING_TEXT[lang].get(m, m)}" for m in missing]
    lines += ["", f"<sub>{t['cost']} ${cost_usd:.4f} · {t['footer']}</sub>"]
    return "\n".join(lines)
