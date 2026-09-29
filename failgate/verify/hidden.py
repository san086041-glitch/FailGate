"""隐藏考卷：抓"只迎合了公开考卷"的修复（技术方案 9.5b 节，ADR 0021）。

考卷强度（strength.py）查的是"考卷有没有卡住修复写下的代码"，看不到修复**没写**的情况：
演示仓库 PR #7 只把 3 个连续分隔符换成 1 个，恰好对上公开考卷唯一的输入，三层核验和强度都通过。
做法借鉴 Kaggle 的公开榜 / 私有榜：

    出题（LLM，只看 issue + 公开考卷，不看任何修复）──▶ 3–5 道变体题
    在 issue 时的代码上跑 ──▶ 只留"失败、且异常类型和封存签名一致"的题 ──▶ 删掉其余的题再跑一次确认
    封存（hidden_exams 表，只加不改；收据 failgate.hidden/v1）──▶ 对外只公布 sha256
    核验：第一层通过后在 PR 上跑隐藏题 ──▶ 有题没通过 → 提示"疑似只迎合了公开考卷"

隐藏题和强度一样**只作提示，不改变结论**：LLM 出的期望可能和维护者最终的决定不一样
（black #4280 那种），不能拿它驳回 PR。核验报告和收据里只有题数和哈希，不含题目内容——
PR 作者看不到题，才谈得上"隐藏"。
"""

from __future__ import annotations

import ast
import re
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel

from failgate import __version__
from failgate.llm import LLMClient
from failgate.repro.sandbox import ExecResult
from failgate.repro.signature import failure_signature
from failgate.skills.base import load_prompt, priced, untrusted

from .receipt import code_sha256, receipt_digest
from .related import outcomes

PROMPT_VERSION = "1"
SCHEMA = "failgate.hidden/v1"
MAX_TESTS = 5
_SECTION = re.compile(r"^_{2,} (\S+) _{2,}$", re.M)
_END = re.compile(r"^={3,}", re.M)


def hidden_path(test_path: str) -> str:
    """tests/test_failgate_issue_2.py → tests/test_failgate_issue_2_hidden.py"""
    stem, dot, ext = test_path.rpartition(".")
    return f"{stem}_hidden.{ext}" if dot else f"{test_path}_hidden"


def is_hidden_path(path: str) -> bool:
    return path.endswith("_hidden.py")


# ---------------------------------------------------------------- 纯函数：解析、挑题、删题


def list_tests(code: str) -> list[str]:
    """模块顶层的 test_ 函数名（按出现顺序）；语法错误时返回空列表。"""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    return [n.name for n in tree.body
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name.startswith("test_")]


def failure_types(output: str) -> dict[str, str]:
    """pytest 输出的 FAILURES 部分 → {测试函数名: 异常类型的最后一段}。

    不用 -rA 摘要里的错误消息：-q 模式下节点 ID 一长，消息就被截掉了（演示仓库上实测）。
    每个失败测试有一段 "____ 名字 ____" 开头的堆栈，交给复现判定用的 failure_signature 解析。"""
    out: dict[str, str] = {}
    parts = _SECTION.split(output)
    # split 之后是 [前文, 名字1, 内容1, 名字2, 内容2, ...]
    for name, body in zip(parts[1::2], parts[2::2], strict=False):
        body = _END.split(body, maxsplit=1)[0]
        sig = failure_signature(body, None)
        out[name.rsplit(".", 1)[-1]] = (sig.exc_type or "") if sig else ""
    return out


Drop = Literal["passed_on_buggy", "other_failure", "not_run"]


def select_tests(run: ExecResult, path: str, names: list[str], expected: str | None
                 ) -> tuple[list[str], dict[str, str]]:
    """在有 bug 的代码上跑完之后挑题。expected：封存签名的异常类型（None 表示不限）。

    返回 (留下的题, {丢掉的题: 原因})。"""
    text = run.stdout + "\n" + run.stderr
    status = outcomes(text)
    types = failure_types(text)
    want = expected.rsplit(".", 1)[-1] if expected else None
    kept: list[str] = []
    dropped: dict[str, str] = {}
    for name in names:
        node = f"{path}::{name}"
        s = status.get(node)
        if s == "FAILED" and (want is None or types.get(name) == want):
            kept.append(name)
        elif s == "FAILED":
            dropped[name] = f"other_failure:{types.get(name) or '?'}"
        elif s == "PASSED":
            dropped[name] = "passed_on_buggy"
        else:
            dropped[name] = "not_run"
    return kept, dropped


