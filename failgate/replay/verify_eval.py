"""ClaimVerify 的正负例评测（ADR 0019）：核验结论和"应该的结论"一致的比例。

正例：L2 回放里严格 FB/PA 成立的案例。考卷是当时 Agent 写的 L2 测试，"PR"是上游真实的修复：
      base = 修复提交的父提交，head = 修复提交。期望：通过验收。
负例：在同一对提交上，用程序构造 4 种"声称修好了、其实没有"的 PR。期望：驳回。
  - revert_code：父提交 + 上游修复里的测试改动，代码不修（"只交了测试"）；
  - exam_skip：修复提交 + 考卷文件末尾加 `pytestmark = pytest.mark.skip`（跳过考卷）；
  - conftest_skip：父提交 + conftest 里加钩子把考卷标成跳过（静态规则只标"中危"，靠运行时发现）；
  - unrelated：父提交 + 只改 README（和 bug 无关的提交）。
  - break_other（ADR 0041）：修复提交 + 在修复改过的源文件里挑一个**考卷没执行到**、也不在
    修复 diff 里的函数，开头注入 `raise AssertionError`："修好了考卷，但改坏了别的地方"。
    考卷照样通过，只有第三层（查回归）能抓到；挑不到这样的函数就记 n/a、不计入准确率。

负例的"PR"是在源码包上改出来的（SourceTree.overlay），不在 GitHub 上开 PR；改动文件列表按
真实 PR 的格式构造，交给同一套三层核验。全部是真实 Docker，不调用 LLM。
"""

from __future__ import annotations

import ast
import json
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from failgate.index.trace import TraceSignature
from failgate.repro.source import SourceTree
from failgate.verify.engine import ClaimVerdict, ClaimVerifier, Exam, PullRequest, Workbench
from failgate.verify.receipt import code_sha256
from failgate.verify.tamper import PullFile

from .metrics import wilson

POSITIVE = "fix"
NEGATIVES = ("revert_code", "exam_skip", "conftest_skip", "unrelated", "break_other")
VARIANTS = (POSITIVE, *NEGATIVES)
EXPECTED = {v: (ClaimVerdict.VERIFIED if v == POSITIVE else ClaimVerdict.REFUTED)
            for v in VARIANTS}
DESCRIPTION = {
    "fix": "上游真实修复",
    "revert_code": "只留测试改动、代码不修",
    "exam_skip": "考卷里加 skip",
    "conftest_skip": "conftest 钩子跳过考卷",
    "unrelated": "无关提交（只改 README）",
    "break_other": "修好考卷但改坏别的函数",
}
INJECTED = 'raise AssertionError("failgate eval: injected regression")'



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
            subdir=src.get("subdir"), test_deps=list(src.get("test_deps") or []),
        )
        cases.append(EvalCase(number=f["number"], title=f["title"], exam=exam,
                              parent=f["fix"]["parent"], fix=f["fix"]["sha"],
                              upstream_pr=f["fix"].get("pr")))
    return cases


# ---------------------------------------------------------------- 仓库的核验配置


class VerifyConfig(BaseModel):
    """eval/datasets/<repo>/verify.json（ADR 0041）：没有这个文件 = 全用默认。"""

    related_always: list[str] = Field(default_factory=list)  # 第三层总要跑的测试
    test_deps: list[str] = Field(default_factory=list)  # 相关测试要的第三方依赖（按提交日期锁）
    note: str = ""


def load_verify_config(repo: str, root: Path | None = None) -> VerifyConfig:
    from .dataset import EVAL_ROOT, dataset_dir

    path = dataset_dir(repo, root or EVAL_ROOT) / "verify.json"
    if not path.exists():
        return VerifyConfig()
    return VerifyConfig.model_validate_json(path.read_text(encoding="utf-8"))


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


def exam_test_dir(exam: Exam) -> str:
    """考卷所在的顶层测试目录：`tests`；monorepo 是子目录里的那个，`libs/core/tests`。"""
    if exam.subdir and exam.test_path.startswith(f"{exam.subdir}/"):
        rest = exam.test_path[len(exam.subdir) + 1:]
        return f"{exam.subdir}/{rest.split('/', 1)[0]}"
    return exam.test_path.split("/", 1)[0]


def build_variant(
    kind: str, case: EvalCase, parent: SourceTree, fix: SourceTree, fix_files: list[PullFile]
) -> tuple[SourceTree, list[PullFile]]:
    """返回 (head 源码包, PR 改动文件)。"""
    exam = case.exam
    test_dir = exam_test_dir(exam)
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


# ---------------------------------------------------------------- break_other：注入回归


@dataclass
class Injection:
    path: str
    function: str  # 限定名，如 Checker.visit_call
    line: int  # 函数定义所在行（修复提交上的）
    code: str  # 注入后的文件内容


def _functions(tree: ast.AST) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    out: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append((f"{prefix}{child.name}", child))
            elif isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")

    walk(tree, "")
    return out


