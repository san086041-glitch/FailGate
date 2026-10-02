"""命令行实时进度、结果面板、up 状态板（ADR 0038）。"""

from __future__ import annotations

import asyncio
import io
from datetime import UTC, datetime, timedelta

from opentelemetry import trace
from opentelemetry.trace import StatusCode
from rich.console import Console

from failgate import progress, views
from failgate import up as u
from failgate.db import Case, Database, Delivery, Repo, Run, TransitionLog, VerificationRecord
from failgate.fix.agent import FixResult
from failgate.verify.engine import (
    ClaimResult,
    ClaimVerdict,
    ExamRun,
    Layer1,
    Layer2,
    Layer3,
    Verification,
)
from failgate.verify.hidden import HiddenResult
from failgate.verify.strength import StrengthReport
from failgate.verify.tamper import Signal

tracer = trace.get_tracer("test")


def text_of(renderable, width: int = 120) -> str:
    con = Console(record=True, width=width, file=io.StringIO())
    con.print(renderable)
    return con.export_text()


# ---------------------------------------------------------------- 实时进度


def test_tracker_follows_spans():
    tracker = progress.Tracker("核验 acme/app#7")
    hub = progress.relay()
    hub.listeners.append(tracker)
    try:
        with tracer.start_as_current_span("verify prepare base"):
            with tracer.start_as_current_span("sandbox run",
                                              attributes={"failgate.sandbox.argv": "pytest -q"}):
                pass
        with tracer.start_as_current_span("verify layer1"):
            with tracer.start_as_current_span("chat deepseek-flash") as span:
                span.set_attributes({"gen_ai.usage.input_tokens": 1200,
                                     "gen_ai.usage.output_tokens": 300,
                                     "failgate.cost_usd": 0.0021})
            snapshot = text_of(tracker)  # 第一层还在跑
        with tracer.start_as_current_span("verify layer3") as span:
            span.set_status(StatusCode.ERROR)
        with tracer.start_as_current_span("fix edit", attributes={"failgate.fix.round": 2}):
            with tracer.start_as_current_span("fix tool read_file",
                                              attributes={"failgate.fix.target": "src/x.py"}):
                pass
    finally:
        hub.listeners.remove(tracker)
    assert "① 考卷" in snapshot and "准备修复前的环境" in snapshot
    done = text_of(tracker)
    assert "✓ 准备修复前的环境" in done and "✗ ③ 相关测试" in done
    assert "修改（第 2 轮）" in done
    assert "LLM 1 次" in done and "$0.0021" in done and "容器 1 次" in done
    assert "第 1 步 · 最近：read_file src/x.py" in done


def test_listeners_are_removed_after_each_command():
    con = Console(file=io.StringIO(), force_terminal=False)
    with progress.live_progress("a", con):
        assert len(progress.relay().listeners) == 1
    with progress.live_progress("b", con):
        assert len(progress.relay().listeners) == 1  # 交互模式里连着跑也不会叠加
    assert progress.relay().listeners == []


def test_plain_mode_prints_finished_stages():
    buf = io.StringIO()
    con = Console(file=buf, force_terminal=False, width=100)
    with progress.live_progress("核验 acme/app#7", con):
        with tracer.start_as_current_span("verify strength"):
            pass
    out = buf.getvalue()
    assert out.startswith("核验 acme/app#7") and "✓ 考卷强度（变异测试）" in out


# ---------------------------------------------------------------- 结果面板


def verification(verdict: ClaimVerdict, **claim) -> Verification:
    c = ClaimResult(issue=19, verdict=verdict, reasons=claim.pop("reasons", []),
                    test_path="tests/test_failgate_issue_19.py", test_sha256="ab" * 32, **claim)
    return Verification(repo="acme/app", pr=20, base_sha="1234567aaa", head_sha="89abcdefbbb",
                        head_repo="acme/app", claims=[c], verdict=verdict,
                        created_at="2026-10-02T00:00:00Z")


def test_verification_panel_verified():
    v = verification(
        ClaimVerdict.VERIFIED,
        layer1=Layer1(status="pass", reason="pass",
                      base=[ExamRun(exit_code=1, outcome="failed_same")] * 2,
                      head=[ExamRun(exit_code=0, outcome="passed")] * 2),
        layer2=Layer2(), layer3=Layer3(status="pass", reason="pass", files=["tests/a.py"]),
        strength=StrengthReport(status="ok", reason="ok", killed=21, survived=9, kill_rate=0.7,
                                grade="medium"),
        hidden=HiddenResult(status="ok", total=5, passed=5),
    )
    out = text_of(views.verification_panel(v))
    for part in ("VERIFIED", "acme/app#20", "声称修复 #19", "① 考卷", "② 篡改", "③ 回归",
                 "中 21/30（杀死率 70%）", "5 道全部通过", "1234567 → head 89abcde"):
        assert part in out, part
    assert "`" not in out and "**" not in out  # Markdown 记号去掉了