def prune(code: str, keep: list[str]) -> str:
    """删掉不在 keep 里的顶层 test_ 函数（连同装饰器），其余代码原样保留。"""
    tree = ast.parse(code)
    lines = code.splitlines(keepends=True)
    cut: list[tuple[int, int]] = []
    for n in tree.body:
        if (isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name.startswith("test_")
                and n.name not in keep):
            start = min([n.lineno, *(d.lineno for d in n.decorator_list)])
            cut.append((start, n.end_lineno or n.lineno))
    for start, end in sorted(cut, reverse=True):
        del lines[start - 1:end]
    return re.sub(r"\n{3,}(?=\S)", "\n\n\n", "".join(lines))


# ---------------------------------------------------------------- 出题


class HiddenDraft(BaseModel):
    rule: str = ""
    code: str


class HiddenWriter:
    """出题人：一次 LLM 调用，输入只有 issue 和公开考卷（不给任何修复）。"""

    def __init__(self, llm: LLMClient, model: str) -> None:
        self.llm = llm
        self.model = model
        self.cost_usd = 0.0

    async def write(self, *, title: str, body: str, exam_path: str, exam_code: str,
                    package: str) -> HiddenDraft:
        user = "\n\n".join([
            untrusted("issue", "issue", f"{title}\n\n{body[:8000]}"),
            f"被测包：{package}；公开考卷的路径：{exam_path}",
            untrusted("public-exam", "exam", exam_code[:8000]),
            "按 system 中的格式输出 JSON。",
        ])
        draft, usage, resp = await self.llm.complete_json(
            [{"role": "system", "content": load_prompt(f"hidden_exam_v{PROMPT_VERSION}")},
             {"role": "user", "content": user}],
            HiddenDraft, model=self.model,
        )
        self.cost_usd += priced(resp.model, usage)
        return draft


# ---------------------------------------------------------------- 封存


class HiddenExam(BaseModel):
    """封存的隐藏考卷（hidden_exams 表的一行）。"""

    hidden_id: str
    evidence_id: str
    test_path: str
    code: str
    test_sha256: str
    tests: list[str]
    receipt: dict[str, Any] = {}


class HiddenSeal(BaseModel):
    """一次出题 + 验证的结果。hidden 为 None 时 reason 说明为什么没有封存。"""

    hidden: HiddenExam | None = None
    reason: str = "ok"  # ok / no_tests / none_kept / setup / confirm_failed
    rule: str = ""
    generated: list[str] = []
    dropped: dict[str, str] = {}
    cost_usd: float = 0.0


def build_receipt(*, hidden_id: str, exam: Any, repo: str, test_path: str, code: str,
                  tests: list[str], dropped: dict[str, str], source_repo: str | None,
                  source_sha: str | None, model: str, now: datetime | None = None
                  ) -> dict[str, Any]:
    created = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    data: dict[str, Any] = {
        "schema": SCHEMA, "hidden_id": hidden_id, "evidence_id": exam.evidence_id,
        "exam_receipt_sha256": exam.receipt_sha256, "repo": repo, "issue": exam.issue,
        "test_path": test_path, "test_sha256": code_sha256(code), "tests": len(tests),
        "dropped": len(dropped), "validated_on": {
            "source_repo": source_repo, "source_sha": source_sha, "python": exam.python,
            "pytest": exam.pytest,
        },
        "model": model, "prompt_version": PROMPT_VERSION, "failgate_version": __version__,
        "created_at": created.isoformat().replace("+00:00", "Z"),
    }
    data["receipt_sha256"] = receipt_digest(data)
    return data


def as_exam(exam: Any, path: str, code: str) -> Any:
    """把隐藏题包装成一份"考卷"，好复用 Workbench.run_exam（写进工作区、跑 pytest -rA）。"""
    return exam.model_copy(update={"test_path": path, "code": code,
                                   "test_sha256": code_sha256(code)})


