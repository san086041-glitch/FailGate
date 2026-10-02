"""命令行的结果面板和表格（ADR 0038）：verify、cases、evidence、fix run。

文字沿用核验报告（verify/report.py）的翻译表（中文 / 英文都有，ADR 0039 按界面语言选），
终端和 GitHub 评论说法一致；颜色和工作台一致：通过绿、驳回红、无法判定黄、进行中蓝。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from failgate.i18n import lang, t

if TYPE_CHECKING:
    from failgate.db import Case, Repo
    from failgate.fix.agent import FixResult
    from failgate.verify.engine import ClaimResult, Verification
    from failgate.verify.store import EvidenceRef

VERDICT_STYLE = {"VERIFIED": "green", "REFUTED": "red", "INCONCLUSIVE": "yellow",
                 "NO_CLAIM": "dim"}
_VERDICT = {"VERIFIED": ("通过验收", "accepted"), "REFUTED": ("驳回", "refuted"),
            "INCONCLUSIVE": ("无法判定", "inconclusive"),
            "NO_CLAIM": ("没有声明修复", "no fix claimed")}
# 正在干活的状态：蓝；终态按结论上色；其余默认色
WORKING = frozenset({"INTAKE", "TRIAGING", "DEDUPING", "ANSWERING", "REPRODUCING", "FIXING",
                     "VERIFYING", "RESEALING", "REFIXING"})
STATE_STYLE = {"VERIFIED": "green", "REPRODUCED": "green", "ANSWERED": "green",
               "REFIXED": "green", "PR_OPENED": "green", "REFUTED": "red", "FAILED": "red",
               "INCONCLUSIVE": "yellow", "NEED_INFO": "yellow", "DUP_SUSPECTED": "yellow",
               "CLOSED": "dim", "IGNORED": "dim"}


def verdict_word(verdict: str) -> str:
    return t(*_VERDICT[verdict])


def state_text(state: str) -> Text:
    return Text(state, style="bold blue" if state in WORKING else STATE_STYLE.get(state, ""))


def _plain(text: str) -> str:
    """报告里的 Markdown 记号（反引号、加粗）在终端里去掉。"""
    return text.replace("`", "").replace("**", "")


def _mark(ok: bool | None) -> Text:
    if ok is None:
        return Text("·", style="yellow")
    return Text("✓", style="green") if ok else Text("✗", style="red")


def bar(rate: float, width: int = 10) -> str:
    full = round(max(0.0, min(1.0, rate)) * width)
    return "█" * full + "░" * (width - full)


# ---------------------------------------------------------------- verify


def _claim_table(c: ClaimResult, v: Verification) -> Table:
    from failgate.verify.report import _TEXT, _l1, _l3, _runs, _signal

    ui = lang()
    tx = _TEXT[ui]  # 报告的翻译表
    rows = Table.grid(padding=(0, 1))
    rows.add_column(width=1)
    rows.add_column(style="bold", no_wrap=True)
    rows.add_column()
    exam, tamper, regress = t("① 考卷", "① exam"), t("② 篡改", "② tamper"), t("③ 回归", "③ regress")
    if c.layer1 is None and c.layer2 is None:  # 没有考卷
        rows.add_row(_mark(None), exam, _plain(tx["no_exam"].format(issue=c.issue)))
        return rows
    if c.layer1 is not None:
        rows.add_row(_mark(c.layer1.status == "pass" if c.layer1.status != "inconclusive"
                           else None), exam, _plain(_l1(c.layer1, tx)))
        if c.layer1.base or c.layer1.head:
            rows.add_row("", "", Text(
                t("合并基点", "base") + f" {v.base_sha[:7]}: {_runs(c.layer1.base, ui)}"
                f"  →  PR {v.head_sha[:7]}: {_runs(c.layer1.head, ui)}", style="dim"))
    if c.layer2 is not None:
        if not c.layer2.signals:
            rows.add_row(_mark(True), tamper, t("没有改考卷、测试配置，也没有加 skip",
                                                "exam, test config and skips untouched"))
        for s in c.layer2.signals:
            rows.add_row(_mark(False if s.level == "high" else None), tamper,
                         f"{s.path}: {_plain(_signal(s, tx))} ({tx[s.level]})")
    if c.layer3 is not None:
        l3 = c.layer3
        ok = {"pass": True, "fail": False}.get(l3.status)
        rows.add_row(_mark(ok), regress, _plain(_l3(l3, tx)))
        for name in l3.new_failures[:5]:
            rows.add_row("", "", Text(name, style="red"))
    if c.strength is not None:
        st = c.strength
        label = t("强度", "strength")
        if st.status == "ok" and st.kill_rate is not None:
            style = {"strong": "green", "medium": "yellow", "weak": "red"}.get(st.grade, "")
            line = Text(bar(st.kill_rate) + " ", style=style)
            line.append(f"{tx[f'g.{st.grade}']} {st.killed}/{st.killed + st.survived}"
                        + t(f"（杀死率 {st.kill_rate:.0%}）", f" (kill rate {st.kill_rate:.0%})"))
            rows.add_row(_mark(st.grade != "weak" if st.grade != "medium" else None),
                         label, line)
        else:
            why = tx.get(f"s.{st.reason}", st.reason)
            rows.add_row(_mark(None), label, t(f"没有评估（{why}）", f"not assessed ({why})"))
    if c.hidden is not None:
        h = c.hidden
        label = t("隐藏考卷", "hidden exam")
        if h.status != "ok":
            why = tx.get(f"h.{h.reason}", h.reason)
            rows.add_row(_mark(None), label, t(f"没有跑成（{why}）", f"did not run ({why})"))
        elif h.suspicious:
            rows.add_row(_mark(False), label, Text(
                t(f"{h.failed}/{h.total} 道没有通过——疑似只迎合了公开考卷",
                  f"{h.failed}/{h.total} failed: likely tailored to the public exam"),
                style="red"))
        else:
            rows.add_row(_mark(True), label, t(f"{h.total} 道全部通过", f"all {h.total} passed"))
    return rows


def verification_panel(v: Verification) -> RenderableType:
    from failgate.verify.engine import ClaimVerdict

    verdict = v.verdict.value if v.verdict is not None else "NO_CLAIM"
    style = VERDICT_STYLE[verdict]
    title = Text(f" {verdict} ", style=f"bold reverse {style}")
    title.append(f"  {v.repo}#{v.pr}  {verdict_word(verdict)}", style=f"bold {style}")
    if not v.claims:
        body: RenderableType = Text(t(
            "这个 PR 没有声明修复任何 issue（没有 fixes #N），没有可核验的考卷。",
            "This PR claims no fix (no `fixes #N`), so there is no exam to check."))
    else:
        parts: list[RenderableType] = []
        for c in v.claims:
            head = Text(t(f"声称修复 #{c.issue}  ", f"Claims to fix #{c.issue}  "), style="bold")
            head.append(verdict_word(c.verdict.value), style=VERDICT_STYLE[c.verdict.value])
            parts += [head, _claim_table(c, v), Text("")]
        if v.verdict == ClaimVerdict.VERIFIED:
            parts.append(Text(t("通过验收测试 + 无篡改 + 无新增回归（不等于\"修复一定正确\"）",
                                "Passed the acceptance test, no tampering, no new regressions "
                                "(not a proof the fix is correct)"), style="dim"))
        body = Group(*parts)
    return Panel(body, title=title, title_align="left", border_style=style,
                 subtitle=t("合并基点", "base") + f" {v.base_sha[:7]} → head {v.head_sha[:7]}",
                 subtitle_align="right")


# ---------------------------------------------------------------- cases / evidence


def _when(at: datetime) -> str:
    return f"{at:%m-%d %H:%M}"


def cases_table(rows: list[tuple[Case, Repo]]) -> Table:
    table = Table(header_style="bold", box=None, pad_edge=False)
    columns: tuple[tuple[str, dict[str, Any]], ...] = (
               ("#", {"justify": "right", "style": "dim"}), (t("仓库", "Repo"), {}),
               (t("类型", "Kind"), {}),
               (t("标题", "Title"), {"overflow": "ellipsis", "max_width": 48}),
               (t("状态", "State"), {}), (t("花费", "Spent"), {"justify": "right"}),
               (t("更新", "Updated"), {"style": "dim"}))
    for col, kw in columns:
        table.add_column(col, no_wrap=True, **kw)  # type: ignore[arg-type]
    for c, r in rows:
        kind = "PR" if c.kind == "pull" else "issue"
        table.add_row(str(c.id), f"{r.full_name}#{c.number}", kind, c.title or "",
                      state_text(c.state), f"${c.spent_usd:.4f}", _when(c.updated_at))
    return table


def evidence_table(refs: list[EvidenceRef]) -> Table:
    table = Table(header_style="bold", box=None, pad_edge=False)
    for col in ("ID", "issue", t("等级", "Level"), t("考卷", "Exam"), t("判定", "Verdict"),
                t("测试文件", "Test file"), t("封存时间", "Sealed")):
        table.add_column(col, no_wrap=True)
    for ref in refs:
        ev = ref.evidence
        exam = (Text(t("考卷", "exam"), style="green") if ev.acceptance
                else Text(t("非考卷", "not an exam"), style="dim"))
        ident = Text(ev.id[:12], style="dim" if ev.superseded_by else "")
        if ev.superseded_by:
            ident.append(f" → {ev.superseded_by[:8]}", style="dim")
        table.add_row(ident, f"{ref.repo}#{ref.issue}", ev.level, exam,
                      state_text(ev.verdict), ev.test_path, _when(ev.created_at))
    return table


def audit_lines(problems: list[str], receipt_sha: str, test_sha: str) -> Text:
    if problems:
        out = Text()
        for p in problems:
            out.append(f"✗ {p}\n", style="red")
        return out
    return Text(t("✓ 哈希一致：", "✓ hashes match: ")
                + f"receipt {receipt_sha[:12]} · test {test_sha[:12]}", style="green")


# ---------------------------------------------------------------- fix run


def fix_panel(number: int, res: FixResult, *, control: bool) -> Panel:
    ok = res.passed
    style = "green" if ok else ("yellow" if res.patch else "red")
    title = Text(f" {res.status} ", style=f"bold reverse {style}")
    arm = (t("对照组（不给考卷）", "control (no exam)") if control
           else t("实验组（给考卷）", "treatment (with exam)"))
    title.append(f"  #{number} {arm}", style=f"bold {style}")
    rows = Table.grid(padding=(0, 2))
    rows.add_column(style="dim", no_wrap=True)
    rows.add_column()
    rows.add_row(t("结果", "Result"), Text(
        t("封存考卷在全新工作区里通过（不等于修对了，金标准见 replay fix）",
          "sealed exam passes in a fresh workspace (not proof of a correct fix; "
          "gold standard: replay fix)") if ok
        else t("没有通过封存考卷", "did not pass the sealed exam"), style=style))
    rows.add_row(t("过程", "Run"), t(
        f"{len(res.attempts)} 轮 · {res.steps} 步 · {res.duration_s:.0f} 秒 · "
        f"被拒写入 {res.denied} 次",
        f"{len(res.attempts)} round(s) · {res.steps} steps · {res.duration_s:.0f}s · "
        f"{res.denied} denied write(s)"))
    rows.add_row(t("花费", "Cost"), f"${res.cost_usd:.4f} · " + t(
        f"输入 {res.prompt_tokens:,}（缓存 {res.cached_tokens:,}）· 输出 "
        f"{res.completion_tokens:,}（推理 {res.reasoning_tokens:,}）",
        f"in {res.prompt_tokens:,} (cached {res.cached_tokens:,}) · out "
        f"{res.completion_tokens:,} (reasoning {res.reasoning_tokens:,})"))
    rows.add_row(t("改动", "Files"), ", ".join(res.files) or t("无", "none"))
    first = res.first_edit_step if res.first_edit_step is not None else "—"
    rows.add_row(t("交接", "Handoff"), t(
        f"{res.handoff}：修改阶段读 {res.edit_reads} 次（重读 {res.rereads}），"
        f"第一次编辑在第 {first} 步",
        f"{res.handoff}: {res.edit_reads} reads while editing ({res.rereads} re-reads), "
        f"first edit at step {first}"))
    tools: dict[str, Any] = dict(sorted(res.tool_counts.items(), key=lambda kv: -kv[1]))
    if tools:
        rows.add_row(t("工具", "Tools"), " · ".join(f"{k} {v}" for k, v in tools.items()))
    if res.error:
        rows.add_row(t("错误", "Error"), Text(res.error, style="red"))
    if res.give_up_reason:
        rows.add_row(t("放弃", "Gave up"), res.give_up_reason)
    if res.transcript_path:
        rows.add_row(t("记录", "Transcript"), res.transcript_path)
    return Panel(rows, title=title, title_align="left", border_style=style)