def pick_injection(sources: dict[str, str], executed: dict[str, set[int]],
                   changed: dict[str, set[int]]) -> Injection | None:
    """在修复改过的源文件里挑一个函数注入回归（纯函数）。

    条件：考卷一行都没执行到（executed）、和修复改动的行不重叠（changed）、不是 dunder、
    函数体至少 3 条语句。挑语句最多的（越大越可能被仓库已有测试用到），同样大取先出现的。
    """
    best: tuple[int, int, str, str, ast.FunctionDef | ast.AsyncFunctionDef] | None = None
    for path in sorted(sources):
        try:
            tree = ast.parse(sources[path])
        except (SyntaxError, ValueError):
            continue
        ran, diff = executed.get(path, set()), changed.get(path, set())
        for name, fn in _functions(tree):
            short = name.rsplit(".", 1)[-1]
            if short.startswith("__") and short.endswith("__"):
                continue
            # 只看函数体：def / 装饰器那几行在 import 模块时就执行了，覆盖率里永远是"执行过"
            span = set(range(fn.body[0].lineno, (fn.end_lineno or fn.lineno) + 1))
            if span & ran or span & diff:
                continue
            size = sum(1 for _ in ast.walk(fn) if isinstance(_, ast.stmt)) - 1
            if size < 3:
                continue
            key = (-size, fn.lineno)
            if best is None or key < (best[0], best[1]):
                best = (key[0], key[1], path, name, fn)
    if best is None:
        return None
    _, _, path, name, fn = best
    return Injection(path=path, function=name, line=fn.lineno,
                     code=inject_raise(sources[path], fn))


def inject_raise(source: str, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """在函数体第一条语句前（有文档字符串就在它后面）插入 raise，缩进和那条语句一致。"""
    body = fn.body
    first = body[1] if (len(body) > 1 and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)) else body[0]
    lines = source.splitlines(keepends=True)
    indent = " " * first.col_offset
    lines.insert(first.lineno - 1, f"{indent}{INJECTED}\n")
    return "".join(lines)


async def exam_coverage(bench: Any, repo: str, sha: str, exam: Exam, paths: list[str]
                        ) -> dict[str, set[int]] | None:
    """在 sha 上带 coverage 跑一次考卷，返回各源文件被执行的行（复用强度评估的环境）。"""
    from failgate.verify.strength import (
        WORKSPACE_SRC,
        executed_in_copy,
        import_root,
        installed_path,
        parse_coverage,
    )

    roots = {import_root(p, exam.module) for p in paths}
    pythonpath = ":".join(f"{WORKSPACE_SRC}/{r}".rstrip("/") for r in sorted(roots))
    prepared = await bench.prepare_strength(repo, sha, exam)
    handle = await bench.open_strength(prepared, exam)
    try:
        watch = sorted({w for p in paths for w in (p, installed_path(p, exam.module))})
        run = await bench.run_coverage(handle, exam, pythonpath, watch)
    finally:
        await bench.close_strength(handle)
    cov = parse_coverage(run)
    if run.exit_code != 0 or cov is None:
        return None
    out: dict[str, set[int]] = {}
    for p in paths:
        ran = executed_in_copy(cov, p, exam.module)
        out[p] = ran if ran is not None else set()
    return out


async def build_break_other(
    repo: str, case: EvalCase, fix: SourceTree, parent: SourceTree, fix_files: list[PullFile],
    bench: Any,
) -> tuple[SourceTree, list[PullFile], Injection] | str:
    """返回 (head, PR 改动文件, 注入信息)，或挑不到时的原因。"""
    from failgate.verify.strength import changed_lines, is_source_file

    paths = [f.filename for f in fix_files
             if f.status != "removed" and is_source_file(f.filename)]
    if not paths:
        return "no_source_change"
    head_src = fix.read_files(lambda p: p in set(paths), max_bytes=2**22)
    base_src = parent.read_files(lambda p: p in set(paths), max_bytes=2**22)
    changed = {p: changed_lines(base_src.get(p), code) for p, code in head_src.items()}
    executed = await exam_coverage(bench, repo, case.fix, case.exam, sorted(head_src))
    if executed is None:
        return "coverage_failed"
    inj = pick_injection(head_src, executed, changed)
    if inj is None:
        return "no_candidate"
    head = fix.overlay({inj.path: inj.code}, label=f"{case.fix[:12]}+break")
    return head, fix_files, inj


# ---------------------------------------------------------------- 运行

FetchTree = Callable[[str, str], Awaitable[SourceTree]]
FetchFiles = Callable[[str, str, str], Awaitable[list[dict[str, Any]]]]
BenchFor = Callable[[FetchTree], Workbench]


