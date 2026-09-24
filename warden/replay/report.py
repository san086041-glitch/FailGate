"""把一次回放评测渲染成 Markdown 报告（提交进 eval/reports/，在 GitHub 上可以直接阅读）。"""

from __future__ import annotations

from collections.abc import Sequence

from .dataset import Labels
from .dedup import RunResult
from .metrics import Metrics, decide


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _ci(ci: tuple[float, float]) -> str:
    return f"[{ci[0] * 100:.0f}%, {ci[1] * 100:.0f}%]"


def render_report(
    run: RunResult,
    current: Metrics,
    swept: Sequence[Metrics],
    recommended: Metrics | None,
    labels: Labels,
    min_precision: float,
) -> str:
    cfg, rc = run.config, run.recall
    n = rc.usable_pairs or 1
    lines = [
        f"# 查重回放评测：{cfg.repo}",
        "",
        f"- 时间：{run.started_at:%Y-%m-%d %H:%M} UTC",
        f"- 语料：{run.corpus_size} 个 issue（指纹 `{run.corpus_fingerprint}`）；"
        f"标准答案 {run.gold_pairs} 对，可用于召回评测 {rc.usable_pairs} 对"
        f"（其中带报错堆栈 {rc.with_traceback} 对）",
        f"- 判断评测：正样本 {cfg.positives}、对照样本 {cfg.negatives}，seed={cfg.seed}，"
        f"提示词 v{cfg.prompt_version}，模型 `{cfg.model}`",
        f"- 模型调用 {run.model_calls} 次（缓存命中 {run.cached_calls} 次），"
        f"花费 ${run.cost_usd:.4f}",
        "",
        "## 召回（全部可用配对，时间切片）",
        "",
        "| k | 命中 | 比例 |",
        "|---|---|---|",
        *[f"| {k} | {h} / {rc.usable_pairs} | {_pct(h / n)} |" for k, h in rc.hits.items()],
        "",
        f"## 判断（当前阈值 high={current.high}，low={current.low}，"
        f"same_root_cause 闸门{'开启' if current.gate else '关闭'}）",
        "",
        "| 指标 | 数值 | 95% 置信区间 |",
        "|---|---|---|",
        f"| 精确率（判为重复中判对的比例） | {_pct(current.precision)} "
        f"| {_ci(current.precision_ci)} |",
        f"| 端到端召回率（含召回阶段的遗漏） | {_pct(current.recall)} | {_ci(current.recall_ci)} |",
        f"| 召回命中时的判断召回率 | {_pct(current.recall_given_recalled)} | |",
        f"| 正确 issue 出现在评论里的比例 | {_pct(current.surfaced_rate)} | |",
        "",
        f"正样本 {current.n_pos}：判对 {current.tp}（另有 {current.tp_alt} 个指向了标准答案之外、"
        f"经复核确认的同一根因 issue），指错目标 {current.wrong_target}"
        f"（其中未复核 {current.wrong_target_unreviewed}），"
        f"未判为重复 {current.missed}。对照样本 {current.n_neg}：被判为重复 {current.neg_flagged}"
        f"（人工确认是重复 {current.neg_flagged_confirmed_dup}，"
        f"未复核 {current.neg_flagged_unreviewed}，"
        "未复核的保守计为误报）。",
        "",
        "## 阈值扫描（不重新调用模型）",
        "",
        "| 闸门 | high | 精确率 | 召回率 | 判对 | 指错 | 对照被标记 |",
        "|---|---|---|---|---|---|---|",
    ]
    for m in swept:
        mark = " ⭐" if recommended is not None and m == recommended else ""
        lines.append(
            f"| {'开' if m.gate else '关'} | {m.high:.2f}{mark} | {_pct(m.precision)} | "
            f"{_pct(m.recall)} | {m.tp + m.tp_alt} | {m.wrong_target} | {m.neg_flagged} |"
        )
    lines += ["", f"### 推荐（精确率 ≥ {_pct(min_precision)} 时召回最高）", ""]
    if recommended is None:
        lines.append("没有任何组合达到精确率目标。先复核下方被标记的对照样本，再重新扫描。")
    else:
        lines.append(
            f"high = **{recommended.high:.2f}**，闸门{'开启' if recommended.gate else '关闭'}："
            f"精确率 {_pct(recommended.precision)} {_ci(recommended.precision_ci)}，"
            f"召回率 {_pct(recommended.recall)} {_ci(recommended.recall_ci)}。"
        )

    review = []
    for r in run.records:
        if not r.judged:
            continue
        verdict, cands = decide(r, current.high, current.low, current.gate)
        if verdict != "duplicate" or labels.get(r.issue, cands[0].number) is not None:
            continue
        if r.kind == "neg" or cands[0].number not in set(r.gold):
            review.append((r, cands[0]))
    lines += ["", "## 待人工复核（对照样本被判为重复、正样本指向了标准答案之外的 issue）", ""]
    if not review:
        lines.append("无。")
    else:
        lines.append(
            "复核后写进 `eval/datasets/<repo>/labels.json`，键为 `\"issue->候选\"`，"
            "再运行 `warden replay sweep` 重算。"
        )
        lines.append("")
        base = f"https://github.com/{cfg.repo}/issues"
        for r, c in review:
            kind = "对照" if r.kind == "neg" else f"正样本，标准答案 {r.gold[:3]}"
            lines.append(
                f"- [{kind}] [#{r.issue}]({base}/{r.issue}) {r.title} → "
                f"[#{c.number}]({base}/{c.number}) {c.title}（{c.score:.2f}）：{c.reason}"
            )
    errors = [r for r in run.records if r.error]
    if errors:
        lines += ["", f"## 出错的样本（{len(errors)} 个）", ""]
        lines += [f"- #{r.issue}：{r.error}" for r in errors]
    return "\n".join(lines) + "\n"
