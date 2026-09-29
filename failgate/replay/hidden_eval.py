"""隐藏考卷的误报评测（ADR 0021）：真实的上游修复会不会挂在隐藏题上。

对 ClaimVerify 正负例评测里的 9 个 black 正例（严格 FB/PA 成立）：

    issue 标题和正文（回放库）+ 当时的 L2 考卷 ──LLM──▶ 隐藏题
    在修复提交的父提交上挑题（必须按封存签名失败）──▶ 封存
    在修复提交上跑隐藏题 ──▶ 全部通过 = 没有误报；有题失败 = 误报（上游修复是对的）

上游修复是维护者合进去的、对这个 bug 的正确修复，所以它挂在隐藏题上，要么是题出错了
（期望和维护者的决定不一样），要么是题测到了修复之外的行为——都算误报。
抓"迎合考卷的修复"这一面由演示仓库 PR #7 说明（见 ADR 0021）。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from failgate.verify.hidden import HiddenWriter, run_hidden, seal_hidden

from .metrics import wilson
from .verify_eval import EvalCase


async def run_case(repo: str, case: EvalCase, *, title: str, body: str, bench: Any,
                   writer: HiddenWriter) -> dict[str, Any]:
    started = time.monotonic()
    writer.cost_usd = 0.0
    sealed = await seal_hidden(bench, writer, case.exam, repo=repo, title=title, body=body,
                               source_repo=repo, source_sha=case.parent)
    row: dict[str, Any] = {
        "number": case.number, "title": case.title, "rule": sealed.rule,
        "generated": len(sealed.generated), "dropped": sealed.dropped,
        "sealed": sealed.hidden is not None, "reason": sealed.reason,
        "kept": len(sealed.hidden.tests) if sealed.hidden else 0,
        "parent": case.parent, "fix": case.fix,
    }
    if sealed.hidden is not None:
        fix_env = await bench.prepare(repo, case.fix, case.exam)
        res = await run_hidden(bench, fix_env, case.exam, sealed.hidden)
        row |= {"on_fix": res.model_dump(mode="json"), "false_alarm": res.suspicious,
                "code": sealed.hidden.code, "tests": sealed.hidden.tests}
    row |= {"cost_usd": round(sealed.cost_usd, 5), "seconds": round(time.monotonic() - started, 1)}
    return row


def summarize(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    sealed = [r for r in rows if r["sealed"]]
    ran = [r for r in sealed if r.get("on_fix", {}).get("status") == "ok"]
    alarms = [r for r in ran if r["false_alarm"]]
    tests = sum(r["on_fix"]["total"] for r in ran)
    failed = sum(r["on_fix"]["failed"] for r in ran)
    return {
        "n": len(rows), "sealed": len(sealed), "ran": len(ran),
        "false_alarm": len(alarms),
        "false_alarm_ci": wilson(len(alarms), len(ran)) if ran else None,
        "tests": tests, "failed_tests": failed,
        "generated": sum(r["generated"] for r in rows),
        "kept": sum(r["kept"] for r in rows),
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        "seconds": round(sum(r["seconds"] for r in rows), 1),
    }


def render(rows: Sequence[dict[str, Any]], meta: dict[str, Any]) -> str:
    s = summarize(rows)
    ci = s["false_alarm_ci"]
    ci_text = f"，Wilson 95% 区间 {ci[0]:.0%}–{ci[1]:.0%}" if ci else ""
    lines = [
        f"# 隐藏考卷误报评测：{meta['repo']}",
        "",
        f"- 时间：{meta['started']}；来源：`{meta['source']}`（严格 FB/PA 成立的案例）；"
        f"出题模型：{meta['model']}",
        "- 在修复提交的父提交上出题并挑题，再在上游修复提交上跑；上游修复是对的，"
        "挂在隐藏题上就是误报",
        "",
        f"**结果：{s['n']} 个案例封存了 {s['sealed']} 份隐藏考卷（出题 {s['generated']} 道、"
        f"留下 {s['kept']} 道）；在上游修复上误报 {s['false_alarm']}/{s['ran']}{ci_text}；"
        f"逐题看，{s['tests']} 道里 {s['failed_tests']} 道没通过。"
        f"合计 ${s['cost_usd']}，{s['seconds']} 秒**",
        "",
        "| issue | 出题 | 留下 | 丢掉的原因 | 上游修复上 | 误报 | 花费 | 用时 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(rows, key=lambda r: r["number"]):
        drops = "；".join(f"{k.removeprefix('test_hidden_')}: {v}"
                         for k, v in r["dropped"].items()) or "—"
        on_fix = r.get("on_fix")
        fix_text = (f"{on_fix['passed']}/{on_fix['total']} 通过" if on_fix and
                    on_fix["status"] == "ok" else (on_fix or {}).get("reason", r["reason"]))
        alarm = "❌" if r.get("false_alarm") else ("—" if not r["sealed"] else "✅ 无")
        lines.append(f"| #{r['number']} | {r['generated']} | {r['kept']} | {drops} | "
                     f"{fix_text} | {alarm} | ${r['cost_usd']} | {r['seconds']}s |")
    lines += ["", "## 每个案例归纳的规律", ""]
    lines += [f"- #{r['number']} {r['title']}：{r['rule'] or '—'}"
              for r in sorted(rows, key=lambda r: r["number"])]
    alarms = [r for r in rows if r.get("false_alarm")]
    if alarms:
        lines += ["", "## 误报诊断（待逐条填写）", ""]
        lines += [f"- #{r['number']}：{r['on_fix']['failed']}/{r['on_fix']['total']} 道没通过"
                  for r in alarms]
    return "\n".join(lines) + "\n"
