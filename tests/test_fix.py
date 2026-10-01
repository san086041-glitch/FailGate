"""修复 Agent（ADR 0027）：写权限、宿主机改动清单、LangGraph 控制流；最后一个真实 Docker 场景。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from failgate.fix.agent import (
    PIN_MAX_LINES,
    FixAgent,
    FixTask,
    clip,
    merge_ranges,
    overlaps,
    pin_ranges,
)
from failgate.fix.guard import GuardError, WriteGuard
from failgate.fix.workspace import FixWorkspace, count_changed
from failgate.llm import LLMClient
from failgate.replay import fixtures as fx_mod
from failgate.repro.config import PackageConfig
from failgate.repro.envcache import EnvCache
from failgate.repro.l2 import TestReproducer
from failgate.repro.package import IssueContext
from failgate.repro.sandbox import DockerSandbox, ExecResult

EXAM_PATH = "tests/test_failgate_issue_101.py"
PARSER = "confkit/parser.py"


# ---------------------------------------------------------------- 写权限（纯函数）


@pytest.mark.parametrize("path", [
    "tests/test_parser.py", "src/tests/x.py", "Tests/test_a.py", "pkg/test_x.py",
    "pkg/x_test.py", "conftest.py", "pkg/conftest.py", "pytest.ini", "tox.ini", "setup.cfg",
    "pyproject.toml", "PyProject.toml", ".github/workflows/ci.yml", ".failgate/x.py",
    "pkg/.hidden.py", "sitecustomize.py", "evil.pth", "pkg/data.bin", "pkg/x.sh",
    "../outside.py", "pkg/../../outside.py", "/abs/path.py", "C:/x.py", "pkg\\..\\x.py", "",
    "tests/../tests/test_failgate_issue_101.py", EXAM_PATH,
])
def test_guard_denies(path: str):
    with pytest.raises(GuardError):
        WriteGuard([EXAM_PATH]).check(path)


@pytest.mark.parametrize("path", [
    "confkit/parser.py", "src/black/linegen.py", "pkg/sub/mod.pyi", "pkg/notes.md",
    "pkg/./mod.py", "src/contest_utils.py",
])
def test_guard_allows_source(path: str):
    assert WriteGuard([EXAM_PATH]).check(path)


def test_guard_protected_is_case_and_dot_insensitive():
    g = WriteGuard(["pkg/exam_file.py"])
    with pytest.raises(GuardError):
        g.check("PKG/./Exam_File.py")


def test_guard_totals():
    g = WriteGuard(max_files=2, max_lines=10)
    g.check_totals(2, 10)
    with pytest.raises(GuardError):
        g.check_totals(3, 1)
    with pytest.raises(GuardError):
        g.check_totals(1, 11)


def test_count_changed():
    assert count_changed("a\nb\n", "a\nc\n") == 2
    assert count_changed(None, "x\ny\n") == 2
    assert count_changed("a\n", "a\n") == 0


# ---------------------------------------------------------------- 假沙箱


class FakeSandbox:
    def __init__(self) -> None:
        self.files: dict[str, dict[str, str]] = {}  # 卷 → 相对路径 → 内容
        self.runs: list[tuple[str, list[str], list[str]]] = []
        self.pytest_exits: list[int] = []
        self.volumes = 0

    async def create_workspace(self, key: str) -> str:
        self.volumes += 1
        name = f"ws{self.volumes}"
        self.files[name] = {}
        return name

    async def remove_workspace(self, volume: str) -> None:
        self.files.pop(volume, None)

    async def copy_in(self, volume: str, src: Path, image: str) -> None:
        for p in await asyncio.to_thread(lambda: sorted(src.rglob("*"))):
            if await asyncio.to_thread(p.is_file):
                self.files[volume][p.relative_to(src).as_posix()] = await asyncio.to_thread(
                    p.read_text)

    async def run(self, image: str, volume: str, argv: list[str], **kw: Any) -> ExecResult:
        self.runs.append((volume, list(argv), list(kw.get("env", []))))
        if argv[:2] == ["python", "-c"]:  # CodeTools 辅助程序
            payload = {"list": {"files": ["confkit/parser.py"], "total": 1},
                       "search": {"hits": ["confkit/parser.py:35: sections[section]"],
                                  "truncated": False},
                       "read": {"path": "x", "lines": ["   35  sections[section]"],
                                "total_lines": 40}}[argv[3]]
            return ExecResult(phase="run", argv=argv, exit_code=0,
                              stdout="FAILGATE_TOOL" + json.dumps(payload))
        if argv[:3] == ["python", "-m", "pytest"]:
            code = self.pytest_exits.pop(0) if self.pytest_exits else 1
            return ExecResult(phase="run", argv=argv, exit_code=code,
                              stdout=f"pytest output exit {code}\nE   KeyError: None")
        return ExecResult(phase="run", argv=argv, exit_code=0, stdout="snippet ok")


class FakeTester:
    def __init__(self, sandbox: FakeSandbox) -> None:
        self.sandbox = sandbox

    async def open_workspace(self, prepared: Any, key: str) -> str:
        return await self.sandbox.create_workspace(key)

    async def write_test(self, ws: str, prepared: Any, content: str) -> None:
        self.sandbox.files[ws][f"src/{prepared.test_path}"] = content


EXAM = "def test_x():\n    assert False\n"


def make_workspace(sb: FakeSandbox, *, exam: str | None = EXAM) -> FixWorkspace:
    fx = fx_mod.load_all(only=["bug-keyerror"])[0]
    prepared = SimpleNamespace(
        cfg=PackageConfig(name="confkit"), tree=fx.tree, test_path=EXAM_PATH,
        env=SimpleNamespace(image="img", key="k" * 16),
    )
    return FixWorkspace(FakeTester(sb), prepared, WriteGuard([EXAM_PATH]),  # type: ignore[arg-type]
                        exam_code=exam)


# ---------------------------------------------------------------- 工作区改动清单


async def test_workspace_edit_patch_and_limits():
    sb = FakeSandbox()
    ws = make_workspace(sb)
    assert "current = None" in (ws.original(PARSER) or "")
    with pytest.raises(GuardError, match="没有找到"):
        ws.edit(PARSER, "no such text", "x")
    with pytest.raises(GuardError, match="出现了"):
        ws.edit(PARSER, "sections", "s")  # 出现多次
    with pytest.raises(GuardError, match="已存在"):
        ws.edit(PARSER, "", "x")
    with pytest.raises(GuardError, match="不存在"):
        ws.edit("confkit/nope.py", "a", "b")
    msg = ws.edit(PARSER, "    current = None\n", "    current = DEFAULT\n")
    assert "1 个文件" in msg
    assert ws.changed_files() == [PARSER]
    patch = ws.patch()
    assert patch.startswith(f"--- a/{PARSER}\n+++ b/{PARSER}\n")
    assert "-    current = None" in patch and "+    current = DEFAULT" in patch
    ws.edit("confkit/new_mod.py", "", "X = 1\n")  # 新建文件
    assert "/dev/null" in ws.patch()
    with pytest.raises(GuardError):
        ws.edit(EXAM_PATH, "", "def test_y(): pass\n")
    assert ws.changed_files() == ["confkit/new_mod.py", PARSER]  # 被拒的没有留下痕迹


async def test_workspace_edit_over_limit_is_not_applied():
    ws = make_workspace(FakeSandbox())
    ws.guard.max_lines = 3
    with pytest.raises(GuardError, match="上限"):
        ws.edit(PARSER, "    current = None\n", "\n".join(f"a{i} = {i}" for i in range(10)) + "\n")
    assert ws.changed_files() == []


async def test_workspace_sync_run_tests_and_revert():
    sb = FakeSandbox()
    ws = make_workspace(sb)
    await ws.open("k")
    assert sb.files[ws.volume][f"src/{EXAM_PATH}"] == EXAM  # 考卷已放进工作区
    ws.edit(PARSER, "    current = None\n", "    current = DEFAULT\n")
    await ws.run_tests([EXAM_PATH, "tests/test_parser.py::test_a[1]"])
    assert "current = DEFAULT" in sb.files[ws.volume][f"src/{PARSER}"]
    vol, argv, env = sb.runs[-1]
    assert argv[:5] == ["python", "-m", "pytest", f"src/{EXAM_PATH}",
                        "src/tests/test_parser.py::test_a[1]"]
    assert env == ["PYTHONPATH=/workspace/src"]  # fixture 不是 src 布局
    for bad in ["-c", "--rootdir=/etc", "../x.py", "/etc/passwd", "a b", "tests/x.py;rm"]:
        with pytest.raises(GuardError):
            await ws.run_tests([bad])
    with pytest.raises(GuardError):
        await ws.run_tests([])
    await ws.revert()
    assert ws.changed_files() == []
    assert "current = None" in sb.files[ws.volume][f"src/{PARSER}"]


async def test_verify_uses_fresh_workspace_with_host_edits_only():
    sb = FakeSandbox()
    ws = make_workspace(sb)
    await ws.open("k")
    ws.edit(PARSER, "    current = None\n", "    current = DEFAULT\n")
    # Agent 在工作区里直接改了文件（绕过工具层）：不应出现在验收用的全新工作区里
    sb.files[ws.volume][f"src/{PARSER}"] = "CHEATED = True\n"
    sb.files[ws.volume][f"src/{EXAM_PATH}"] = "def test_x():\n    pass\n"
    sb.pytest_exits = [0]
    res = await ws.verify_acceptance()
    assert res.exit_code == 0
    fresh = sb.runs[-1][0]
    assert fresh != ws.volume
    assert sb.files.get(fresh) is None  # 用完即删
    assert sb.runs[-1][1][:3] == ["python", "-m", "pytest"] and sb.runs[-1][1][-1] == "-rA"


# ---------------------------------------------------------------- 图的控制流（剧本式 LLM）


def call(name: str, args: dict[str, Any], cid: str | None = None) -> dict[str, Any]:
    return {"id": cid or f"c-{name}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


class ScriptedLLM:
    def __init__(self, replies: list[dict[str, Any]], *, prompt_tokens: int = 1000) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []
        self.prompt_tokens = prompt_tokens

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        reply = self.replies.pop(0) if self.replies else {
            "tool_calls": [call("give_up", {"reason": "脚本用完"})]}
        message: dict[str, Any] = {"role": "assistant", "content": reply.get("content", "")}
        if reply.get("tool_calls"):
            message["tool_calls"] = reply["tool_calls"]
        return httpx.Response(200, json={
            "model": "deepseek-flash", "choices": [{"message": message}],
            "usage": {"prompt_tokens": self.prompt_tokens, "completion_tokens": 50,
                      "prompt_cache_hit_tokens": 0},
        })

    def client(self) -> LLMClient:
        return LLMClient("http://llm", "k", transport=httpx.MockTransport(self.handler))


def plan(**kw: Any) -> dict[str, Any]:
    args = {"hypothesis": "current 初始为 None", "files": [PARSER], "approach": "默认 DEFAULT"}
    return {"tool_calls": [call("submit_plan", {**args, **kw})]}


FIX_EDIT = {"tool_calls": [call("edit_file", {
    "path": PARSER, "old": "    current = None\n", "new": "    current = DEFAULT\n"})]}
FINISH = {"tool_calls": [call("finish_edit", {"summary": "改了初始值"})]}


def task(*, exam: str | None = EXAM) -> FixTask:
    return FixTask(repo="fixture/bug-keyerror", number=101,
                   issue=IssueContext(title="crash", body="KeyError: None"),
                   test_path=EXAM_PATH, test_code=exam)


def agent_for(sb: FakeSandbox, llm: ScriptedLLM, ws: FixWorkspace, t: FixTask, **kw: Any
              ) -> FixAgent:
    return FixAgent(llm.client(), "deepseek-flash", ws, t, **kw)


async def test_graph_passes_on_first_round():
    sb = FakeSandbox()
    ws = make_workspace(sb)
    await ws.open("k")
    sb.pytest_exits = [0]
    llm = ScriptedLLM([
        {"tool_calls": [call("read_file", {"path": PARSER, "start": 20, "end": 40})]},
        plan(), FIX_EDIT, FINISH,
    ])
    res = await agent_for(sb, llm, ws, task()).run()
    assert res.status == "passed" and res.passed
    assert res.files == [PARSER] and "+    current = DEFAULT" in res.patch
    assert len(res.attempts) == 1 and res.attempts[0].exit_code == 0
    assert res.tool_counts["edit_file"] == 1 and res.steps == 4
    assert res.cost_usd > 0 and res.denied == 0


async def test_graph_reflects_and_replans_then_passes():
    sb = FakeSandbox()
    ws = make_workspace(sb)
    await ws.open("k")
    sb.pytest_exits = [1, 0]  # 第一轮验收失败，第二轮通过
    llm = ScriptedLLM([
        plan(), FIX_EDIT, FINISH,
        {"content": json.dumps({"analysis": "输出里 KeyError: None 仍然存在，改动没起作用",
                                "next": "replan"})},
        plan(hypothesis="_store 应该 setdefault"),
        {"tool_calls": [call("reset_edits", {})]},
        {"tool_calls": [call("edit_file", {
            "path": PARSER, "old": "    sections[section][key.strip()]",
            "new": "    sections.setdefault(section, {})[key.strip()]"})]},
        FINISH,
    ])
    res = await agent_for(sb, llm, ws, task()).run()
    assert res.status == "passed"
    assert [a.n for a in res.attempts] == [1, 2]
    assert "KeyError: None" in res.attempts[0].reflection and res.attempts[0].exit_code == 1
    assert res.attempts[1].exit_code == 0
    # 第二轮的规划提示里带着第一轮的补丁、验收输出和反思
    replan_msg = next(r["messages"][1]["content"] for r in llm.requests
                      if "此前的尝试" in r["messages"][1]["content"]
                      and "规划阶段" in r["messages"][1]["content"])
    assert "current = DEFAULT" in replan_msg and "E   KeyError: None" in replan_msg
    assert "输出里 KeyError: None 仍然存在" in replan_msg
    assert res.files == [PARSER]  # reset 之后只剩第二轮的改动
    assert "setdefault" in res.patch and "current = DEFAULT" not in res.patch


async def test_graph_stops_after_max_rounds():
    sb = FakeSandbox()
    ws = make_workspace(sb)
    await ws.open("k")
    sb.pytest_exits = [1, 1]
    replies: list[dict[str, Any]] = []
    for _ in range(2):
        replies += [plan(), FIX_EDIT, FINISH,
                    {"content": json.dumps({"analysis": "still failing", "next": "replan"})}]
    res = await agent_for(sb, llm := ScriptedLLM(replies), ws, task(), max_rounds=2).run()
    assert res.status == "failed" and not res.passed and len(res.attempts) == 2
    assert not llm.replies


async def test_graph_reflection_can_give_up():
    sb = FakeSandbox()
    ws = make_workspace(sb)
    await ws.open("k")
    sb.pytest_exits = [1]
    llm = ScriptedLLM([plan(), FIX_EDIT, FINISH,
                       {"content": json.dumps({"analysis": "需要网络", "next": "give_up"})}])
    res = await agent_for(sb, llm, ws, task()).run()
    assert res.status == "gave_up" and res.give_up_reason == "需要网络"


async def test_graph_plan_give_up():
    sb = FakeSandbox()
    ws = make_workspace(sb)
    await ws.open("k")
    llm = ScriptedLLM([{"tool_calls": [call("give_up", {"reason": "bug 不存在"})]}])
    res = await agent_for(sb, llm, ws, task()).run()
    assert res.status == "gave_up" and res.patch == "" and res.attempts == []


async def test_denied_writes_are_counted_and_fed_back():
    sb = FakeSandbox()
    ws = make_workspace(sb)
    await ws.open("k")
    sb.pytest_exits = [0]
    llm = ScriptedLLM([
        plan(),
        {"tool_calls": [call("edit_file", {"path": EXAM_PATH, "old": "assert False",
                                           "new": "assert True"}, "c1"),
                        call("edit_file", {"path": "tests/conftest.py", "old": "", "new": "x=1\n"},
                             "c2")]},
        FIX_EDIT, FINISH,
    ])
    res = await agent_for(sb, llm, ws, task()).run()
    assert res.denied == 2 and res.status == "passed"
    tool_msgs = [m["content"] for r in llm.requests for m in r["messages"] if m["role"] == "tool"]
    assert any("被拒绝" in c and "验收测试" in c for c in tool_msgs)
    assert res.files == [PARSER]


async def test_control_group_has_no_exam_and_ends_without_verify():
    sb = FakeSandbox()
    ws = make_workspace(sb, exam=None)
    await ws.open("k")
    llm = ScriptedLLM([plan(), FIX_EDIT, FINISH])
    res = await agent_for(sb, llm, ws, task(exam=None)).run()
    assert res.status == "done" and not res.passed and res.files == [PARSER]
    assert f"src/{EXAM_PATH}" not in sb.files[ws.volume]
    first = llm.requests[0]["messages"][1]["content"]
    assert "验收测试（只读" not in first and "没有现成的验收测试" in first
    assert not any(v.startswith("ws2") for v in sb.files)  # 没有开验收用的工作区


async def test_control_group_without_a_patch_is_failed_not_done():
    sb = FakeSandbox()
    ws = make_workspace(sb, exam=None)
    await ws.open("k")
    res = await agent_for(sb, ScriptedLLM([plan(), FINISH]), ws, task(exam=None)).run()
    assert res.status == "failed" and res.patch == ""


async def test_only_permission_denials_are_counted_not_edit_mistakes():
    sb = FakeSandbox()
    ws = make_workspace(sb)
    await ws.open("k")
    sb.pytest_exits = [0]
    llm = ScriptedLLM([plan(), {"tool_calls": [
        call("edit_file", {"path": PARSER, "old": "no such text", "new": "x"}, "c1"),
        call("edit_file", {"path": "tests/test_parser.py", "old": "a", "new": "b"}, "c2"),
        call("run_tests", {"targets": ["--ignore=x"]}, "c3")]}, FIX_EDIT, FINISH])
    res = await agent_for(sb, llm, ws, task()).run()
    assert res.denied == 1  # 只有改 tests/ 那一次



async def test_no_changes_is_a_failed_attempt():
    sb = FakeSandbox()
    ws = make_workspace(sb)
    await ws.open("k")
    llm = ScriptedLLM([plan(), FINISH,
                       {"content": json.dumps({"analysis": "没改", "next": "give_up"})}])
    res = await agent_for(sb, llm, ws, task()).run()
    assert res.attempts[0].output == "没有任何改动。" and res.status == "gave_up"
    assert not any(a[1][:3] == ["python", "-m", "pytest"] for a in sb.runs)  # 没有浪费一次运行


async def test_budget_stops_the_loop():
    sb = FakeSandbox()
    ws = make_workspace(sb)
    await ws.open("k")
    llm = ScriptedLLM([{"tool_calls": [call("read_file", {"path": PARSER})]}] * 20,
                      prompt_tokens=5_000_000)
    res = await agent_for(sb, llm, ws, task(), budget_usd=0.01).run()
    assert res.status == "budget" and res.steps == 1


async def test_model_without_tool_calls_errors_out():
    sb = FakeSandbox()
    ws = make_workspace(sb)
    await ws.open("k")
    llm = ScriptedLLM([{"content": "我想想"}] * 5)
    res = await agent_for(sb, llm, ws, task()).run()
    assert res.status == "error" and "不调用工具" in (res.error or "")


async def test_transcript_is_saved(tmp_path: Path):
    sb = FakeSandbox()
    ws = make_workspace(sb)
    await ws.open("k")
    sb.pytest_exits = [0]
    llm = ScriptedLLM([plan(), FIX_EDIT, FINISH])
    res = await agent_for(sb, llm, ws, task(), artifacts_dir=tmp_path).run()
    text = await asyncio.to_thread(Path(res.transcript_path or "").read_text, encoding="utf-8")
    data = json.loads(text)
    assert [p["phase"] for p in data["phases"]] == ["plan", "edit"]
    assert "test_code" not in data["task"] and data["result"]["status"] == "passed"


def test_clip():
    text = "\n".join(str(i) for i in range(200))
    out = clip(text, 5, 5)
    assert "省略 190 行" in out and out.startswith("0\n") and out.endswith("199")


# ---------------------------------------------------------------- 真实 Docker：绕过工具层没有用

KEYERROR_EXAM = '''from confkit import parse


def test_keys_before_first_section_go_to_default() -> None:
    assert parse("name = demo\\n[server]\\nhost = example.org\\n") == {
        "DEFAULT": {"name": "demo"},
        "server": {"host": "example.org"},
    }
'''

CHEAT = '''import pathlib
p = pathlib.Path("/workspace/src/confkit/parser.py")
src = p.read_text().replace("current = None", "current = DEFAULT")
p.write_text(src.replace("sections[section][key", "sections.setdefault(section, {})[key"))
'''


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory: pytest.TempPathFactory) -> DockerSandbox:
    sb = DockerSandbox(artifacts_dir=tmp_path_factory.mktemp("artifacts"))
    if asyncio.run(sb.server_version()) is None:
        pytest.skip("Docker 不可用")
    return sb


async def run_fixture(sandbox: DockerSandbox, tmp_path: Path, llm: ScriptedLLM,
                      **kw: Any) -> Any:
    from failgate.fix.run import fix_tree
    from failgate.repro.pypi import PyPIClient

    fx = fx_mod.load_all(only=["bug-keyerror"])[0]
    pypi = PyPIClient()
    tester = TestReproducer(sandbox, EnvCache(sandbox, tmp_path / "envcache.json"), pypi)
    t = FixTask(repo=fx.repo, number=fx.number,
                issue=IssueContext(title=fx.title, body=fx.body), test_code=KEYERROR_EXAM)
    try:
        return await fix_tree(llm.client(), "deepseek-flash", tester, fx.cfg, fx.tree, t,
                              python=fx.python, version=fx.version, **kw)
    finally:
        await pypi.aclose()
        for entry in _images(tmp_path / "envcache.json"):
            await sandbox.remove_image(entry)


def _images(index: Path) -> list[str]:
    try:
        return [str(v["image"]) for v in json.loads(index.read_text()).values()]
    except (OSError, ValueError):
        return []


@pytest.mark.docker
async def test_docker_real_fix_passes_acceptance(sandbox: DockerSandbox, tmp_path: Path):
    llm = ScriptedLLM([
        {"tool_calls": [call("search_code", {"pattern": "current"})]},
        plan(),
        {"tool_calls": [
            call("edit_file", {"path": PARSER, "old": "    current = None\n",
                               "new": "    current = DEFAULT\n"}, "e1"),
            call("edit_file", {"path": PARSER, "old": "    sections[section][key",
                               "new": "    sections.setdefault(section, {})[key"}, "e2"),
        ]},
        {"tool_calls": [call("run_tests", {"targets": [EXAM_PATH, "tests/test_parser.py"]})]},
        FINISH,
    ])
    res = await run_fixture(sandbox, tmp_path, llm)
    assert res.status == "passed", res.attempts
    assert res.files == [PARSER]
    # 交付的补丁在全新工作区里对封存的考卷通过；Agent 自己也跑过（这里的 run_tests 输出）
    tool_out = [m["content"] for r in llm.requests for m in r["messages"] if m["role"] == "tool"]
    assert any("passed" in c for c in tool_out)


@pytest.mark.docker
async def test_docker_cheating_around_the_tools_does_not_pass(sandbox: DockerSandbox,
                                                              tmp_path: Path):
    llm = ScriptedLLM([
        plan(),
        # 工具层能看到的改动只有一行注释；然后直接在沙箱工作区里改源码（绕过 edit_file）
        {"tool_calls": [call("edit_file", {
            "path": PARSER, "old": "DEFAULT = \"DEFAULT\"\n",
            "new": "DEFAULT = \"DEFAULT\"  # tweak\n"})]},
        {"tool_calls": [call("run_python", {"code": CHEAT})]},
        {"tool_calls": [call("run_tests", {"targets": [EXAM_PATH]})]},
        FINISH,
        {"content": json.dumps({"analysis": "KeyError 仍在", "next": "give_up"})},
    ])
    res = await run_fixture(sandbox, tmp_path, llm)
    tool_out = [m["content"] for r in llm.requests for m in r["messages"] if m["role"] == "tool"]
    assert any("1 passed" in c for c in tool_out)  # 在被改过的工作区里"通过"了……
    assert res.status == "gave_up" and not res.passed  # ……但全新工作区里的验收没通过
    assert res.attempts[0].exit_code == 1
    assert "current = DEFAULT" not in res.patch  # 补丁里也没有那处改动
    assert res.files == [PARSER] and "tweak" in res.patch


async def test_plan_step_limit_falls_through_to_edit_with_a_warning():
    sb = FakeSandbox()
    ws = make_workspace(sb)
    await ws.open("k")
    sb.pytest_exits = [0]
    read = {"tool_calls": [call("read_file", {"path": PARSER})]}
    llm = ScriptedLLM([read] * 5 + [FIX_EDIT, FINISH])
    res = await agent_for(sb, llm, ws, task(), plan_steps=5).run()
    assert res.status == "passed"  # 计划没交，也没有丢掉这一轮：直接进入修改
    reminders = [m["content"] for r in llm.requests for m in r["messages"]
                 if m["role"] == "tool" and "[提醒]" in m["content"]]
    assert len(set(reminders)) == 1 and "submit_plan" in reminders[0]


# ---------------------------------------------------------------- 按驳回理由重修（ADR 0029）


async def test_preload_keeps_source_edits_and_drops_protected_files():
    sb = FakeSandbox()
    ws = make_workspace(sb)
    original = ws.original(PARSER)
    assert original is not None
    ws.preload({PARSER: original.replace("current = None", "current = DEFAULT"),
                EXAM_PATH: "def test_x():\n    pass\n",  # 考卷：guard 拒绝，直接丢掉
                "tests/test_parser.py": "x = 1\n"})
    assert ws.changed_files() == [PARSER]
    await ws.open("k")
    await ws.sync()  # 第一次读文件 / 跑测试前会同步
    # 上一版的改动同步进了工作区，Agent 读到的就是它
    assert "current = DEFAULT" in sb.files[ws.volume][f"src/{PARSER}"]


async def test_verify_also_runs_must_pass_tests_in_the_fresh_workspace():
    sb = FakeSandbox()
    ws = make_workspace(sb)
    await ws.open("k")
    ws.edit(PARSER, "    current = None\n", "    current = DEFAULT\n")
    sb.pytest_exits = [0, 1]  # 考卷通过，上一轮被查出的测试仍然失败
    res = await ws.verify_acceptance(["tests/test_parser.py::test_basic", "--deselect=x"])
    assert res.exit_code == 1
    exam_run, extra_run = sb.runs[-2], sb.runs[-1]
    assert exam_run[0] == extra_run[0] != ws.volume  # 同一个全新工作区
    assert "src/tests/test_parser.py::test_basic" in extra_run[1]
    assert not any("deselect" in a for a in extra_run[1])  # 选项一律不收
    # 考卷没过就不跑额外的测试
    sb.pytest_exits = [1]
    n = len(sb.runs)
    await ws.verify_acceptance(["tests/test_parser.py::test_basic"])
    assert len(sb.runs) - n == 1


async def test_feedback_and_must_pass_reach_the_agent_and_the_verify_node():
    sb = FakeSandbox()
    ws = make_workspace(sb)
    await ws.open("k")
    sb.pytest_exits = [0, 0]
    llm = ScriptedLLM([plan(), FIX_EDIT, FINISH])
    t = task()
    t.feedback = "- 相关测试里有 1 个在 PR 的代码上新出现失败（layer3:new_failures）"
    t.must_pass = ["tests/test_parser.py::test_basic"]
    res = await agent_for(sb, llm, ws, t).run()
    assert res.status == "passed"
    first = llm.requests[0]["messages"][1]["content"]
    assert "被驳回了" in first and "tests/test_parser.py::test_basic" in first
    assert any("src/tests/test_parser.py::test_basic" in r[1] for r in sb.runs)


# ---------------------------------------------------------------- 规划 → 修改的交接（ADR 0030）


def test_pin_ranges_caps_ranges_and_lines():
    raw = [{"path": "./src/a.py", "start": 10, "end": 20},
           {"path": "b.py", "start": 5, "end": 3},  # 倒着的：丢掉
           {"path": "c.py", "start": "x", "end": 9},  # 不是数字：丢掉
           "not a dict",
           {"path": "d.py", "start": 1, "end": 10_000}]  # 超过总行数：截断
    pins = pin_ranges(raw)
    assert pins[0] == ("src/a.py", 10, 20)
    assert pins[1] == ("d.py", 1, PIN_MAX_LINES - 11)
    assert sum(e - s + 1 for _, s, e in pins) == PIN_MAX_LINES
    assert pin_ranges(None) == [] and pin_ranges("x") == []
    assert len(pin_ranges([{"path": f"f{i}.py", "start": 1, "end": 2} for i in range(9)])) == 6


def test_merge_ranges_and_overlaps():
    seen = [("a.py", 10, 20), ("a.py", 21, 30), ("a.py", 50, 60), ("b.py", 1, 5)]
    assert merge_ranges(seen) == {"a.py": [(10, 30), (50, 60)], "b.py": [(1, 5)]}
    assert overlaps(("a.py", 25, 40), seen) and overlaps(("a.py", 1, 10), seen)
    assert not overlaps(("a.py", 31, 49), seen) and not overlaps(("c.py", 1, 5), seen)


def _read(start: int, end: int) -> dict[str, Any]:
    return {"tool_calls": [call("read_file", {"path": PARSER, "start": start, "end": end})]}


async def test_reset_counts_rereads_and_first_edit_step():
    sb = FakeSandbox()
    ws = make_workspace(sb)
    await ws.open("k")
    sb.pytest_exits = [0]
    llm = ScriptedLLM([_read(20, 40), plan(), _read(20, 40), _read(100, 120), FIX_EDIT, FINISH])
    res = await agent_for(sb, llm, ws, task()).run()
    assert res.status == "passed" and res.handoff == "reset"
    # 假沙箱每次只回一行：规划读了 20，修改阶段读 20（重读）和 100（新的）
    assert res.edit_reads == 2 and res.rereads == 1
    assert res.first_edit_step == 5
    assert len(llm.requests[2]["messages"]) == 2  # 修改阶段从空白开始


async def test_notes_pins_current_code_and_lists_plan_reads():
    sb = FakeSandbox()
    ws = make_workspace(sb)
    await ws.open("k")
    sb.pytest_exits = [0]
    llm = ScriptedLLM([
        _read(20, 40),
        plan(key_code=[{"path": f"./{PARSER}", "start": 30, "end": 40}]),
        FIX_EDIT, FINISH,
    ])
    res = await agent_for(sb, llm, ws, task(), handoff="notes").run()
    assert res.status == "passed" and res.handoff == "notes"
    submit = next(t for t in llm.requests[0]["tools"] if t["function"]["name"] == "submit_plan")
    assert "key_code" in submit["function"]["parameters"]["required"]
    edit_msgs = llm.requests[2]["messages"]
    assert len(edit_msgs) == 2  # 仍然是新的上下文，只是多了交接笔记
    notes = edit_msgs[1]["content"]
    assert "规划阶段的交接笔记" in notes and f"`{PARSER}` 第 30–40 行" in notes
    assert "sections[section]" in notes  # 钉进来的是工作区里的当前内容
    assert f"- {PARSER}: 20–20" in notes
    assert any(r[1][3] == "read" and r[1][-2:] == ["30", "40"] for r in sb.runs)


async def test_continue_keeps_plan_context_within_a_round_and_resets_between_rounds():
    sb = FakeSandbox()
    ws = make_workspace(sb)
    await ws.open("k")
    sb.pytest_exits = [1, 0]
    llm = ScriptedLLM([
        _read(20, 40), plan(), FIX_EDIT, FINISH,
        {"content": json.dumps({"analysis": "KeyError: None 还在", "next": "replan"})},
        plan(hypothesis="再看看"), FINISH,
    ])
    agent = agent_for(sb, llm, ws, task(), handoff="continue", max_rounds=2)
    res = await agent.run()
    assert res.status == "passed" and res.handoff == "continue"
    edit1 = llm.requests[2]
    msgs = edit1["messages"]
    # 规划阶段的 6 条（system、任务、读文件、结果、交计划、已记录）原样接着，再加修改阶段的指示
    assert len(msgs) == 7 and msgs[2]["tool_calls"][0]["function"]["name"] == "read_file"
    assert msgs[-1]["role"] == "user" and "不用重读" in msgs[-1]["content"]
    assert "edit_file" in {t["function"]["name"] for t in edit1["tools"]}
    # 第二轮的规划从空白开始，只带上一轮的小结
    replan = llm.requests[5]["messages"]
    assert len(replan) == 2 and "此前的尝试" in replan[1]["content"]
    edit2 = llm.requests[6]["messages"]
    assert not any(tc["function"]["name"] == "read_file"
                   for m in edit2 for tc in m.get("tool_calls") or [])
    edits = [e for e in agent.log if e["phase"] == "edit"]
    assert [e.get("prefix") for e in edits] == [6, 4]
