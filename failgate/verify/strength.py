"""考卷强度：对修复改动过、并且考卷真的执行到的行做变异测试（技术方案 9.5 节，ADR 0020）。

"测试通过 ≠ 修对了"。第一层通过之后再问一句：如果把修复写下的代码悄悄改坏，考卷能察觉吗？

    合并基点 / head 的源文件 ──difflib──▶ head 上改动过的行
    head 上跑一次考卷（coverage 驱动脚本）──▶ 考卷执行到的行 ──∩──▶ 目标行
    cosmic-ray 的算子（外加两个自带算子）在宿主机上把源码当文本改 ──▶ 最多 30 个变异体
    同一个工作区里逐个换上变异体跑考卷（断网、只读根），跑完换回原文件
        exit 1 → 杀死（区分断言失败 / 崩溃）；2（import 时就崩）→ 崩溃杀死；超时 → 杀死（单列）；
        0 → 存活；3–5、OOM → 无效

设计要点：
- 变异体在宿主机生成（只把 PR 的源码当文本解析，不执行），沙箱里仍然只跑 pytest，
  被测环境里不装变异工具；只多装一个 coverage，放在单独的环境里（不影响核验用的环境）；
- 测试导入的是工作区里的源码副本：PYTHONPATH 指向副本的导入根目录，并用 coverage 的数据
  确认"执行的确实是副本"，否则如实给 n/a（shadow），不拿 site-packages 里的原文件充数；
- 算子做了挑选：cosmic-ray 自带 213 个，其中 ReplaceBinaryOperator 的 132 个大多会变成
  str + int 这类必然崩溃的代码，只要执行到就"杀死"，会把杀死率虚抬。只保留语义上相邻的替换
  （< ↔ <=、+ ↔ -、and ↔ or 等），再加"删语句""删 if 分支""返回 None"三个自带算子；
- 强度不改变核验结论，只附加在报告里。杀死率是下限（等价变异体无法自动识别）。
- 只变异 diff 行，所以看不到"修复漏掉的情况"（迎合考卷的过拟合修复，如演示仓库 PR #7），
  那是隐藏考卷（W5 第二步）的职责。
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Literal, Protocol

import parso
from cosmic_ray import plugins
from cosmic_ray.ast import ast_nodes
from cosmic_ray.mutating import MutationVisitor
from cosmic_ray.operators.operator import Operator
from parso.python import tree as pytree
from pydantic import BaseModel

from failgate.repro.l2 import PYTEST_INVALID
from failgate.repro.sandbox import ExecResult

from .tamper import is_test_file

MAX_MUTANTS = 30
MIN_TIMEOUT_S = 15
TIMEOUT_FACTOR = 3
STRONG, MEDIUM = 0.8, 0.5  # 初始阈值，用评测数据校准
WORKSPACE_SRC = "/workspace/src"  # 工作区里源码副本的位置（和 l2.WORK_SRC 对应）
NON_SOURCE_DIRS = frozenset({"tests", "test", "testing", "docs", "doc", "examples", "scripts",
                             "benchmarks", ".github"})
COVERAGE_MARK = "@@FAILGATE_COVERAGE@@"

# 在沙箱里用 coverage 的 API 跑 pytest，结束后只把关心的文件（argv[1]，JSON 列表：工作区副本的
# 相对路径和 site-packages 里的相对路径）执行过的行打印成一行 JSON。只打印这几个文件：
# 沙箱输出有 64 KB 上限，全量打印会被截断（black 上实测）。
# 和 INSTALLER 一样按整段脚本精确匹配白名单，内容一个字都不能改。
COVERAGE_DRIVER = r'''
import json, sys
import coverage, pytest
want = json.loads(sys.argv[1])
cov = coverage.Coverage(data_file=None, include=["/workspace/src/*", "*/site-packages/*"])
cov.start()
code = pytest.main(sys.argv[2:])
cov.stop()
data = cov.get_data()
out = {}
for f in data.measured_files():
    if any(f == "/workspace/src/" + w or f.endswith("/site-packages/" + w) for w in want):
        out[f] = sorted(data.lines(f) or [])
print("@@FAILGATE_COVERAGE@@" + json.dumps(out))
sys.exit(int(code))
'''
COVERAGE_PREFIX = ("python", "-c", COVERAGE_DRIVER)


# ---------------------------------------------------------------- 算子

# cosmic-ray 自带算子里保留的（语义上相邻、不会大面积制造类型错误的）
_PAIRS = {
    "Comparison": ["Lt_LtE", "LtE_Lt", "Gt_GtE", "GtE_Gt", "Eq_NotEq", "NotEq_Eq", "Is_IsNot",
                   "IsNot_Is"],
    "Binary": ["Add_Sub", "Sub_Add", "Mul_FloorDiv", "Div_Mul", "FloorDiv_Mul", "BitAnd_BitOr",
               "BitOr_BitAnd", "LShift_RShift", "RShift_LShift"],
    "Unary": ["Delete_Not", "USub_UAdd"],  # 去掉 not；-x → +x
}
_SINGLE = ["NumberReplacer", "AddNot", "ReplaceTrueWithFalse", "ReplaceFalseWithTrue",
           "ReplaceAndWithOr", "ReplaceOrWithAnd", "ReplaceBreakWithContinue",
           "ReplaceContinueWithBreak", "ZeroIterationForLoop", "ExceptionReplacer"]


class DeleteStatement(Operator):
    """删语句：把一条简单语句（赋值、表达式、raise、del 等）换成 pass。"""

    KINDS = frozenset({"expr_stmt", "raise_stmt", "del_stmt", "assert_stmt", "return_stmt"})

    def mutation_positions(self, node: Any) -> Iterator[tuple[Any, Any]]:
        if self._bare_annotation(node):
            return  # `x: int` 只是注解，删掉运行时行为不变（等价变异体）
        if node.type in self.KINDS or (
            node.type in ("power", "atom_expr") and node.parent.type == "simple_stmt"
        ):
            yield node.start_pos, node.end_pos

    @staticmethod
    def _bare_annotation(node: Any) -> bool:
        if node.type != "expr_stmt":
            return False
        ann = next((c for c in node.children if c.type == "annassign"), None)
        return ann is not None and not any(getattr(c, "value", None) == "=" for c in ann.children)

    def mutate(self, node: Any, index: int) -> Any:
        prefix = node.get_first_leaf().prefix
        return pytree.Keyword("pass", node.start_pos, prefix=prefix)

    @classmethod
    def examples(cls) -> tuple[()]:
        return ()


class ReplaceCondition(Operator):
    """删 if / elif 分支：条件换成 True（总进这个分支）或 False（永远不进）。"""

    VALUES = ("True", "False")

    def mutation_positions(self, node: Any) -> Iterator[tuple[Any, Any]]:
        parent = node.parent
        if parent is None or parent.type != "if_stmt":
            return
        prev = node.get_previous_sibling()
        if prev is not None and prev.type == "keyword" and prev.value in ("if", "elif"):
            for _ in self.VALUES:
                yield node.start_pos, node.end_pos

    def mutate(self, node: Any, index: int) -> Any:
        prefix = node.get_first_leaf().prefix
        return pytree.Keyword(self.VALUES[index], node.start_pos, prefix=prefix)

    @classmethod
    def examples(cls) -> tuple[()]:
        return ()


class ReturnNone(Operator):
    """返回值换成 None：return x → return None。"""

    def mutation_positions(self, node: Any) -> Iterator[tuple[Any, Any]]:
        if node.type == "return_stmt" and len(node.children) == 2:
            value = node.children[1]
            if not (value.type == "keyword" and value.value == "None"):
                yield node.start_pos, node.end_pos

    def mutate(self, node: Any, index: int) -> Any:
        value = node.children[1]
        prefix = value.get_first_leaf().prefix
        node.children[1] = pytree.Keyword("None", value.start_pos, prefix=prefix)
        node.children[1].parent = node
        return node

    @classmethod
    def examples(cls) -> tuple[()]:
        return ()


def operators() -> list[tuple[str, Operator]]:
    """(名字, 算子实例)，名字进报告和收据。"""
    out: list[tuple[str, Operator]] = []
    for family, pairs in _PAIRS.items():
        for pair in pairs:
            name = f"core/Replace{family}Operator_{pair}"
            out.append((name.removeprefix("core/"), plugins.get_operator(name)()))
    out += [(n, plugins.get_operator(f"core/{n}")()) for n in _SINGLE]
    out += [("DeleteStatement", DeleteStatement()), ("ReplaceCondition", ReplaceCondition()),
            ("ReturnNone", ReturnNone())]
    return out


# ---------------------------------------------------------------- 纯函数：改动行、变异体


def is_source_file(path: str) -> bool:
    """会被 import 的源码：.py、不是测试、不在 tests/docs 等目录、不是仓库根的脚本。"""
    parts = path.split("/")
    if not path.endswith(".py") or is_test_file(path) or len(parts) < 2:
        return False
    return not any(p in NON_SOURCE_DIRS for p in parts[:-1])


def import_root(path: str) -> str:
    """源文件所在的导入根目录（相对仓库根）：src 布局是 src，否则是仓库根。"""
    return "src" if path.startswith("src/") else ""


def expand_executed(source: str, executed: set[int]) -> set[int]:
    """coverage 只把一条语句记在它的第一行：多行语句（跨行的调用、条件）的后几行不会出现在
    执行行里。按语句展开：第一行执行到了，这条语句的每一行都算执行到（复合语句只展开到
    冒号那一行，不包括语句体）。解析不了就原样返回。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set(executed)
    out = set(executed)
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt) or node.lineno not in executed:
            continue
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
            end = body[0].lineno - 1  # 复合语句：只到语句体之前
        else:
            end = node.end_lineno or node.lineno
        out.update(range(node.lineno, max(end, node.lineno) + 1))
    return out


