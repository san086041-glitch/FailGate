"""仓库体检（W12，ADR 0043）：一条命令把评测流程跑在任意公开 Python 仓库上。

把 ADR 0040、0041 里对 pylint / packaging 手动做的那套串起来：

    概况（只调 GitHub / PyPI API，$0）：可用题估计、bug 类标签
      │
    选题规则：没有 selection.json 就按标签起草一份，停下来等人确认（--accept-rule）
      │      规则必须在跑之前定好，否则就是挑样本
    回填 issue（$0）
      │
    留出集 L2 + 严格 FB/PA（调 LLM）
      │
    ClaimVerify 正负例，含 break_other（$0）
      │
    报告：数字 + 区间 + 配置建议（related_always / test_deps）

每一步的产物记在 eval/checkups/<repo>/checkup.json，中断后重跑跳过已完成的步骤。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from failgate.replay.dataset import EVAL_ROOT, repo_slug
from failgate.replay.metrics import wilson
from failgate.replay.selection import Selection

SAMPLE = 200

# 标签名里的关键词：起草规则用（只是初稿，必须人确认）
_BUG = re.compile(r"bug|crash|regression|false.?positive|false.?negative|defect", re.I)
_EXCLUDE = re.compile(
    r"duplicate|invalid|wont.?fix|won't fix|not.?planned|question|support|cannot reproduce|"
    r"can't reproduce|works as intended|not.?a.?bug|upstream|downstream|outdated|wontfix", re.I)
_REPRO = re.compile(r"crash|false.?positive|false.?negative|regression", re.I)

_TEST_DIRS = re.compile(r"(^|/)(tests?|testing)/|(^|/)test_[^/]*\.py$|_test\.py$|conftest\.py$")
_DEP_FILES = re.compile(
    r"(^|/)(pyproject\.toml|setup\.py|setup\.cfg|requirements[^/]*\.txt|poetry\.lock|uv\.lock|"
    r"tox\.ini)$")

Q_SEARCH = """query($q:String!,$after:String){search(query:$q,type:ISSUE,first:50,after:$after){
 issueCount pageInfo{hasNextPage endCursor}
 nodes{... on Issue{number timelineItems(itemTypes:[CLOSED_EVENT],last:1){nodes{
  ... on ClosedEvent{closer{__typename
   ... on PullRequest{merged files(first:100){nodes{path}}}}}}}}}}}"""


# ---------------------------------------------------------------- 概况


class Survey(BaseModel):
    repo: str
    stars: int = 0
    size_mb: float = 0.0
    languages: dict[str, float] = Field(default_factory=dict)  # 语言 → 占比
    archived: bool = False
    pushed: str = ""
    pypi: str | None = None  # PyPI 上的最新版本；None = 没找到
    labels: list[str] = Field(default_factory=list)
    since: str = ""
    closed: int = 0  # since 之后已完成关闭的 issue 数
    sampled: int = 0
    closer_pr: int = 0  # 样本里被合并 PR 关闭的
    usable: int = 0  # 样本里 PR 同时改了源码和测试、不改依赖的
    surveyed_at: str = ""

    @property
    def est_usable(self) -> int:
        return round(self.closed * self.usable / self.sampled) if self.sampled else 0

    @property
    def python_share(self) -> float:
        return self.languages.get("Python", 0.0)


def usable_pr(files: Sequence[str]) -> bool:
    """评测集能用的修复：改了源码、改了测试、没改依赖声明（和 fixset 的口径一致）。"""
    src = [f for f in files if f.endswith(".py") and not _TEST_DIRS.search(f)]
    tst = [f for f in files if _TEST_DIRS.search(f)]
    return bool(src and tst) and not any(_DEP_FILES.search(f) for f in files)


async def survey(gh: Any, http: Any, repo: str, package: str, *, since: str,
                 pypi_url: str = "https://pypi.org", sample: int = SAMPLE) -> Survey:
    """gh：GitHubRest（REST + GraphQL）；http：httpx.AsyncClient（查 PyPI）。只读，$0。"""
    info = await gh.repo(repo)
    langs = (await gh._http.get(f"/repos/{repo}/languages")).json()
    total = sum(langs.values()) or 1
    out = Survey(repo=repo, stars=info.get("stargazers_count", 0),
                 size_mb=round(info.get("size", 0) / 1024, 1),
                 languages={k: round(v / total, 3) for k, v in langs.items()},
                 archived=bool(info.get("archived")), pushed=(info.get("pushed_at") or "")[:10],
                 labels=[lb["name"] for lb in await gh.list_labels(repo)], since=since,
                 surveyed_at=datetime.now().isoformat(timespec="seconds"))
    r = await http.get(f"{pypi_url.rstrip('/')}/pypi/{package}/json")
    if r.status_code == 200:
        out.pypi = r.json()["info"]["version"]
    q = f"repo:{repo} is:issue is:closed reason:completed created:>={since} sort:created-desc"
    nodes: list[dict[str, Any]] = []
    after = None
    while len(nodes) < sample:
        d = (await gh.graphql(Q_SEARCH, {"q": q, "after": after}))["search"]
        out.closed = d["issueCount"]
        nodes += [n for n in d["nodes"] if n]
        if not d["pageInfo"]["hasNextPage"]:
            break
        after = d["pageInfo"]["endCursor"]
    nodes = nodes[:sample]
    out.sampled = len(nodes)
    for n in nodes:
        ev = n["timelineItems"]["nodes"]
        closer = (ev[0] or {}).get("closer") if ev else None
        if not closer or closer.get("__typename") != "PullRequest" or not closer.get("merged"):
            continue
        out.closer_pr += 1
        out.usable += usable_pr([f["path"] for f in closer["files"]["nodes"]])
    return out


# ---------------------------------------------------------------- 选题规则初稿


def draft_selection(labels: Sequence[str]) -> Selection:
    """按标签名起草选题规则。只是初稿：标签的实际用法要人看过才算数。"""
    exclude = [x for x in labels if _EXCLUDE.search(x)]
    bug = [x for x in labels if _BUG.search(x) and x not in exclude]
    repro = [x for x in bug if _REPRO.search(x)]
    note = ("草稿：由 failgate checkup 按标签名起草。确认 bug_labels / exclude_labels "
            "符合这个仓库的"
            "实际用法后，用 --accept-rule 继续。repro_labels 为空表示所有 bug 都参与复现。")
    # 只有一个笼统的 bug 标签时，复现不再按类别筛（和 packaging 一样）
    return Selection(bug_labels=bug, repro_labels=repro if len(repro) < len(bug) else [],
                     exclude_labels=exclude, note=note)


# ---------------------------------------------------------------- 状态


class Checkup(BaseModel):
    repo: str
    package: str
    since: str
    offset: int
    limit: int
    survey: Survey | None = None
    rule_path: str | None = None
    rule_drafted: bool = False
    backfilled: int | None = None
    l2_run: str | None = None
    verify_run: str | None = None
    report: str | None = None
    subdir: str | None = None  # monorepo 的子目录（ADR 0045）
    test_deps: list[str] = Field(default_factory=list)


def state_path(repo: str, root: Path = EVAL_ROOT) -> Path:
    return root / "checkups" / repo_slug(repo) / "checkup.json"


def load_state(path: Path) -> Checkup | None:
    return Checkup.model_validate_json(path.read_text(encoding="utf-8")) if path.exists() else None


def save_state(state: Checkup, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state.model_dump_json(indent=1), encoding="utf-8")


# ---------------------------------------------------------------- 建议与报告


def suggestions(verify_rows: Sequence[dict[str, Any]], l2: dict[str, Any] | None
                ) -> list[str]:
    """从核验结果里找该补的仓库配置（verify.json）。只给建议，不自动改。"""
    out: list[str] = []
    fixes = [r for r in verify_rows if r.get("kind") == "fix" and not r.get("skipped")]
    l3 = [r.get("layer3") or {} for r in fixes]
    none = sum(x.get("status") == "none" and x.get("reason") == "none" for x in l3)
    if fixes and none:
        out.append(f"第三层在 {none}/{len(fixes)} 个真实修复上一个相关测试都没挑到"
                   "（按 import 静态挑选看不到）：看看这些修复的测试在哪，"
                   "考虑 verify.json 的 `related_always`（ADR 0041）。")
    missing = Counter(m for x in l3 for m in x.get("missing_modules") or [])
    not_run = sum(bool(x.get("not_run")) for x in l3)
    if not_run:
        mods = "、".join(f"`{m}`" for m, _ in missing.most_common(5)) or "（没解析出模块名）"
        out.append(f"{not_run}/{len(fixes)} 个真实修复的相关测试在合并基点和 PR 上都收集失败，"
                   f"缺的模块：{mods}。加进 verify.json 的 `test_deps`"
                   "（模块名和 pip 包名可能不同）。")
    misses = [r for r in verify_rows
              if r.get("kind") == "break_other" and r.get("verdict") == "VERIFIED"]
    gap = [r for r in misses if (r.get("layer3") or {}).get("status") == "pass"]
    if gap:
        out.append(f"break_other 有 {len(gap)} 个漏判时第三层其实跑了测试：仓库本身没测试覆盖"
                   "被注入的函数，这是仓库测试的盲区，不是配置问题。")
    if l2:
        failed = l2.get("outcomes", {}).get("setup_failed")
        if failed:
            out.append(f"L2 有 {failed} 题环境没搭起来（源码环境或 pytest 预检没过）："
                       "看回放报告里的"
                       "原因，常见是需要编译器 / 系统库，或测试依赖没装。")
    return out


def _pct(k: int, n: int) -> str:
    if not n:
        return "—"
    lo, hi = wilson(k, n)
    return f"{k}/{n} = {k / n:.0%}（{lo:.0%}–{hi:.0%}）"


def _names(xs: Sequence[str], empty: str) -> str:
    return "、".join(f"`{x}`" for x in xs) or empty


def _survey_lines(s: Survey) -> list[str]:
    impure = "（不是纯 Python，源码环境可能装不上）" if s.python_share < 0.9 else ""
    return ["## 概况（只调 API，$0）", "",
            "| 项 | 值 |", "|---|---|",
            f"| 星数 / 仓库大小 | {s.stars} / {s.size_mb} MB |",
            f"| Python 占比 | {s.python_share:.0%}{impure} |",
            f"| PyPI | {s.pypi or '没找到（只能走源码模式）'} |",
            f"| 最近推送 / 归档 | {s.pushed} / {'是' if s.archived else '否'} |",
            f"| 已完成关闭的 issue | {s.closed} |",
            f"| 样本里被合并 PR 关闭 | {s.closer_pr}/{s.sampled} |",
            f"| 样本里可用题（改源码 + 改测试 + 不改依赖） | {s.usable}/{s.sampled}，"
            f"估计约 {s.est_usable} 题 |", ""]


def _verify_lines(state: Checkup, verify_rows: Sequence[dict[str, Any]]) -> list[str]:
    rows = [r for r in verify_rows if not r.get("skipped")]
    if not rows:
        return []
    by: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by.setdefault(r["kind"], []).append(r)
    lines = ["## 核验：ClaimVerify 正负例", "", "| 变体 | 期望 | 判对 |", "|---|---|---|"]
    for kind, rs in by.items():
        ok = sum(r["correct"] for r in rs)
        lines.append(f"| {kind} | {rs[0]['expected']} | {_pct(ok, len(rs))} |")
    total = sum(r["correct"] for r in rows)
    lines.append(f"| **合计** | | **{_pct(total, len(rows))}** |")
    na = [r for r in verify_rows if r.get("skipped")]
    if na:
        items = "；".join(f"#{r['number']} {r['kind']}：{r['skipped']}" for r in na)
        lines += ["", f"n/a（不计入）：{items}"]
    return [*lines, "", f"核验记录：`{state.verify_run}`", ""]


GLOSSARY = [
    ("出题（L2）", "AI 在\"bug 还在\"的旧代码上写一个测试，要求它失败，"
                   "而且失败的原因和 issue 描述的一致。"),
    ("严格 FB/PA", "检验测试是不是真抓住了这个 bug：放到修复前一刻的代码上应该失败（Fail Before），"
                   "放到修复后的代码上应该通过（Pass After）。两头都对，才算一份合格的\"考卷\"。"),
    ("核验（ClaimVerify）", "拿合格的考卷去判别人提交的修复 PR："
                          "真修好了就放行，没修好或作弊就驳回。"),
    ("break_other", "故意构造的\"修好了这个 bug、但弄坏了别处\"的 PR，"
                    "专门检验\"查有没有弄坏别处\"这一步。"),
    ("区间（如 45%–94%）", "样本少，百分比不稳：真实水平大概率落在这个范围里。题越少，范围越宽。"),
]


def _kind_counts(rows: Sequence[dict[str, Any]], kinds: Sequence[str]) -> tuple[int, int]:
    rs = [r for r in rows if r["kind"] in kinds]
    return sum(r["correct"] for r in rs), len(rs)


def plain_summary(state: Checkup, l2: dict[str, Any] | None,
                  verify_rows: Sequence[dict[str, Any]]) -> list[str]:
    """报告开头的大白话：一句结论 + 几句解释 + 名词对照。数字和后面的表格一致。"""
    lines = ["## 大白话总结", "",
             f"体检在做什么：拿 `{state.repo}` 过去真实发生过、已经修好的 bug 考 FailGate 两件事——"
             "**会不会出题**（为 bug 写一个修复前失败、修复后通过的测试），"
             "**会不会阅卷**（用这些测试分清\"真修好了\"和\"假装修好了\"的 PR）。"
             "这些 bug 都已经修好，正确答案是已知的，所以能算出判对了多少。", ""]
    s = state.survey
    if s:
        enough = "题够多，值得体检" if s.est_usable >= 30 else "题偏少，结果的参考价值有限"
        lines.append(f"- **题库**：这个仓库的历史 bug 里，估计约 {s.est_usable} 个能当考题"
                     f"（有人提交了修复，修复同时改了代码和测试）。{enough}。")
    if not l2:
        lines += ["- 还没跑出题和阅卷（只做了概况和选题规则）。", ""]
        return [*lines, *_glossary_lines()]
    lines.append(f"- **出题**：选了 {l2['n']} 个真实 bug，"
                 f"AI 为其中 **{l2['l2']}** 个写出了能抓住 bug 的测试；"
                 f"能对照修复代码验证的 {l2['fbpa_eligible']} 个里，**{l2['fb_pa']}** 个确认合格"
                 "（修复前失败、修复后通过）。")
    rows = [r for r in verify_rows if not r.get("skipped")]
    if rows:
        fix_ok, fix_n = _kind_counts(rows, ["fix"])
        cheat_ok, cheat_n = _kind_counts(
            rows, ["revert_code", "exam_skip", "conftest_skip", "unrelated"])
        bo_ok, bo_n = _kind_counts(rows, ["break_other"])
        total_ok = sum(r["correct"] for r in rows)
        parts = [f"真修复 {fix_ok}/{fix_n} 正确放行", f"作弊 {cheat_ok}/{cheat_n} 被拦住"]
        if bo_n:
            parts.append(f"\"修好了但弄坏别处\" {bo_ok}/{bo_n} 被抓到")
        lines.append(f"- **阅卷**：用合格的考卷判了 {len(rows)} 个答案已知的 PR，"
                     f"判对 **{total_ok}** 个：" +
                     "，".join(parts) + "。")
    tips = suggestions(verify_rows, l2)
    lines.append(f"- **建议**：{len(tips)} 条需要补的配置，见文末\"建议\"。" if tips
                 else "- **建议**：没发现需要补的配置。")
    lines += ["", f"**结论：{_verdict(l2, rows)}**", "",
              "数字后面括号里的区间要一起看：每项只有十几道题，单看百分比容易高估或低估。", ""]
    return [*lines, *_glossary_lines()]


def _verdict(l2: dict[str, Any], rows: Sequence[dict[str, Any]]) -> str:
    if not l2.get("fbpa_eligible"):
        return "数据不够：没有能对照修复验证的考卷，下不了结论。"
    fbpa = l2["fb_pa"] / l2["fbpa_eligible"]
    cheat_ok, cheat_n = _kind_counts(
        rows, ["revert_code", "exam_skip", "conftest_skip", "unrelated"])
    fix_ok, fix_n = _kind_counts(rows, ["fix"])
    clean = cheat_ok == cheat_n and fix_ok == fix_n
    if fbpa >= 0.7 and clean:
        return ("FailGate 在这个仓库上能用——大多数 bug 能写出合格的考卷，"
                "阅卷没有放过作弊、也没有冤枉真修复。")
    if clean:
        return "阅卷可信，但出题成功率偏低：只有一部分 bug 能写出合格的考卷。"
    return "需要先看看判错的案例：阅卷有放过作弊或冤枉真修复的情况。"


def _glossary_lines() -> list[str]:
    lines = ["<details><summary>名词对照（后面的表格会用到）</summary>", ""]
    lines += [f"- **{k}**：{v}" for k, v in GLOSSARY]
    return [*lines, "", "</details>", ""]


def render(state: Checkup, rule: Selection | None, l2: dict[str, Any] | None,
           verify_rows: Sequence[dict[str, Any]]) -> str:
    lines = [f"# 仓库体检：{state.repo}", "",
             f"- 包名 `{state.package}`；{state.since} 之后创建的 issue；"
             f"留出集 = 按编号从新到旧跳过 {state.offset} 个、取 {state.limit} 个", ""]
    lines += plain_summary(state, l2, verify_rows)
    lines += ["---", "", "以下是详细数据。", ""]
    if state.survey:
        lines += _survey_lines(state.survey)
    if rule:
        drafted = "（由体检起草、人确认后使用）" if state.rule_drafted else ""
        lines += ["## 选题规则", "",
                  f"- bug 标签：{_names(rule.bug_labels, '（无）')}",
                  f"- 复现另外要求：{_names(rule.repro_labels, '不要求')}",
                  f"- 排除：{_names(rule.exclude_labels, '（无）')}",
                  f"- 规则文件：`{state.rule_path}`{drafted}", ""]
    if l2:
        lines += ["## 出题：留出集 L2 + 严格 FB/PA", "",
                  "| 指标 | 结果 |", "|---|---|",
                  f"| 出题 L2（AI 写出了能抓住 bug 的测试） | {_pct(l2['l2'], l2['n'])} |",
                  f"| 严格 FB/PA（合格的考卷：修复前失败、修复后通过） | "
                  f"{_pct(l2['fb_pa'], l2['fbpa_eligible'])} |",
                  f"| 花费 | ${l2.get('total_cost_usd', 0)} |",
                  f"| 回放记录 | `{state.l2_run}` |", ""]
    lines += _verify_lines(state, verify_rows)
    tips = suggestions(verify_rows, l2)
    lines += ["## 建议", ""]
    lines += [f"- {t}" for t in tips] or ["- 没发现需要补的配置。"]
    lines += ["", "## 怎么读这份报告", "",
              "- 每个数字都带 Wilson 95% 区间：样本少时区间很宽，不要拿单个仓库的百分比下结论。",
              "- 留出集只跑一次；选题规则在跑之前定好（见上），没有按结果调整。",
              "- 人工核对（如果有）由 Claude 完成，不是维护者。", ""]
    return "\n".join(lines)


def load_rows(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