async def seal_hidden(bench: Any, writer: HiddenWriter, exam: Any, *, repo: str, title: str,
                      body: str, source_repo: str | None, source_sha: str) -> HiddenSeal:
    """出题 → 在 issue 时的代码（source_sha）上挑题 → 删题后再跑一次确认 → 生成收据。

    bench：verify.engine.Workbench（prepare / run_exam）。"""
    draft = await writer.write(title=title, body=body, exam_path=exam.test_path,
                               exam_code=exam.code, package=exam.package)
    names = list_tests(draft.code)[:MAX_TESTS]
    out = HiddenSeal(rule=draft.rule, generated=names, cost_usd=writer.cost_usd)
    if not names:
        out.reason = "no_tests"
        return out
    path = hidden_path(exam.test_path)
    code = prune(draft.code, names)
    expected = exam.signature.exc_type if exam.signature else None
    try:
        env = await bench.prepare(source_repo or repo, source_sha, exam)
    except Exception as e:  # noqa: BLE001 — 环境问题只是出不了隐藏题，不影响别的
        out.reason = "setup"
        out.dropped = {"*": f"{type(e).__name__}: {str(e)[:200]}"}
        return out
    first = await bench.run_exam(env, as_exam(exam, path, code))
    kept, dropped = select_tests(first, path, names, expected)
    out.dropped = dropped
    if not kept:
        out.reason = "none_kept"
        return out
    code = prune(code, kept)
    # 删题之后再跑一次：留下的题必须仍然全部按预期失败（排除偶发和题之间的相互影响）
    again = await bench.run_exam(env, as_exam(exam, path, code))
    still, lost = select_tests(again, path, kept, expected)
    if lost:
        out.dropped |= {k: f"confirm:{v}" for k, v in lost.items()}
        if not still:
            out.reason = "confirm_failed"
            return out
        code = prune(code, still)
    hidden_id = uuid.uuid4().hex
    receipt = build_receipt(hidden_id=hidden_id, exam=exam, repo=repo, test_path=path,
                            code=code, tests=still, dropped=out.dropped,
                            source_repo=source_repo, source_sha=source_sha, model=writer.model)
    out.hidden = HiddenExam(hidden_id=hidden_id, evidence_id=exam.evidence_id, test_path=path,
                            code=code, test_sha256=receipt["test_sha256"], tests=still,
                            receipt=receipt)
    out.cost_usd = writer.cost_usd
    return out


# ---------------------------------------------------------------- 核验时跑


class HiddenResult(BaseModel):
    """核验时隐藏考卷的结果。只有题数和哈希，没有题目内容（会进 PR 评论和收据）。"""

    status: Literal["ok", "n/a"]
    reason: str = "ok"  # ok / infra / invalid
    hidden_id: str | None = None
    test_sha256: str | None = None
    total: int = 0
    passed: int = 0
    failed: int = 0

    @property
    def suspicious(self) -> bool:
        return self.status == "ok" and self.failed > 0


async def run_hidden(bench: Any, head_env: Any, exam: Any, hidden: HiddenExam) -> HiddenResult:
    """在 PR 的代码上跑隐藏考卷；有题失败时整份再跑一次，两次都失败的才算（排除偶发）。"""
    base = {"hidden_id": hidden.hidden_id, "test_sha256": hidden.test_sha256,
            "total": len(hidden.tests)}
    probe = as_exam(exam, hidden.test_path, hidden.code)
    failed: set[str] | None = None
    for _ in range(2):
        run = await bench.run_exam(head_env, probe)
        if run.infra_failure:
            return HiddenResult(status="n/a", reason="infra", **base)
        status = outcomes(run.stdout + "\n" + run.stderr)
        mine = {n.rsplit("::", 1)[-1]: s for n, s in status.items()
                if n.startswith(f"{hidden.test_path}::")}
        if not mine:
            return HiddenResult(status="n/a", reason="invalid", **base)
        now = {n for n in hidden.tests if mine.get(n) != "PASSED"}
        failed = now if failed is None else failed & now
        if not failed:
            break
    n_failed = len(failed or ())
    return HiddenResult(status="ok", passed=len(hidden.tests) - n_failed, failed=n_failed,
                        **base)
