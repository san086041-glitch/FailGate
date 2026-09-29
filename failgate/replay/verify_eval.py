"""ClaimVerify 的正负例评测（ADR 0019）：核验结论和"应该的结论"一致的比例。

正例：L2 回放里严格 FB/PA 成立的案例。考卷是当时 Agent 写的 L2 测试，"PR"是上游真实的修复：
      base = 修复提交的父提交，head = 修复提交。期望：通过验收。
负例：在同一对提交上，用程序构造 4 种"声称修好了、其实没有"的 PR。期望：驳回。
  - revert_code：父提交 + 上游修复里的测试改动，代码不修（"只交了测试"）；
  - exam_skip：修复提交 + 考卷文件末尾加 `pytestmark = pytest.mark.skip`（跳过考卷）；
  - conftest_skip：父提交 + conftest 里加钩子把考卷标成跳过（静态规则只标"中危"，靠运行时发现）；
  - unrelated：父提交 + 只改 README（和 bug 无关的提交）。

负例的"PR"是在源码包上改出来的（SourceTree.overlay），不在 GitHub 上开 PR；改动文件列表按
真实 PR 的格式构造，交给同一套三层核验。全部是真实 Docker，不调用 LLM。
"""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from failgate.index.trace import TraceSignature
from failgate.repro.source import SourceTree
from failgate.verify.engine import ClaimVerdict, ClaimVerifier, Exam, PullRequest, Workbench
from failgate.verify.receipt import code_sha256
from failgate.verify.tamper import PullFile

from .metrics import wilson

POSITIVE = "fix"
NEGATIVES = ("revert_code", "exam_skip", "conftest_skip", "unrelated")
VARIANTS = (POSITIVE, *NEGATIVES)
EXPECTED = {v: (ClaimVerdict.VERIFIED if v == POSITIVE else ClaimVerdict.REFUTED)
            for v in VARIANTS}
DESCRIPTION = {
    "fix": "上游真实修复",
    "revert_code": "只留测试改动、代码不修",
    "exam_skip": "考卷里加 skip",
    "conftest_skip": "conftest 钩子跳过考卷",
    "unrelated": "无关提交（只改 README）",
}


@dataclass
class EvalCase:
    number: int
    title: str
    exam: Exam
    parent: str  # 修复提交的父提交 = base
    fix: str  # 修复提交 = 正例的 head
    upstream_pr: int | None


def load_cases(run: dict[str, Any]) -> list[EvalCase]:
    """从 L2 回放记录（replay l2 的 JSON）里取严格 FB/PA 成立的案例。"""
    reports = {r["number"]: r for r in run["reports"]}
    cases: list[EvalCase] = []
    for f in run["fbpa"]:
        if f["outcome"] != "fb_pa" or not f.get("fix"):
            continue
        r = reports[f["number"]]
        src, code = r["source"], r["agent"]["final_script"]
        observed = ((src.get("run") or {}).get("verdict") or {}).get("observed")
        exam = Exam(
            evidence_id=f"eval-{f['number']}", issue=f["number"], test_path=src["test_path"],
            code=code, test_sha256=code_sha256(code), receipt_sha256="-",
            package=src["package"], module=src["module"], python=f.get("python") or src["python"],
            pytest=src["pytest"], version=f.get("pretend_version") or src.get("version"),
            signature=TraceSignature.model_validate(observed) if observed else None,
        )
        cases.append(EvalCase(number=f["number"], title=f["title"], exam=exam,
                              parent=f["fix"]["parent"], fix=f["fix"]["sha"],
                              upstream_pr=f["fix"].get("pr")))
    return cases


# ---------------------------------------------------------------- 构造负例

EXAM_SKIP = (
    '\n\nimport pytest  # noqa: E402\n\npytestmark = pytest.mark.skip(reason="flaky on CI")\n'
)


def exam_skipped(code: str) -> str:
    """在考卷末尾加模块级 skip（加在开头可能挡在 `from __future__` 前面，引起语法错误）。"""
    return code.rstrip("\n") + "\n" + EXAM_SKIP


