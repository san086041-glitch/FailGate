"""ClaimVerify：用封存的考卷核验声称"修复了 #N"的 PR（技术方案第 9 节，ADR 0017）。

三层，每层单独给结论，最后合成一个：
① 修复前后：在合并基点（base）和 PR head 上各建一次源码环境（Python、pytest、伪版本号都用
   收据里的），注入**封存的**考卷各跑 2 次。base 上失败且是封存时那个失败、head 上通过，
   才算通过；用 pytest 的 -rA 摘要确认考卷真的执行并通过了，而不是被跳过或标成 xfail；
② 防篡改：看 PR 的改动文件和 head 上考卷的内容（tamper.py）；
③ 相关回归：挑相关的已有测试在 base 和 head 上各跑一次，head 上新出现的失败重跑一次确认。

判定只用程序，不调用 LLM。拿不准就给 INCONCLUSIVE，不强行下结论。对外措辞是"通过验收测试 +
未发现篡改 + 无新增回归"，不说"修复正确"：考卷可能太弱（W5 的考卷强度评估解决这个）。

建环境、跑测试都通过 Workbench 接口完成：线上是 Docker 沙箱（workbench.py），测试里是假的，
所以判定逻辑可以用纯单元测试覆盖。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from failgate import __version__
from failgate.index.trace import TraceSignature
from failgate.repro.judge import same_failure
from failgate.repro.l2 import PYTEST_INVALID
from failgate.repro.sandbox import ExecResult

from .receipt import receipt_digest
from .related import failed_nodes, outcomes, select_related_tests
from .tamper import PullFile, Signal, is_test_file, tamper_signals

SCHEMA = "failgate.verify/v1"
EXAM_RUNS = 2
RELATED_TIMEOUT_S = 300


class ClaimVerdict(StrEnum):
    VERIFIED = "VERIFIED"  # ①通过 ∧ ②没有高危信号 ∧ ③没有新增失败
    REFUTED = "REFUTED"  # 考卷在 head 上仍失败 / 被跳过 / 高危篡改 / 新增回归
    INCONCLUSIVE = "INCONCLUSIVE"  # 没有考卷、base 上不失败、环境搭不起来、结果不一致


class Exam(BaseModel):
    """一份封存的考卷（evidence 表里的一行），以及重放它需要的条件。"""

    evidence_id: str
    issue: int
    test_path: str
    code: str
    test_sha256: str
    receipt_sha256: str
    package: str
    module: str  # 判定器按它过滤栈帧
    python: str | None = None
    pytest: str | None = None
    version: str | None = None  # 伪版本号：base 和 head 用同一个，只让代码这一个变量变化
    signature: TraceSignature | None = None
    receipt: dict[str, Any] = {}  # 原始收据：重新封存时在它的基础上生成新收据


class PullRequest(BaseModel):
    repo: str
    number: int
    title: str
    body: str = ""
    base_sha: str  # 合并基点
    head_sha: str
    head_repo: str
    files: list[PullFile]


class SetupFailed(RuntimeError):
    """某个提交上的环境搭不起来（源码拿不到、安装失败、预检不通过）。"""


class Workbench(Protocol):
    async def prepare(self, repo: str, sha: str, exam: Exam) -> Any: ...
    def read_files(self, prepared: Any, paths: set[str] | None = None) -> dict[str, str]: ...
    async def run_exam(self, prepared: Any, exam: Exam) -> ExecResult: ...
    async def run_tests(self, prepared: Any, targets: list[str], timeout_s: int) -> ExecResult: ...


# ---------------------------------------------------------------- 结果

Outcome = Literal["failed_same", "failed_other", "passed", "skipped", "invalid", "infra"]


class ExamRun(BaseModel):
    exit_code: int
    outcome: Outcome


# 理由都用代码（语言无关，写进收据）；报告渲染时再翻译成中文或英文（report.py）


class Layer1(BaseModel):
    status: Literal["pass", "fail", "inconclusive"]
    # pass / head_failed / head_invalid / head_skipped / head_flaky / base_passed / base_other /
    # base_invalid / base_mixed / infra / setup
    reason: str
    detail: str = ""  # setup：哪一边、什么错误
    base: list[ExamRun] = []
    head: list[ExamRun] = []


class Layer2(BaseModel):
    signals: list[Signal] = []

    @property
    def high(self) -> list[Signal]:
        return [s for s in self.signals if s.level == "high"]


class Layer3(BaseModel):
    status: Literal["pass", "fail", "inconclusive", "none"]
    reason: str  # pass / new_failures / none / base_infra / head_infra / setup
    files: list[str] = []
    new_failures: list[str] = []


class ClaimResult(BaseModel):
    issue: int
    verdict: ClaimVerdict
    reasons: list[str]  # no_exam / tamper:<种类> / layer1:<代码> / layer3:<代码>
    evidence_id: str | None = None
    exam_receipt_sha256: str | None = None
    test_path: str | None = None
    test_sha256: str | None = None
    layer1: Layer1 | None = None
    layer2: Layer2 | None = None
    layer3: Layer3 | None = None


class Verification(BaseModel):
    repo: str
    pr: int
    base_sha: str
    head_sha: str
    head_repo: str
    claims: list[ClaimResult]
    verdict: ClaimVerdict | None  # None：PR 没有声明修复任何 issue
    created_at: str

    def receipt(self) -> dict[str, Any]:
        """核验收据：和证据收据同一套哈希规则（ADR 0016），schema 不同。"""
        data = {"schema": SCHEMA, **self.model_dump(mode="json"),
                "failgate_version": __version__}
        data["receipt_sha256"] = receipt_digest(data)
        return data


# ---------------------------------------------------------------- 纯函数：单次运行 → 结果类别


def classify_base(run: ExecResult, exam: Exam) -> ExamRun:
    if run.infra_failure:
        return ExamRun(exit_code=run.exit_code, outcome="infra")
    if run.exit_code in PYTEST_INVALID:
        return ExamRun(exit_code=run.exit_code, outcome="invalid")
    if not run.failed:
        return ExamRun(exit_code=run.exit_code, outcome="passed")
    same = same_failure(run, exam.signature, exam.module)
    return ExamRun(exit_code=run.exit_code, outcome="failed_same" if same else "failed_other")


def classify_head(run: ExecResult, exam: Exam) -> ExamRun:
    """head 上"通过"必须是：退出码 0、考卷里至少一个测试 PASSED、没有被跳过或标成 xfail。"""
    if run.infra_failure:
        return ExamRun(exit_code=run.exit_code, outcome="infra")
    mine = {n: s for n, s in outcomes(run.stdout + "\n" + run.stderr).items()
            if n == exam.test_path or n.startswith(f"{exam.test_path}::")}
    if any(s in ("SKIPPED", "XFAIL", "XPASS") for s in mine.values()):
        return ExamRun(exit_code=run.exit_code, outcome="skipped")
    if run.exit_code in PYTEST_INVALID:
        # 5 = 没收集到测试：考卷整个被跳过或被取消收集
        return ExamRun(exit_code=run.exit_code,
                       outcome="skipped" if run.exit_code == 5 else "invalid")
    if run.exit_code == 0:
        passed = any(s == "PASSED" for s in mine.values())
        return ExamRun(exit_code=0, outcome="passed" if passed else "skipped")
    same = same_failure(run, exam.signature, exam.module)
    return ExamRun(exit_code=run.exit_code, outcome="failed_same" if same else "failed_other")


def judge_layer1(base: list[ExamRun], head: list[ExamRun]) -> Layer1:
    """两组运行 → 第一层结论。先看有没有被跳过（篡改），再看 base，最后看 head。"""
    kw: dict[str, Any] = {"base": base, "head": head}
    if any(r.outcome == "infra" for r in [*base, *head]):
        return Layer1(status="inconclusive", reason="infra", **kw)
    if any(r.outcome == "skipped" for r in head):
        return Layer1(status="fail", reason="head_skipped", **kw)
    b = {r.outcome for r in base}
    if b != {"failed_same"}:
        reason = {
            frozenset({"passed"}): "base_passed",
            frozenset({"failed_other"}): "base_other",
            frozenset({"invalid"}): "base_invalid",
        }.get(frozenset(b), "base_mixed")
        return Layer1(status="inconclusive", reason=reason, **kw)
    h = {r.outcome for r in head}
    if h == {"passed"}:
        return Layer1(status="pass", reason="pass", **kw)
    if "passed" in h:
        return Layer1(status="inconclusive", reason="head_flaky", **kw)
    if h == {"invalid"}:
        return Layer1(status="fail", reason="head_invalid", **kw)
    return Layer1(status="fail", reason="head_failed", **kw)


def combine(layer1: Layer1, layer2: Layer2, layer3: Layer3) -> tuple[ClaimVerdict, list[str]]:
    refuted: list[str] = []
    unsure: list[str] = []
    if layer2.high:
        refuted += [f"tamper:{s.kind}" for s in layer2.high]
    if layer1.status == "fail":
        refuted.append(f"layer1:{layer1.reason}")
    elif layer1.status == "inconclusive":
        unsure.append(f"layer1:{layer1.reason}")
    if layer3.status == "fail":
        refuted.append(f"layer3:{layer3.reason}")
    elif layer3.status == "inconclusive":
        unsure.append(f"layer3:{layer3.reason}")
    if refuted:
        return ClaimVerdict.REFUTED, refuted + unsure
    if unsure:
        return ClaimVerdict.INCONCLUSIVE, unsure
    return ClaimVerdict.VERIFIED, []


def overall(claims: list[ClaimResult]) -> ClaimVerdict | None:
    verdicts = {c.verdict for c in claims}
    for v in (ClaimVerdict.REFUTED, ClaimVerdict.INCONCLUSIVE, ClaimVerdict.VERIFIED):
        if v in verdicts:
            return v
    return None


# ---------------------------------------------------------------- 编排


class ClaimVerifier:
    def __init__(self, bench: Workbench, *, exam_runs: int = EXAM_RUNS) -> None:
        self.bench = bench
        self.exam_runs = exam_runs

    async def verify(
        self, pr: PullRequest, claims: list[int], exams: dict[int, Exam | None],
        *, now: datetime | None = None,
    ) -> Verification:
        results = [await self.verify_claim(pr, n, exams.get(n)) for n in claims]
        created = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
        return Verification(
            repo=pr.repo, pr=pr.number, base_sha=pr.base_sha, head_sha=pr.head_sha,
            head_repo=pr.head_repo, claims=results, verdict=overall(results),
            created_at=created.isoformat().replace("+00:00", "Z"),
        )

    async def verify_claim(self, pr: PullRequest, issue: int, exam: Exam | None) -> ClaimResult:
        if exam is None:
            return ClaimResult(issue=issue, verdict=ClaimVerdict.INCONCLUSIVE, reasons=["no_exam"])
        base_env = head_env = None
        setup: list[str] = []
        try:
            base_env = await self.bench.prepare(pr.repo, pr.base_sha, exam)
        except SetupFailed as e:
            setup.append(f"base: {e}")
        try:
            # fork 的提交也从 base 仓库取：GitHub 为每个 PR 保留 refs/pull/N/head
            head_env = await self.bench.prepare(pr.repo, pr.head_sha, exam)
        except SetupFailed as e:
            setup.append(f"head: {e}")

        head_code = None
        if head_env is not None:
            head_code = self.bench.read_files(head_env, {exam.test_path}).get(exam.test_path)
        layer2 = Layer2(signals=tamper_signals(
            pr.files, test_path=exam.test_path, sealed_sha256=exam.test_sha256,
            head_code=head_code,
        ))
        if base_env is None or head_env is None:
            layer1 = Layer1(status="inconclusive", reason="setup", detail="; ".join(setup))
            layer3 = Layer3(status="inconclusive", reason="setup")
        else:
            layer1 = await self._layer1(base_env, head_env, exam)
            layer3 = await self._layer3(pr, base_env, head_env, exam)
        verdict, reasons = combine(layer1, layer2, layer3)
        return ClaimResult(
            issue=issue, verdict=verdict, reasons=reasons, evidence_id=exam.evidence_id,
            exam_receipt_sha256=exam.receipt_sha256, test_path=exam.test_path,
            test_sha256=exam.test_sha256, layer1=layer1, layer2=layer2, layer3=layer3,
        )

    async def _layer1(self, base_env: Any, head_env: Any, exam: Exam) -> Layer1:
        base = [classify_base(await self.bench.run_exam(base_env, exam), exam)
                for _ in range(self.exam_runs)]
        head = [classify_head(await self.bench.run_exam(head_env, exam), exam)
                for _ in range(self.exam_runs)]
        return judge_layer1(base, head)

    async def _layer3(self, pr: PullRequest, base_env: Any, head_env: Any, exam: Exam) -> Layer3:
        tests = self.bench.read_files(head_env)
        tests = {p: s for p, s in tests.items() if is_test_file(p)}
        files = select_related_tests(pr.files, tests, exclude=exam.test_path)
        if not files:
            return Layer3(status="none", reason="none")
        on_base = set(self.bench.read_files(base_env, set(files)))
        head_run = await self.bench.run_tests(head_env, files, RELATED_TIMEOUT_S)
        base_targets = [f for f in files if f in on_base]
        base_failed: set[str] = set()
        if base_targets:
            base_run = await self.bench.run_tests(base_env, base_targets, RELATED_TIMEOUT_S)
            if base_run.infra_failure:
                return Layer3(status="inconclusive", files=files, reason="base_infra")
            base_failed = failed_nodes(base_run.stdout + "\n" + base_run.stderr)
        if head_run.infra_failure:
            return Layer3(status="inconclusive", files=files, reason="head_infra")
        new = failed_nodes(head_run.stdout + "\n" + head_run.stderr) - base_failed
        if new:
            # 重跑一次，排除偶发失败
            again = await self.bench.run_tests(head_env, sorted(new), RELATED_TIMEOUT_S)
            new &= failed_nodes(again.stdout + "\n" + again.stderr)
        if new:
            return Layer3(status="fail", files=files, new_failures=sorted(new),
                          reason="new_failures")
        return Layer3(status="pass", files=files, reason="pass")