def test_verification_panel_refuted_and_suspicious():
    v = verification(
        ClaimVerdict.REFUTED, reasons=["tamper:exam_modified", "layer3:new_failures"],
        layer1=Layer1(status="pass", reason="pass"),
        layer2=Layer2(signals=[Signal(level="high", kind="exam_modified",
                                      path="tests/test_failgate_issue_19.py")]),
        layer3=Layer3(status="fail", reason="new_failures", new_failures=["tests/a.py::t1"]),
        hidden=HiddenResult(status="ok", total=5, passed=3, failed=2),
    )
    out = text_of(views.verification_panel(v))
    assert "驳回" in out and "tests/a.py::t1" in out and "2/5 道没有通过" in out
    assert "高危" in out


def test_no_claim_panel():
    v = Verification(repo="acme/app", pr=3, base_sha="a" * 7, head_sha="b" * 7,
                     head_repo="acme/app", claims=[], verdict=None, created_at="x")
    assert "没有声明修复任何 issue" in text_of(views.verification_panel(v))


def test_strength_bar():
    assert views.bar(0.7) == "███████░░░"
    assert views.bar(0) == "░" * 10 and views.bar(1.5) == "█" * 10


def test_fix_panel():
    res = FixResult(status="done", passed=True, patch="--- a\n+++ b\n", files=["src/x.py"],
                    steps=22, cost_usd=0.0134, duration_s=70, tool_counts={"read_file": 9,
                                                                            "edit_file": 2})
    out = text_of(views.fix_panel(19, res, control=False))
    assert "done" in out and "实验组" in out and "22 步" in out and "$0.0134" in out
    assert "read_file 9 · edit_file 2" in out and "src/x.py" in out


def test_state_colors():
    assert views.state_text("VERIFYING").style == "bold blue"
    assert views.state_text("REFUTED").style == "red"


# ---------------------------------------------------------------- up 状态板


def test_board_snapshot_counts_only_this_run(tmp_path):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'b.db').as_posix()}"
    since = datetime.now(UTC)
    old = since - timedelta(hours=1)

    async def go() -> u.Snapshot:
        db = Database(url)
        await db.create_all()
        async with db.session() as s, s.begin():
            repo = Repo(platform="github", full_name="acme/app", mode="live")
            s.add(repo)
            await s.flush()
            done = Case(repo_id=repo.id, kind="issue", number=1, title="a", state="REPRODUCED")
            busy = Case(repo_id=repo.id, kind="pull", number=2, title="b", state="VERIFYING")
            s.add_all([done, busy])
            await s.flush()
            s.add_all([
                Delivery(delivery_id="old", platform="github", event="issues", received_at=old),
                Delivery(delivery_id="new", platform="github", event="issues"),
                TransitionLog(case_id=done.id, from_state="NEW", to_state="INTAKE",
                              event="x", at=old),
                TransitionLog(case_id=done.id, from_state="REPRODUCING", to_state="REPRODUCED",
                              event="x"),
                Run(case_id=done.id, skill="repro", skill_version="1", status="ok", usd=0.004),
                Run(case_id=done.id, skill="repro", skill_version="1", status="ok", usd=9.0,
                    started_at=old, ended_at=old),
                VerificationRecord(case_id=busy.id, pr_number=2, base_sha="a", head_sha="b",
                                   verdict="VERIFIED", receipt={}, receipt_sha256="0" * 64),
            ])
        snap = await u.snapshot(db, since)
        await db.dispose()
        return snap

    snap = asyncio.run(go())
    assert (snap.events, snap.cases) == (1, 1)  # 启动前的不算
    assert abs(snap.cost - 0.004) < 1e-9 and snap.verdicts == {"VERIFIED": 1}
    assert [r[5] for r in snap.recent] == ["REPRODUCED"]
    assert [(w[1], w[3]) for w in snap.working] == [(2, "VERIFYING")]

    board = u.Board([u.Proc("serve", ["x"])], "http://127.0.0.1:8080", since)
    board.snap = snap
    out = text_of(board)
    for part in ("FailGate 在线", "事件 1", "Case 1", "✓1", "$0.0040", "进行中", "app PR #2",
                 "VERIFYING", "REPRODUCING → REPRODUCED", "工作台 http://127.0.0.1:8080/console"):
        assert part in out, part
    assert "○ serve" in out  # 没有真的起进程：显示没在跑


def test_board_survives_db_errors():
    class Broken:
        def session(self):
            raise RuntimeError("down")

    board = u.Board([], "http://h", datetime.now(UTC))
    asyncio.run(board.refresh(Broken()))
    assert "数据库读不到（RuntimeError）" in text_of(board)


def test_log_filter():
    assert not u.keep_line("serve", '127.0.0.1 - "GET /healthz HTTP/1.1" 200', True)
    assert u.keep_line("serve", "INFO: request", True)
    assert not u.keep_line("serve", "INFO: request", False)
    assert u.keep_line("serve", "2026 WARNING failgate.app: x", False)
    assert u.keep_line("smee", "Error: connect ECONNREFUSED", False)