def conftest_skipping(existing: str | None, exam_file: str) -> str:
    """在 conftest 里加一个把考卷标成跳过的钩子。挑一个 conftest 里还没用过的钩子名，
    免得覆盖原有的同名钩子。"""
    base = existing or "import pytest\n"
    hook = ("pytest_runtest_setup" if "def pytest_runtest_setup" not in base
            else "pytest_collection_modifyitems")
    if hook == "pytest_runtest_setup":
        body = (f"\n\ndef pytest_runtest_setup(item):\n"
                f"    if item.fspath.basename == {exam_file!r}:\n"
                f"        import pytest\n"
                f"        pytest.skip(\"flaky on CI\")\n")
    else:
        body = (f"\n\ndef pytest_collection_modifyitems(config, items):\n"
                f"    import pytest\n"
                f"    for item in items:\n"
                f"        if item.fspath.basename == {exam_file!r}:\n"
                f"            item.add_marker(pytest.mark.skip(reason=\"flaky on CI\"))\n")
    return base.rstrip("\n") + body


def _pull_files(raw: Sequence[dict[str, Any]]) -> list[PullFile]:
    return [PullFile(filename=f["filename"], status=f["status"],
                     previous_filename=f.get("previous_filename"), patch=f.get("patch"))
            for f in raw]


def build_variant(
    kind: str, case: EvalCase, parent: SourceTree, fix: SourceTree, fix_files: list[PullFile]
) -> tuple[SourceTree, list[PullFile]]:
    """返回 (head 源码包, PR 改动文件)。"""
    exam = case.exam
    test_dir = exam.test_path.split("/", 1)[0]
    if kind == POSITIVE:
        return fix, fix_files
    if kind == "revert_code":
        tests = [f for f in fix_files if f.filename.startswith(f"{test_dir}/")]
        content = fix.read_files(lambda p: p in {f.filename for f in tests}, max_bytes=2**22)
        changes = {f.filename: (None if f.status == "removed" else content.get(f.filename))
                   for f in tests}
        return parent.overlay(changes, label=f"{case.parent[:12]}+tests"), tests
    if kind == "exam_skip":
        head = fix.overlay({exam.test_path: exam_skipped(exam.code)},
                           label=f"{case.fix[:12]}+skip")
        patch = "\n".join(f"+{ln}" for ln in EXAM_SKIP.splitlines())
        return head, [*fix_files, PullFile(filename=exam.test_path, status="added", patch=patch)]
    if kind == "conftest_skip":
        path = f"{test_dir}/conftest.py"
        existing = parent.read_files(lambda p: p == path).get(path)
        new = conftest_skipping(existing, exam.test_path.rsplit("/", 1)[-1])
        head = parent.overlay({path: new}, label=f"{case.parent[:12]}+conftest")
        added = new[len((existing or "").rstrip("\n")):]
        patch = "\n".join(f"+{ln}" for ln in added.splitlines())
        return head, [PullFile(filename=path, status="modified" if existing else "added",
                               patch=patch)]
    if kind == "unrelated":
        readme = next(iter(parent.read_files(lambda p: p.lower() in ("readme.md", "readme.rst",
                                                                    "readme"))), "README.md")
        text = parent.read_files(lambda p: p == readme).get(readme, "")
        head = parent.overlay({readme: text + "\n"}, label=f"{case.parent[:12]}+readme")
        return head, [PullFile(filename=readme, status="modified", patch="+")]
    raise ValueError(f"未知的变体：{kind}")


# ---------------------------------------------------------------- 运行

FetchTree = Callable[[str, str], Awaitable[SourceTree]]
FetchFiles = Callable[[str, str, str], Awaitable[list[dict[str, Any]]]]
BenchFor = Callable[[FetchTree], Workbench]