def changed_lines(base: str | None, head: str) -> set[int]:
    """head 上新增或改写的行号（从 1 开始）。base 为 None 表示新文件。"""
    head_lines = head.splitlines()
    out: set[int] = set()
    if base is None:
        out = set(range(1, len(head_lines) + 1))
    else:
        sm = difflib.SequenceMatcher(a=base.splitlines(), b=head_lines, autojunk=False)
        for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
            if tag in ("replace", "insert"):
                out.update(range(j1 + 1, j2 + 1))
    # 空行和纯注释行没有可变异的代码
    return {n for n in out
            if (s := head_lines[n - 1].strip()) and not s.startswith("#")}


class Mutant(BaseModel):
    path: str
    line: int
    operator: str
    occurrence: int
    before: str  # 原来那一行（去掉首尾空白）
    after: str  # 变异后那一行
    code: str = ""  # 变异后的整个文件（不进收据）

    def key(self) -> str:
        return f"{self.path}:{self.line}:{self.operator}:{self.occurrence}"


def _first_changed_line(a: str, b: str) -> int | None:
    for i, (x, y) in enumerate(zip(a.splitlines(), b.splitlines(), strict=False), start=1):
        if x != y:
            return i
    return None


def mutants_for_file(path: str, source: str, lines: set[int], *, python: str | None = None
                     ) -> list[Mutant] | None:
    """在 lines 上能做的全部变异体。解析出错（语法比 parso 支持的新等）时返回 None。"""
    try:  # 按目标代码的 Python 版本解析；parso 不认识的版本退回默认语法
        if python is not None and re.fullmatch(r"3\.\d+", python):
            grammar = parso.load_grammar(version=python)
        else:
            grammar = parso.load_grammar()
    except (NotImplementedError, ValueError):
        grammar = parso.load_grammar()
    module = grammar.parse(source)
    if next(iter(grammar.iter_errors(module)), None) is not None:
        return None
    src_lines = source.splitlines()
    out: list[Mutant] = []
    seen: set[str] = set()
    for name, op in operators():
        occurrence = 0
        for node in ast_nodes(module):
            for start, _end in op.mutation_positions(node):
                if start[0] in lines:
                    visitor = MutationVisitor(occurrence, op)
                    mutated = visitor.walk(grammar.parse(source)).get_code()
                    line = (_first_changed_line(source, mutated)
                            if visitor.mutation_applied else None)
                    digest = hashlib.sha256(mutated.encode()).hexdigest()
                    # 只接受改动确实落在目标行上、并且和别的变异体不重复的
                    if line is not None and line == start[0] and digest not in seen:
                        seen.add(digest)
                        out.append(Mutant(
                            path=path, line=line, operator=name, occurrence=occurrence,
                            before=src_lines[line - 1].strip(),
                            after=mutated.splitlines()[line - 1].strip(), code=mutated,
                        ))
                occurrence += 1
    return out


