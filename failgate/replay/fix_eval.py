"""修复 Agent 的提升实验（ADR 0028）：给考卷 vs 不给考卷，用上游修复自带的测试判成败。

对 ClaimVerify 正负例评测里的 black 正例（严格 FB/PA 成立、有修复提交）：

    父提交 P ──修复 Agent──▶ 补丁
                               │
    金标准 = 上游修复提交 F 里改动的测试文件（维护者写的，不是我们的考卷）
      P + 金标准测试         → 失败集合 fail_P
      F（P + F 的全部改动）   → 失败集合 fail_F
      F2P = fail_P − fail_F（上游修复修好的那些测试；为空说明金标准分不出好坏，这题不用）
      P + 补丁 + 金标准测试   → 失败集合 fail_A
      修好 ⇔ fail_A ⊆ fail_F（F2P 都通过，也没有 F 上本来通过的测试被弄坏）

只列失败（-rfE）而不是全部结果（-rA）：black 的测试上千个，全部列出会超过沙箱的输出上限；
收集出错会以 ERROR 出现在失败里，所以"失败集合"本身就够判定。

实验组的补丁还要：封存考卷有没有通过（Agent 自己的验收）、隐藏考卷有没有被触发
（ADR 0021，9-29 那次封存的题）。交叉起来看"过了考卷但没修对"时隐藏考卷能抓住几个。
"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from failgate.fix.workspace import FixWorkspace, workspace_pythonpath
from failgate.repro.l2 import RUN_PREFIXES, WORK_SRC, SourcePrepared, TestReproducer, pytest_argv
from failgate.repro.sandbox import ExecResult
from failgate.repro.source import SourceTree
from failgate.verify.related import failed_nodes
from failgate.verify.tamper import PullFile, is_test_file

from .dedup_compare import binom_two_sided
from .metrics import wilson

ARMS = ("exam", "control")
ARM_NAME = {"exam": "实验组（给封存考卷）", "control": "对照组（不给考卷）"}
GOLD_TIMEOUT_S = 900
GOLD_ARGS = ["-q", "--tb=no", "-p", "no:cacheprovider", f"--rootdir={WORK_SRC}", "-rfE",
             "--continue-on-collection-errors"]
_COUNT = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)")


# ---------------------------------------------------------------- 纯函数


def is_test_change(path: str, test_dir: str) -> bool:
    return path.startswith(f"{test_dir}/") or is_test_file(path)


DEP_FILES = ("pyproject.toml", "setup.py", "setup.cfg")
# pyproject / setup.py 里是带引号的需求串："pytokens>=0.1.10"、"click"
_REQ_QUOTED = re.compile(r"""^[+-]\s*["'][A-Za-z0-9][\w.\-\[\]]*\s*(?:[<>=!~;]|["'])""")
# requirements*.txt / setup.cfg 里不带引号：click>=8.0（要有版本运算符，免得误认普通配置行）
_REQ_BARE = re.compile(r"^[+-]\s*[A-Za-z0-9][\w.\-\[\]]*\s*(?:[<>!~]=?|==)")


def deps_changed(files: Sequence[PullFile]) -> list[str]:
    """上游修复改了依赖声明的文件（新增 / 删除了形如 "pytokens>=0.1.10" 的行）。

    所有运行都在父提交的环境里，新依赖装不进去：F 在这个环境里跑不起来，金标准不可信（#4588）。
    Agent 在断网沙箱里也装不了新依赖。这种题整题排除。"""
    out = []
    for f in files:
        name = f.filename.rsplit("/", 1)[-1]
        is_dep = name in DEP_FILES or (name.startswith("requirements") and name.endswith(".txt"))
        if not is_dep or ("/" in f.filename and name in DEP_FILES):  # 只看仓库根的项目配置
            continue
        lines = [ln for ln in (f.patch or "").splitlines()
                 if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
        pattern = _REQ_QUOTED if name in ("pyproject.toml", "setup.py") else _REQ_BARE
        if any(pattern.match(ln) for ln in lines):
            out.append(f.filename)
    return out


def gold_targets(files: Sequence[PullFile], test_dir: str) -> list[str]:
    """上游修复改动的测试模块；只改了测试数据（black 的 tests/data/cases/*）时跑整个测试目录。"""
    mods = sorted({f.filename for f in files if f.status != "removed"
                   and f.filename.endswith(".py") and is_test_file(f.filename)
                   and f.filename.rsplit("/", 1)[-1] != "conftest.py"})
    return mods or [test_dir]


def overlay_of(tree: SourceTree, files: Sequence[PullFile]) -> dict[str, str]:
    """F 上这些文件的内容（删除的文件跳过：工作区只能写不能删，记在报告里）。"""
    wanted = {f.filename for f in files if f.status != "removed"}
    return tree.read_files(lambda p: p in wanted, max_bytes=2**22)


def counts(output: str) -> dict[str, int]:
    """pytest 最后一行的 "3 failed, 1200 passed in 30.1s"。"""
    line = next((ln for ln in reversed(output.splitlines()) if _COUNT.search(ln)), "")
    out: dict[str, int] = {}
    for n, kind in _COUNT.findall(line):
        out[kind.rstrip("s") if kind.startswith("error") else kind] = int(n)
    return out


def valid_run(res: ExecResult) -> str | None:
    """None 表示这次运行可以用来判定；否则返回原因。"""
    if res.timed_out:
        return "timeout"
    if res.oom_killed:
        return "oom"
    if res.exit_code not in (0, 1):
        return f"exit_{res.exit_code}"
    if not counts(res.stdout):
        return "no_summary"
    return None


class Gold(BaseModel):
    status: str = "ok"  # ok / no_f2p / infra / deps_changed
    reason: str = ""
    targets: list[str] = Field(default_factory=list)
    test_files: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    fail_parent: list[str] = Field(default_factory=list)
    fail_fix: list[str] = Field(default_factory=list)
    f2p: list[str] = Field(default_factory=list)
    counts_parent: dict[str, int] = Field(default_factory=dict)
    counts_fix: dict[str, int] = Field(default_factory=dict)


def derive_gold(parent: ExecResult, fix: ExecResult, *, targets: list[str],
                test_files: list[str], removed: list[str]) -> Gold:
    base = {"targets": targets, "test_files": test_files, "removed": removed,
            "counts_parent": counts(parent.stdout), "counts_fix": counts(fix.stdout)}
    for name, res in (("parent", parent), ("fix", fix)):
        if bad := valid_run(res):
            return Gold(status="infra", reason=f"{name}:{bad}", **base)
    fp, ff = failed_nodes(parent.stdout), failed_nodes(fix.stdout)
    f2p = sorted(fp - ff)
    return Gold(status="ok" if f2p else "no_f2p", fail_parent=sorted(fp), fail_fix=sorted(ff),
                f2p=f2p, **base)


def judge(res: ExecResult, gold: Gold) -> dict[str, Any]:
    """补丁在金标准测试上的结果。resolved 只在运行有效时有意义。"""
    if bad := valid_run(res):
        return {"valid": False, "reason": bad, "resolved": False}
    fa = failed_nodes(res.stdout)
    extra = fa - set(gold.fail_fix)
    f2p_failed = sorted(extra & set(gold.f2p))
    broken = sorted(extra - set(gold.f2p))  # F 上通过、补丁上失败，又不是 F2P：新增回归
    return {"valid": True, "reason": "", "resolved": not extra,
            "f2p_passed": len(gold.f2p) - len(f2p_failed), "f2p_total": len(gold.f2p),
            "f2p_failed": f2p_failed[:20], "broken": broken[:20], "broken_n": len(broken),
            "counts": counts(res.stdout)}


# ---------------------------------------------------------------- 沙箱里跑


class GoldBench:
    """在父提交的环境里跑"源码副本 + 覆盖文件"。

    和修复 Agent 的验收同一套做法：PYTHONPATH 指向工作区，遮住 site-packages 里的安装版。"""

    def __init__(self, tester: TestReproducer, prepared: SourcePrepared) -> None:
        self.tester = tester
        self.prepared = prepared
        self.pythonpath = workspace_pythonpath(prepared)

    async def run(self, overlay: Mapping[str, str], argv: list[str],
                  timeout_s: int = GOLD_TIMEOUT_S) -> ExecResult:
        p = self.prepared
        ws = await self.tester.open_workspace(p, f"gold-{p.env.key[:8]}")
        try:
            if overlay:
                with tempfile.TemporaryDirectory() as tmp:
                    await asyncio.to_thread(FixWorkspace._write_tree, Path(tmp), dict(overlay))
                    await self.tester.sandbox.copy_in(ws, Path(tmp), p.env.image)
            return await self.tester.sandbox.run(
                p.env.image, ws, argv, timeout_s=timeout_s, allowed=RUN_PREFIXES,
                env=[f"PYTHONPATH={self.pythonpath}"])
        finally:
            await self.tester.sandbox.remove_workspace(ws)

    async def gold_tests(self, overlay: Mapping[str, str], targets: list[str]) -> ExecResult:
        argv = ["python", "-m", "pytest", *[f"{WORK_SRC}/{t}" for t in targets], *GOLD_ARGS]
        return await self.run(overlay, argv)

    async def hidden(self, overlay: Mapping[str, str], path: str, code: str) -> set[str] | None:
        """隐藏考卷：有题失败就整份再跑一次，两次都失败的才算（和 run_hidden 一致）。
        返回失败的题；运行无效返回 None。"""
        argv = [a for a in pytest_argv(path) if a != "-x"] + ["-rA"]
        failed: set[str] | None = None
        for _ in range(2):
            res = await self.run({**overlay, path: code}, argv, timeout_s=180)
            if res.timed_out or res.oom_killed or res.exit_code not in (0, 1):
                return None
            now = failed_nodes(res.stdout)
            failed = now if failed is None else failed & now
            if not failed:
                return set()
        return failed or set()


# ---------------------------------------------------------------- 记录


@dataclass
class HiddenCase:
    code: str
    tests: list[str]


def load_hidden(path: Path) -> dict[int, HiddenCase]:
    """replay hidden 的结果（.jsonl）里封存了的题（路径由考卷路径推出：hidden_path）。"""
    out: dict[int, HiddenCase] = {}
    if not path.exists():
        return out
    for ln in path.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        r = json.loads(ln)
        if r.get("sealed") and r.get("code"):
            out[r["number"]] = HiddenCase(code=r["code"], tests=list(r.get("tests") or []))
    return out


def load_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ---------------------------------------------------------------- 把补丁当 PR 走 ClaimVerify


def patch_edits(row: dict[str, Any]) -> dict[str, str]:
    """一次运行的改动（完整文件内容），从它的 transcript 里取（运行记录里只存了 diff）。"""
    path = row["fix"].get("transcript_path")
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return dict(data["result"].get("edits") or {})


async def claimverify_patch(case: Any, parent: SourceTree, edits: Mapping[str, str], *,
                            label: str, bench_for: Any) -> dict[str, Any]:
    """父提交 + 补丁 当成一个 PR（base = 父提交），用封存考卷走完整的三层核验。"""
    from failgate.verify.engine import ClaimVerifier, PullRequest

    head = parent.overlay(dict(edits), label=label)
    local = {case.parent: parent, head.sha: head}

    async def fetch_local(_: str, sha: str) -> SourceTree:
        return local[sha]

    files = [PullFile(filename=f, status="modified") for f in sorted(edits)]
    pr = PullRequest(repo="eval", number=0, title="fix agent patch",
                     body=f"Fixes #{case.number}", base_sha=case.parent, head_sha=head.sha,
                     head_repo="eval", files=files)
    v = await ClaimVerifier(bench_for(fetch_local), strength=False).verify(
        pr, [case.number], {case.number: case.exam})
    c = v.claims[0]
    return {"verdict": c.verdict.value, "reasons": c.reasons,
            "layer3": c.layer3.model_dump(mode="json") if c.layer3 else None}


def summarize_verify(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    good = [r for r in rows if r["gold_resolved"]]
    bad = [r for r in rows if not r["gold_resolved"]]
    return {
        "n": len(rows), "good": len(good), "bad": len(bad),
        "bad_refuted": sum(r["verdict"] == "REFUTED" for r in bad),
        "good_refuted": sum(r["verdict"] == "REFUTED" for r in good),
        "good_verified": sum(r["verdict"] == "VERIFIED" for r in good),
        "verdicts": dict(Counter((r["gold_resolved"], r["verdict"]) for r in rows)),
    }


# ---------------------------------------------------------------- 汇总


def _runs(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r.get("type") == "run"]


def summarize(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    runs = [r for r in _runs(rows) if r["gold"]["valid"]]
    arms: dict[str, dict[str, Any]] = {}
    for arm in ARMS:
        rs = [r for r in runs if r["arm"] == arm]
        k, n = sum(r["gold"]["resolved"] for r in rs), len(rs)
        arms[arm] = {
            "n": n, "resolved": k, "rate": k / n if n else 0.0,
            "ci": wilson(k, n) if n else (0.0, 0.0),
            "status": dict(Counter(r["fix"]["status"] for r in rs)),
            "cost": round(sum(r["fix"]["cost_usd"] for r in rs), 4),
            "mean_cost": round(sum(r["fix"]["cost_usd"] for r in rs) / n, 4) if n else 0.0,
            "mean_steps": round(sum(r["fix"]["steps"] for r in rs) / n, 1) if n else 0.0,
            "mean_seconds": round(sum(r["fix"]["duration_s"] for r in rs) / n, 1) if n else 0.0,
            "no_patch": sum(not r["fix"]["files"] for r in rs),
            "broke": sum(r["gold"].get("broken_n", 0) > 0 for r in rs),
            "denied": sum(r["fix"]["denied"] for r in rs),
        }
    # 成对：同一题、同一次重复编号
    by = {(r["number"], r["rep"], r["arm"]): r["gold"]["resolved"] for r in runs}
    gained = lost = same = 0
    for (num, rep, arm), ok in by.items():
        if arm != "exam" or (num, rep, "control") not in by:
            continue
        ctl = by[(num, rep, "control")]
        if ok and not ctl:
            gained += 1
        elif ctl and not ok:
            lost += 1
        else:
            same += 1
    # 实验组：封存考卷 × 金标准 × 隐藏考卷
    exam_runs = [r for r in runs if r["arm"] == "exam"]
    cross = Counter((r["fix"]["passed"], r["gold"]["resolved"]) for r in exam_runs)
    false_pass = [r for r in exam_runs if r["fix"]["passed"] and not r["gold"]["resolved"]]
    with_hidden = [r for r in exam_runs if r["fix"]["passed"] and r.get("hidden") is not None]
    caught = [r for r in with_hidden if not r["gold"]["resolved"] and r["hidden"]["flagged"]]
    missed = [r for r in with_hidden if not r["gold"]["resolved"] and not r["hidden"]["flagged"]]
    alarms = [r for r in with_hidden if r["gold"]["resolved"] and r["hidden"]["flagged"]]
    good = [r for r in with_hidden if r["gold"]["resolved"]]
    return {
        "arms": arms, "diff": arms["exam"]["rate"] - arms["control"]["rate"],
        "paired": {"gained": gained, "lost": lost, "same": same,
                   "p": binom_two_sided(gained, gained + lost)},
        "exam_cross": {f"{'过' if p else '没过'}考卷/{'修好' if g else '没修好'}": n
                       for (p, g), n in sorted(cross.items(), reverse=True)},
        "false_pass": len(false_pass), "exam_passed": sum(r["fix"]["passed"] for r in exam_runs),
        "hidden": {"n": len(with_hidden), "caught": len(caught), "missed": len(missed),
                   "false_alarm": len(alarms), "good": len(good)},
        "invalid_runs": len(_runs(rows)) - len(runs),
        "cost": round(sum(r["fix"]["cost_usd"] for r in _runs(rows)), 4),
    }


def _pct(k: int, n: int) -> str:
    return f"{k}/{n}（{k / n * 100:.0f}%）" if n else "—"


def render(rows: Sequence[dict[str, Any]], meta: dict[str, Any]) -> str:
    s = summarize(rows)
    golds = [r for r in rows if r.get("type") == "gold"]
    lines = [
        f"# 修复 Agent 提升实验：{meta['repo']}",
        "",
        f"- 时间：{meta['started']}；来源：`{meta['source']}`；隐藏考卷：`{meta.get('hidden')}`",
        f"- 模型：{meta.get('model')}；每次上限 ${meta.get('budget')}、"
        f"最多 {meta.get('rounds')} 轮；每题每组重复 {meta.get('reps')} 次",
        "- 成功判定：上游修复提交自带的测试（F2P 全部通过、F 上通过的测试没被弄坏），"
        "不是我们的考卷",
        "",
        "## 金标准",
        "",
        "| issue | 跑的测试 | P 上失败 | F 上失败 | F2P | 状态 |",
        "|---|---|---|---|---|---|",
    ]
    for g in sorted(golds, key=lambda g: -g["number"]):
        gd = g["gold"]
        lines.append(f"| #{g['number']} | {', '.join(gd['targets'])} | {len(gd['fail_parent'])} | "
                     f"{len(gd['fail_fix'])} | {len(gd['f2p'])} | {gd['status']}"
                     f"{'（' + gd['reason'] + '）' if gd['reason'] else ''} |")
    lines += ["", "## 结果", "",
              "| 组 | 次数 | 修好 | Wilson 95% | 平均花费 | 平均步数 | 平均用时 | "
              "没交补丁 | 弄坏别的测试 | 状态分布 |", "|---|---|---|---|---|---|---|---|---|---|"]
    for arm in ARMS:
        a = s["arms"][arm]
        lo, hi = a["ci"]
        dist = "、".join(f"{k} {v}" for k, v in sorted(a["status"].items()))
        lines.append(f"| {ARM_NAME[arm]} | {a['n']} | {_pct(a['resolved'], a['n'])} | "
                     f"{lo:.0%}–{hi:.0%} | ${a['mean_cost']} | {a['mean_steps']} | "
                     f"{a['mean_seconds']} s | {a['no_patch']} | {a['broke']} | {dist} |")
    p = s["paired"]
    lines += [
        "",
        f"**差值 {s['diff'] * 100:+.0f} 个百分点**；成对（同题同重复编号）："
        f"实验组修好而对照组没修好 {p['gained']}、反过来 {p['lost']}、结论相同 {p['same']}，"
        f"精确二项检验 p = {p['p']:.3f}",
        "",
        f"- 无效运行（金标准测试没跑起来）：{s['invalid_runs']}；总花费 ${s['cost']}",
        "",
        "## 实验组：封存考卷 vs 金标准",
        "",
        "、".join(f"{k} {v}" for k, v in s["exam_cross"].items()) or "—",
        "",
        f"- 过了封存考卷但没修好（考卷放过的错误修复）：{s['false_pass']} / {s['exam_passed']}",
    ]
    h = s["hidden"]
    lines += [
        f"- 其中有隐藏考卷的 {h['n']} 次：没修好的里被隐藏考卷抓到 {h['caught']}、"
        f"漏掉 {h['missed']}；修好的 {h['good']} 次里误报 {h['false_alarm']}",
        "",
        "## 逐条",
        "",
        "| issue | 组 | 次 | Agent 状态 | 考卷 | 金标准 | F2P | 弄坏 | 隐藏考卷 | 步数 | 花费 | "
        "改动文件 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(_runs(rows), key=lambda r: (-r["number"], r["arm"], r["rep"])):
        g, f = r["gold"], r["fix"]
        exam = "—" if r["arm"] == "control" else ("✅" if f["passed"] else "❌")
        gold = ("✅" if g["resolved"] else "❌") if g["valid"] else f"无效（{g['reason']}）"
        hid = "—" if r.get("hidden") is None else (
            f"{len(r['hidden']['failed'])}/{r['hidden']['total']} 失败" if r["hidden"]["flagged"]
            else f"0/{r['hidden']['total']}")
        f2p = f"{g.get('f2p_passed', '—')}/{g.get('f2p_total', '—')}" if g["valid"] else "—"
        lines.append(f"| #{r['number']} | {r['arm']} | {r['rep']} | {f['status']} | {exam} | "
                     f"{gold} | {f2p} | {g.get('broken_n', '—')} | {hid} | {f['steps']} | "
                     f"${f['cost_usd']:.4f} | {', '.join(f['files']) or '—'} |")
    return "\n".join(lines) + "\n"


VERIFY_HEADING = "## 实验组过了考卷的补丁：当成 PR 走 ClaimVerify"


def render_verify(rows: Sequence[dict[str, Any]]) -> str:
    """追加到报告末尾的一节：过了封存考卷的补丁，完整三层核验会怎么判。"""
    s = summarize_verify(rows)
    lines = [
        VERIFY_HEADING,
        "",
        "base = 父提交，head = 父提交 + 补丁，考卷 = 封存的 L2 测试；不调 LLM，不算考卷强度。",
        "",
        f"- 金标准没修好的 {s['bad']} 个：被驳回 {s['bad_refuted']}",
        f"- 金标准修好的 {s['good']} 个：通过 {s['good_verified']}、被误驳回 {s['good_refuted']}",
        "",
        "| issue | 次 | 金标准 | 金标准里弄坏的 | ClaimVerify | 理由 | 第三层新增失败 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in sorted(rows, key=lambda r: (-r["number"], r["rep"])):
        l3 = r.get("layer3") or {}
        new = l3.get("new_failures") or []
        lines.append(
            f"| #{r['number']} | {r['rep']} | {'✅' if r['gold_resolved'] else '❌'} | "
            f"{r['broken_n']} | {r['verdict']} | {', '.join(r['reasons']) or '—'} | "
            f"{len(new)}（第三层 {l3.get('status', '—')}） |")
    return "\n".join(lines) + "\n"