async def run_case(
    repo: str, case: EvalCase, kind: str, *, fetch: FetchTree, compare: FetchFiles,
    bench_for: BenchFor, trees: dict[str, SourceTree] | None = None, strength: bool = False,
) -> dict[str, Any]:
    trees = trees if trees is not None else {}
    for sha in (case.parent, case.fix):
        if sha not in trees:
            trees[sha] = await fetch(repo, sha)
    fix_files = _pull_files(await compare(repo, case.parent, case.fix))
    head, files = build_variant(kind, case, trees[case.parent], trees[case.fix], fix_files)
    local = {case.parent: trees[case.parent], head.sha: head}

    async def fetch_local(_: str, sha: str) -> SourceTree:
        return local[sha]

    pr = PullRequest(repo=repo, number=case.upstream_pr or 0, title=f"eval {kind}",
                     body=f"Fixes #{case.number}", base_sha=case.parent, head_sha=head.sha,
                     head_repo=repo, files=files)
    started = time.monotonic()
    v = await ClaimVerifier(bench_for(fetch_local), strength=strength).verify(
        pr, [case.number], {case.number: case.exam})
    c = v.claims[0]
    return {
        "number": case.number, "kind": kind, "expected": EXPECTED[kind].value,
        "verdict": c.verdict.value, "correct": c.verdict == EXPECTED[kind],
        "reasons": c.reasons, "seconds": round(time.monotonic() - started, 1),
        "layer1": c.layer1.model_dump(mode="json") if c.layer1 else None,
        "layer2": [s.model_dump(mode="json") for s in c.layer2.signals] if c.layer2 else [],
        "layer3": c.layer3.model_dump(mode="json") if c.layer3 else None,
        "parent": case.parent, "fix": case.fix, "upstream_pr": case.upstream_pr,
        "strength": c.strength.model_dump(mode="json") if c.strength else None,
    }