def sample(mutants: Sequence[Mutant], limit: int, seed: str) -> list[Mutant]:
    """按行轮流挑，同一行里按算子轮流，顺序由 seed 决定（可复现）。"""
    def rank(m: Mutant) -> str:
        return hashlib.sha256(f"{seed}:{m.key()}".encode()).hexdigest()

    by_line: dict[tuple[str, int], list[Mutant]] = {}
    for m in sorted(mutants, key=rank):
        by_line.setdefault((m.path, m.line), []).append(m)
    for key, group in by_line.items():
        # 同一行里先让不同的算子各出一个
        firsts: dict[str, Mutant] = {}
        rest: list[Mutant] = []
        for m in group:
            if m.operator in firsts:
                rest.append(m)
            else:
                firsts[m.operator] = m
        by_line[key] = [*firsts.values(), *rest]
    out: list[Mutant] = []
    queues = [by_line[k] for k in sorted(by_line)]
    while len(out) < limit and any(queues):
        for q in queues:
            if q and len(out) < limit:
                out.append(q.pop(0))
    return out


# ---------------------------------------------------------------- 纯函数：运行结果


def parse_coverage(run: ExecResult) -> dict[str, set[int]] | None:
    for line in reversed((run.stdout or "").splitlines()):
        if line.startswith(COVERAGE_MARK):
            try:
                raw = json.loads(line[len(COVERAGE_MARK):])
            except json.JSONDecodeError:
                return None
            return {str(f): set(v) for f, v in raw.items()}
    return None


