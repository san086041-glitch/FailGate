import pytest

from failgate.repro.judge import VerdictKind, assess, judge
from failgate.repro.sandbox import ExecResult

REPORTED = """\
Traceback (most recent call last):
  File "/home/u/.venv/lib/python3.12/site-packages/mylib/core.py", line 10, in load
  File "/home/u/.venv/lib/python3.12/site-packages/mylib/parse.py", line 42, in parse
KeyError: 'name'
"""
SAME = REPORTED.replace("/home/u/.venv/lib/python3.12/site-packages", "/workspace/src")
DIFFERENT = """\
Traceback (most recent call last):
  File "/workspace/src/mylib/io.py", line 7, in open_file
FileNotFoundError: [Errno 2] No such file or directory: 'data.csv'
"""


def run(exit_code: int = 1, stderr: str = SAME, **kw: object) -> ExecResult:
    return ExecResult(phase="run", argv=["python", "repro.py"], exit_code=exit_code,
                      stderr=stderr, **kw)  # type: ignore[arg-type]


PASS = run(0, "")


def j(runs: list[ExecResult], **kw: object):
    kw.setdefault("reported_traceback", REPORTED)
    kw.setdefault("package", "mylib")
    return judge(runs, **kw)  # type: ignore[arg-type]


def test_passing_script_is_not_reproduced():
    assert j([PASS]).kind == VerdictKind.NOT_REPRODUCED


@pytest.mark.parametrize(
    ("kw", "word"), [({"timed_out": True, "exit_code": 124}, "超时"),
                     ({"oom_killed": True, "exit_code": 137}, "内存")]
)
def test_timeout_and_oom_are_not_evidence(kw, word):
    v = j([run(**kw)])
    assert v.kind == VerdictKind.INCONCLUSIVE and word in v.reason


def test_manufactured_failure_is_rejected():
    # Agent 写的脚本失败了，但失败的不是报告里那个 bug（比如文件路径写错）
    v = j([run(stderr=DIFFERENT)])
    assert v.kind == VerdictKind.UNRELATED_FAILURE
    assert v.match is not None and v.match < 0.6 and v.match_method == "signature"


def test_failure_raised_by_the_script_itself_is_rejected():
    # 类型和消息都抄对了（签名分数会有 0.5 + 0.2 = 0.7），但一帧都没经过 mylib
    forged = (
        'Traceback (most recent call last):\n  File "/workspace/.failgate/repro.py", line 3, in '
        "<module>\n    raise KeyError('name')\nKeyError: 'name'\n"
    )
    v = j([run(stderr=forged)])
    assert v.kind == VerdictKind.UNRELATED_FAILURE and "没有经过目标包" in v.reason


def test_consistent_failure_is_reproduced():
    v = j([run(), run(), run(), run()])
    assert v.kind == VerdictKind.REPRODUCED and v.fail_rate == 1.0 and v.runs == 4
    assert v.match == 1.0 and v.observed and v.observed.frames == [
        "mylib/core.py:load", "mylib/parse.py:parse"
    ]


def test_sparse_signature_still_matches_itself():
    # 只有栈帧、没有可解析的异常行：自己和自己打分只有 0.3，但必须算"同一个失败"
    sparse = 'Traceback (most recent call last):\n  File "/w/src/mylib/core.py", line 1, in f\n'
    v = j([run(stderr=sparse)] * 4, reported_traceback=None, llm_match=0.9)
    assert v.kind == VerdictKind.REPRODUCED and v.fail_rate == 1.0


def test_rerun_with_a_different_failure_counts_as_not_failing():
    v = j([run(), run(), run(stderr=DIFFERENT), run()])
    assert v.kind == VerdictKind.FLAKY and v.fail_rate == 0.75


def test_no_traceback_needs_llm_then_uses_its_score():
    assert j([run()], reported_traceback=None).kind == VerdictKind.INCONCLUSIVE
    v = j([run()], reported_traceback=None, llm_match=0.9)
    assert v.kind == VerdictKind.REPRODUCED and v.match_method == "llm"
    assert j([run()], reported_traceback=None, llm_match=0.3).kind == (
        VerdictKind.UNRELATED_FAILURE
    )


class Rerunner:
    def __init__(self, results: list[ExecResult]) -> None:
        self.results, self.calls = list(results), 0

    async def __call__(self) -> ExecResult:
        self.calls += 1
        return self.results.pop(0) if self.results else run()


async def test_assess_does_not_rerun_unrelated_failures():
    r = Rerunner([])
    v = await assess(run(stderr=DIFFERENT), r, reported_traceback=REPORTED, package="mylib")
    assert v.kind == VerdictKind.UNRELATED_FAILURE and r.calls == 0


async def test_assess_stops_after_three_consistent_reruns():
    r = Rerunner([])
    v = await assess(run(), r, reported_traceback=REPORTED, package="mylib")
    assert v.kind == VerdictKind.REPRODUCED and r.calls == 3 and v.runs == 4


async def test_assess_extends_flaky_to_thirty_runs():
    # 第 2 次重跑通过了 → 追加到 30 次；之后每隔一次通过一次
    pattern = [run(), PASS, run()] + [PASS if i % 2 else run() for i in range(26)]
    r = Rerunner(pattern)
    v = await assess(run(), r, reported_traceback=REPORTED, package="mylib")
    assert v.kind == VerdictKind.FLAKY and v.runs == 30 and r.calls == 29
    assert v.fail_rate == round(sum(1 for x in [run(), *pattern] if x.failed) / 30, 4)