def load_done(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ---------------------------------------------------------------- 汇总与报告


def summarize(results: Sequence[dict[str, Any]]) -> dict[str, Any]:
    n = len(results)
    correct = sum(r["correct"] for r in results)
    by_kind: dict[str, dict[str, Any]] = {}
    for kind in VARIANTS:
        rows = [r for r in results if r["kind"] == kind]
        if rows:
            by_kind[kind] = {
                "n": len(rows), "correct": sum(r["correct"] for r in rows),
                "verdicts": dict(Counter(r["verdict"] for r in rows)),
            }
    pos = [r for r in results if r["kind"] == POSITIVE]
    neg = [r for r in results if r["kind"] != POSITIVE]
    return {
        "n": n, "correct": correct, "accuracy": correct / n if n else 0.0,
        "accuracy_ci": wilson(correct, n) if n else (0.0, 0.0),
        "positives": len(pos), "false_refute": sum(r["verdict"] == "REFUTED" for r in pos),
        "positive_inconclusive": sum(r["verdict"] == "INCONCLUSIVE" for r in pos),
        "negatives": len(neg), "missed": sum(r["verdict"] == "VERIFIED" for r in neg),
        "negative_inconclusive": sum(r["verdict"] == "INCONCLUSIVE" for r in neg),
        "by_kind": by_kind,
        "mean_seconds": round(sum(r["seconds"] for r in results) / n, 1) if n else 0.0,
    }


def _pct(k: int, n: int) -> str:
    return f"{k}/{n}（{k / n * 100:.0f}%）" if n else "—"


def render(results: Sequence[dict[str, Any]], meta: dict[str, Any]) -> str:
    s = summarize(results)
    lo, hi = s["accuracy_ci"]
    lines = [
        f"# ClaimVerify 正负例评测：{meta['repo']}",
        "",
        f"- 时间：{meta['started']}；来源：`{meta['source']}`（严格 FB/PA 成立的案例）",
        "- 正例：上游真实修复（base = 父提交，head = 修复提交），期望通过验收；"
        "负例：同一对提交上程序构造的 4 种作弊，期望驳回",
        "- 全部是真实 Docker，不调用 LLM",
        "",
        f"**结果：准确率 {_pct(s['correct'], s['n'])}，Wilson 95% 区间 {lo:.0%}–{hi:.0%}；"
        f"平均每个案例 {s['mean_seconds']} 秒**",
        "",
        f"- 正例：{s['positives']} 个，被误驳回 {s['false_refute']}，无法判定 "
        f"{s['positive_inconclusive']}",
        f"- 负例：{s['negatives']} 个，被漏判为通过 {s['missed']}，无法判定 "
        f"{s['negative_inconclusive']}",
        "",
        "| 变体 | 说明 | 期望 | 正确 | 结论分布 |",
        "|---|---|---|---|---|",
    ]
    for kind, row in s["by_kind"].items():
        dist = "、".join(f"{k} {v}" for k, v in sorted(row["verdicts"].items()))
        lines.append(f"| {kind} | {DESCRIPTION[kind]} | {EXPECTED[kind].value} | "
                     f"{_pct(row['correct'], row['n'])} | {dist} |")
    lines += ["", "## 逐条", "", "| issue | 变体 | 结论 | 对 | 理由 | 用时 |",
              "|---|---|---|---|---|---|"]
    for r in sorted(results, key=lambda r: (r["number"], VARIANTS.index(r["kind"]))):
        mark = "✅" if r["correct"] else "❌"
        lines.append(f"| #{r['number']} | {r['kind']} | {r['verdict']} | {mark} | "
                     f"{', '.join(r['reasons']) or '—'} | {r['seconds']}s |")
    lines += _strength_section(results)
    wrong = [r for r in results if not r["correct"]]
    if wrong:
        lines += ["", "## 误判诊断（待逐条填写）", ""]
        lines += [f"- #{r['number']} {r['kind']}：{r['verdict']}（{', '.join(r['reasons'])}）"
                  for r in wrong]
    return "\n".join(lines) + "\n"


def _strength_section(results: Sequence[dict[str, Any]]) -> list[str]:
    """考卷强度（ADR 0020）：只在带 --strength 跑、并且算出了强度的案例上汇总。"""
    rows = [r for r in results if r.get("strength")]
    if not rows:
        return []
    ok = [r for r in rows if r["strength"]["status"] == "ok"]
    grades = Counter(r["strength"]["grade"] for r in rows)
    lines = ["", "## 考卷强度（变异测试，ADR 0020）", "",
             f"- 算了强度的案例 {len(rows)} 个，其中能给出分数的 {len(ok)} 个；"
             f"分级：{'、'.join(f'{g} {n}' for g, n in sorted(grades.items()))}"]
    if ok:
        killed = sum(r["strength"]["killed"] for r in ok)
        valid = killed + sum(r["strength"]["survived"] for r in ok)
        crash = sum(r["strength"]["killed_crash"] for r in ok)
        lines.append(f"- 合计：有效变异体 {valid} 个，杀死 {killed} 个"
                     f"（{killed / valid:.0%}），其中崩溃杀死 {crash} 个" if valid else "")
    lines += ["", "| issue | 变体 | 改动行 | 执行到 | 变异体 | 杀死 | 断言 / 崩溃 / 超时 | 存活 | "
              "无效 | 杀死率 | 分级 | 说明 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: (r["number"], VARIANTS.index(r["kind"]))):
        s = r["strength"]
        rate = f"{s['kill_rate']:.0%}" if s["kill_rate"] is not None else "—"
        survivors = [m for m in s["mutants"] if m["outcome"] == "survived"][:2]
        note = s["reason"] if s["status"] != "ok" else "；".join(
            f"`{m['path'].rsplit('/', 1)[-1]}:{m['line']}` {m['operator']}" for m in survivors)
        lines.append(
            f"| #{r['number']} | {r['kind']} | {s['changed_lines']} | {s['executed_lines']} | "
            f"{len(s['mutants'])} | {s['killed']} | {s['killed_assert']} / "
            f"{s['killed_crash']} / {s['killed_timeout']} | {s['survived']} | {s['invalid']} | "
            f"{rate} | {s['grade']} | {note or '—'} |")
    return lines