def installed_path(path: str) -> str:
    """源文件装进 site-packages 后的相对路径（去掉导入根目录）。"""
    root = import_root(path)
    return path[len(root) + 1:] if root else path


def executed_in_copy(cov: Mapping[str, set[int]], path: str) -> set[int] | None:
    """考卷在工作区副本里执行到的行；副本没被执行、而 site-packages 里同名模块被执行了
    （PYTHONPATH 没生效）时返回 None。"""
    copy = f"{WORKSPACE_SRC}/{path}"
    if copy in cov:
        return cov[copy]
    if any(f.endswith(f"/site-packages/{installed_path(path)}") for f in cov):
        return None
    return set()


MutantOutcome = Literal["killed_assert", "killed_crash", "killed_timeout", "survived", "invalid"]


def classify_mutant(run: ExecResult) -> MutantOutcome:
    """exit 2（收集出错）算崩溃杀死：cosmic-ray 的变异体语法都合法，收集出错说明变异体在
    import 时就抛了异常（比如删掉了模块级的定义），考卷根本加载不起来——这也是"察觉到了"。"""
    if run.timed_out:
        return "killed_timeout"
    if run.exit_code == 2 and not run.oom_killed:
        return "killed_crash"
    if run.oom_killed or run.exit_code in PYTEST_INVALID:
        return "invalid"
    if run.exit_code == 0:
        return "survived"
    text = run.stdout + "\n" + run.stderr
    return "killed_assert" if "AssertionError" in text else "killed_crash"


Grade = Literal["strong", "medium", "weak", "n/a"]


def grade(kill_rate: float | None) -> Grade:
    if kill_rate is None:
        return "n/a"
    return "strong" if kill_rate >= STRONG else "medium" if kill_rate >= MEDIUM else "weak"


class MutantResult(BaseModel):
    path: str
    line: int
    operator: str
    before: str
    after: str
    outcome: MutantOutcome
    seconds: float = 0.0


class StrengthReport(BaseModel):
    # ok / no_source_change / not_executed / shadow / baseline_failed / parse_error /
    # no_mutants / setup
    status: Literal["ok", "n/a"]
    reason: str
    detail: str = ""
    changed_lines: int = 0  # 修复改动过的源码行
    executed_lines: int = 0  # 其中考卷执行到的
    unexecuted: list[str] = []  # 没执行到的改动行（path:line，最多 10 个）
    candidates: int = 0  # 目标行上能做的变异体总数（抽样前）
    mutants: list[MutantResult] = []
    killed: int = 0
    killed_assert: int = 0
    killed_crash: int = 0
    killed_timeout: int = 0
    survived: int = 0
    invalid: int = 0
    kill_rate: float | None = None  # killed / (killed + survived)，等价变异体使它偏低
    grade: Grade = "n/a"

    @property
    def survivors(self) -> list[MutantResult]:
        return [m for m in self.mutants if m.outcome == "survived"]


def summarize(results: list[MutantResult], **kw: Any) -> StrengthReport:
    counts = {o: sum(r.outcome == o for r in results)
              for o in ("killed_assert", "killed_crash", "killed_timeout", "survived", "invalid")}
    killed = counts["killed_assert"] + counts["killed_crash"] + counts["killed_timeout"]
    valid = killed + counts["survived"]
    rate = round(killed / valid, 4) if valid else None
    status: Literal["ok", "n/a"] = "ok" if valid else "n/a"
    reason = "ok" if valid else "no_mutants"
    return StrengthReport(status=status, reason=reason, mutants=results, killed=killed,
                          kill_rate=rate, grade=grade(rate), **counts, **kw)


# ---------------------------------------------------------------- 编排