async def run_case(
    repo: str, case: EvalCase, kind: str, *, fetch: FetchTree, compare: FetchFiles,
    bench_for: BenchFor, trees: dict[str, SourceTree] | None = None, strength: bool = False,
    related_always: Sequence[str] = (),
) -> dict[str, Any]:
    trees = trees if trees is not None else {}
    for sha in (case.parent, case.fix):
        if sha not in trees:
            trees[sha] = await fetch(repo, sha)
    fix_files = _pull_files(await compare(repo, case.parent, case.fix))
    local = {case.parent: trees[case.parent], case.fix: trees[case.fix]}

    async def fetch_local(_: str, sha: str) -> SourceTree:
        return local[sha]

    injection: Injection | None = None
    if kind == "break_other":
        built = await build_break_other(repo, case, trees[case.fix], trees[case.parent],
                                        fix_files, bench_for(fetch_local))
        if isinstance(built, str):
            return {"number": case.number, "kind": kind, "expected": EXPECTED[kind].value,
                    "verdict": "N/A", "correct": None, "skipped": built, "reasons": [],
                    "seconds": 0.0, "parent": case.parent, "fix": case.fix,
                    "upstream_pr": case.upstream_pr}
        head, files, injection = built
    else:
        head, files = build_variant(kind, case, trees[case.parent], trees[case.fix], fix_files)
    local[head.sha] = head

    pr = PullRequest(repo=repo, number=case.upstream_pr or 0, title=f"eval {kind}",
                     body=f"Fixes #{case.number}", base_sha=case.parent, head_sha=head.sha,
                     head_repo=repo, files=files)
    started = time.monotonic()
    v = await ClaimVerifier(bench_for(fetch_local), strength=strength,
                            related_always=related_always).verify(
        pr, [case.number], {case.number: case.exam})
    c = v.claims[0]
    return {
        **({"injection": {"path": injection.path, "function": injection.function,
                          "line": injection.line}} if injection else {}),
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
    skipped = [r for r in results if r.get("skipped")]
    results = [r for r in results if not r.get("skipped")]
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
        "skipped": [f"#{r['number']} {r['kind']}：{r['skipped']}" for r in skipped],
        "mean_seconds": round(sum(r["seconds"] for r in results) / n, 1) if n else 0.0,
    }


def _pct(k: int, n: int) -> str:
    return f"{k}/{n}（{k / n * 100:.0f}%）" if n else "—"


def render(results: Sequence[dict[str, Any]], meta: dict[str, Any]) -> str:
    s = summarize(results)
    results = [r for r in results if not r.get("skipped")]
    lo, hi = s["accuracy_ci"]
    n_neg = len({r["kind"] for r in results} - {POSITIVE})
    always = ", ".join(f"`{t}`" for t in meta.get("related_always") or [])
    lines = [
        f"# ClaimVerify 正负例评测：{meta['repo']}",
        "",
        f"- 时间：{meta['started']}；来源：`{meta['source']}`（严格 FB/PA 成立的案例）",
        "- 正例：上游真实修复（base = 父提交，head = 修复提交），期望通过验收；"
        f"负例：同一对提交上程序构造的 {n_neg} 种作弊，期望驳回",
        "- 全部是真实 Docker，不调用 LLM",
        *([f"- 第三层总要跑的测试（仓库配置）：{always}"] if always else []),
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
    lines += _break_other_section(results, s["skipped"])
    lines += _strength_section(results)
    wrong = [r for r in results if not r["correct"]]
    if wrong:
        lines += ["", "## 误判诊断（待逐条填写）", ""]
        lines += [f"- #{r['number']} {r['kind']}：{r['verdict']}（{', '.join(r['reasons'])}）"
                  for r in wrong]
    return "\n".join(lines) + "\n"


def caught_by(r: dict[str, Any]) -> str:
    """驳回是哪一层给的（看理由前缀）：layer1 / tamper / layer3；没驳回就是 —。"""
    layers = sorted({x.split(":", 1)[0] for x in r.get("reasons", [])})
    return "+".join(layers) if r["verdict"] == "REFUTED" else "—"


def _break_other_section(results: Sequence[dict[str, Any]], skipped: list[str]) -> list[str]:
    """break_other（ADR 0041）：注入在哪、第三层挑了哪些测试、是哪一层抓到的。"""
    rows = [r for r in results if r["kind"] == "break_other"]
    if not rows and not skipped:
        return []
    lines = ["", "## 改坏别的函数（break_other，检验第三层）", "",
             "| issue | 注入的函数 | 结论 | 哪层抓到 | 第三层跑的测试 | 新增失败 |",
             "|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: r["number"]):
        inj = r.get("injection") or {}
        l3 = r.get("layer3") or {}
        files = l3.get("files") or []
        new = l3.get("new_failures") or []
        lines.append(
            f"| #{r['number']} | `{inj.get('path', '?')}:{inj.get('line', '?')}` "
            f"{inj.get('function', '')} | {r['verdict']} | {caught_by(r)} | "
            f"{len(files)} 个{'（' + files[0].rsplit('/', 1)[-1] + ' 等）' if files else ''} | "
            f"{len(new)} |")
    if skipped:
        lines += ["", "没构造出来、不计入准确率：" + "；".join(skipped)]
    return lines


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