class StrengthBench(Protocol):
    """强度评估需要的沙箱操作（线上是 workbench.SandboxWorkbench，测试里是假的）。"""

    async def prepare_strength(self, repo: str, sha: str, exam: Any) -> Any: ...
    async def open_strength(self, prepared: Any, exam: Any) -> Any: ...
    async def run_coverage(self, handle: Any, exam: Any, pythonpath: str, watch: list[str]
                           ) -> ExecResult: ...
    async def put_file(self, handle: Any, path: str, content: str) -> None: ...
    async def run_mutant(self, handle: Any, exam: Any, pythonpath: str, timeout_s: int
                         ) -> ExecResult: ...
    async def close_strength(self, handle: Any) -> None: ...


class StrengthEvaluator:
    def __init__(self, bench: StrengthBench, *, max_mutants: int = MAX_MUTANTS) -> None:
        self.bench = bench
        self.max_mutants = max_mutants

    async def evaluate(self, repo: str, head_sha: str, exam: Any,
                       sources: Mapping[str, tuple[str | None, str]]) -> StrengthReport:
        """sources：改动过的源文件 → (合并基点上的内容或 None, head 上的内容)。"""
        changed = {p: changed_lines(b, h) for p, (b, h) in sources.items() if is_source_file(p)}
        changed = {p: ls for p, ls in changed.items() if ls}
        n_changed = sum(len(ls) for ls in changed.values())
        if not changed:
            return StrengthReport(status="n/a", reason="no_source_change")
        roots = {import_root(p) for p in changed}
        pythonpath = ":".join(f"{WORKSPACE_SRC}/{r}".rstrip("/") for r in sorted(roots))
        try:
            prepared = await self.bench.prepare_strength(repo, head_sha, exam)
        except Exception as e:  # noqa: BLE001 — 环境问题只让强度变成 n/a，不影响核验结论
            return StrengthReport(status="n/a", reason="setup", changed_lines=n_changed,
                                  detail=f"{type(e).__name__}: {str(e)[:300]}")
        handle = await self.bench.open_strength(prepared, exam)
        try:
            return await self._run(handle, exam, sources, changed, n_changed, pythonpath)
        finally:
            await self.bench.close_strength(handle)

    async def _run(self, handle: Any, exam: Any, sources: Mapping[str, tuple[str | None, str]],
                   changed: dict[str, set[int]], n_changed: int, pythonpath: str
                   ) -> StrengthReport:
        watch = sorted({w for p in changed for w in (p, installed_path(p))})
        base = await self.bench.run_coverage(handle, exam, pythonpath, watch)
        cov = parse_coverage(base)
        if base.exit_code != 0 or cov is None:
            return StrengthReport(status="n/a", reason="baseline_failed", changed_lines=n_changed,
                                  detail=f"exit={base.exit_code}: {base.output_tail(8)}")
        targets: dict[str, set[int]] = {}
        unexecuted: list[str] = []
        for path, lines in sorted(changed.items()):
            ran = executed_in_copy(cov, path)
            if ran is None:
                return StrengthReport(status="n/a", reason="shadow", changed_lines=n_changed,
                                      detail=path)
            targets[path] = lines & expand_executed(sources[path][1], ran)
            unexecuted += [f"{path}:{n}" for n in sorted(lines - ran)]
        n_exec = sum(len(v) for v in targets.values())
        kw: dict[str, Any] = {"changed_lines": n_changed, "executed_lines": n_exec,
                              "unexecuted": unexecuted[:10]}
        if not n_exec:
            return StrengthReport(status="n/a", reason="not_executed", **kw)
        pool: list[Mutant] = []
        unparsed: list[str] = []
        for path, lines in targets.items():
            if lines:
                found = mutants_for_file(path, sources[path][1], lines, python=exam.python)
                if found is None:
                    unparsed.append(path)
                else:
                    pool += found
        if not pool:
            reason = "parse_error" if unparsed else "no_mutants"
            return StrengthReport(status="n/a", reason=reason, detail=", ".join(unparsed), **kw)
        picked = sample(pool, self.max_mutants, seed=f"{exam.evidence_id}:{exam.test_sha256}")
        timeout = max(MIN_TIMEOUT_S, int(base.duration_s * TIMEOUT_FACTOR) + 1)
        results: list[MutantResult] = []
        for m in picked:
            await self.bench.put_file(handle, m.path, m.code)
            try:
                run = await self.bench.run_mutant(handle, exam, pythonpath, timeout)
            finally:
                await self.bench.put_file(handle, m.path, sources[m.path][1])
            results.append(MutantResult(path=m.path, line=m.line, operator=m.operator,
                                        before=m.before, after=m.after,
                                        outcome=classify_mutant(run),
                                        seconds=round(run.duration_s, 2)))
        return summarize(results, candidates=len(pool), **kw)
