"""复现 Agent 循环（技术方案 8.5 节）。

    ┌─────────── 最多 max_steps 次工具调用、max_attempts 次提交、budget_usd 美元 ───────────┐
    │  LLM ──tool_call──→ list_files / search_code / read_file   （环境镜像里只读、断网）   │
    │                     write_scratch                            （只能写 .warden/*.py）  │
    │                     run                                      （断网沙箱，只许 python）│
    │                     submit ──→ 全新工作区里只放这一个脚本 ──→ 判定器（ADR 0008）    │
    │                                   │ 复现 → 结束；否则把判定理由反馈给模型，继续    │
    │                     give_up                                  （说明原因和可疑位置）│
    └─────────────────────────────────────────────────────────────────────────────────────┘

为什么是"有边界的循环"而不是自由 Agent（ADR 0001）：整个复现只是状态机里的一个状态，
工具全部只读或只在沙箱里生效，步数、提交次数、花费都有上限，超出就按拿到的最好证据降级。

上下文管理：工具输出只保留开头和结尾若干行，完整内容写进产物目录；每次提交的
"假设 → 脚本 → 判定结果"单独记录，失败原因原样反馈给下一轮。
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, Field

from warden.llm import LLMClient, LLMError, ToolCall, Usage
from warden.repro.codetools import CodeTools
from warden.repro.config import PackageConfig
from warden.repro.envcache import Env
from warden.repro.evidence import EvidenceLevel
from warden.repro.judge import VerdictKind
from warden.repro.l2 import (
    PYTEST_INVALID,
    L2Unsupported,
    SourcePrepared,
    SourceRepro,
    TestReproducer,
    pytest_argv,
)
from warden.repro.package import (
    SETUP_ERRORS,
    IssueContext,
    PackageRepro,
    PackageReproducer,
    Prepared,
    VersionRun,
)
from warden.repro.sandbox import SandboxError
from warden.repro.source import SRC_DIR, SourceError, SourceTree
from warden.skills.base import load_prompt, priced, untrusted

PROMPT_VERSION = "1"
TEST_PROMPT_VERSION = "1"
SCRATCH_DIR = ".warden"
_SCRATCH_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,60}\.(py|txt)$")
MAX_SCRATCH_BYTES = 20_000
RUN_TIMEOUT_S = 60
MAX_NUDGES = 3
STEP_WARNING = 6  # 剩这么多步还没提交时提醒一次

TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "list_files",
        "description": "列出已安装包里的源码文件（相对 site-packages 的路径）。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "子目录，如 black/；留空为整个包"},
        }},
    }},
    {"type": "function", "function": {
        "name": "search_code",
        "description": "在已安装包的源码里按正则搜索，返回 路径:行号: 内容（最多 60 条）。",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Python 正则；不合法时按字面匹配"},
            "path": {"type": "string", "description": "限定目录或文件，留空为整个包"},
        }, "required": ["pattern"]},
    }},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "读取源码文件的若干行（一次最多 250 行）。也可以读自己写的 .warden/ 文件。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "start": {"type": "integer", "description": "起始行号，从 1 开始"},
            "end": {"type": "integer", "description": "结束行号（含）"},
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "write_scratch",
        "description": "在 /workspace/.warden/ 下写文件（覆盖）。只能是 .py 或 .txt。",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "文件名，如 try1.py"},
            "content": {"type": "string"},
        }, "required": ["name", "content"]},
    }},
    {"type": "function", "function": {
        "name": "run",
        "description": "在断网沙箱里执行命令（工作目录 /workspace，只许 python 开头，60 秒超时）。",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "例如 python .warden/try1.py"},
        }, "required": ["command"]},
    }},
    {"type": "function", "function": {
        "name": "submit",
        "description": "提交 .warden/ 下的一个脚本作为候选复现，由独立判定器在全新沙箱里核对。",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "脚本文件名，如 repro.py"},
            "claim": {"type": "string", "description": "一句话：这个脚本复现了什么、怎么失败"},
        }, "required": ["name", "claim"]},
    }},
    {"type": "function", "function": {
        "name": "give_up",
        "description": "确认无法在这个环境里复现时调用。",
        "parameters": {"type": "object", "properties": {
            "reason": {"type": "string"},
            "suspect": {"type": "string", "description": "怀疑的代码位置，如 black/linegen.py:120"},
        }, "required": ["reason"]},
    }},
]

# source 模式（L2）：代码工具看的是仓库源码树；交付物是仓库测试目录里的一个 pytest 文件
_BY_NAME = {t["function"]["name"]: t for t in TOOLS}
TEST_TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "list_files",
        "description": "列出仓库源码树里的 .py 文件"
                       "（路径相对仓库根，如 src/black/linegen.py、tests/）。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "子目录；留空为整个仓库"},
        }},
    }},
    {"type": "function", "function": {
        "name": "search_code",
        "description": "在仓库源码树里按正则搜索（含 tests/），"
                       "返回 路径:行号: 内容（最多 60 条）。",
        "parameters": _BY_NAME["search_code"]["function"]["parameters"],
    }},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "读取仓库里的文件若干行（一次最多 250 行），路径相对仓库根。"
                       "也可以读自己写的 .warden/ 文件。",
        "parameters": _BY_NAME["read_file"]["function"]["parameters"],
    }},
    _BY_NAME["write_scratch"],
    _BY_NAME["run"],
    {"type": "function", "function": {
        "name": "write_test",
        "description": "写（覆盖）这次要交付的测试文件，路径固定（见任务说明）。"
                       "内容是 pytest 测试模块。",
        "parameters": {"type": "object", "properties": {
            "content": {"type": "string"},
        }, "required": ["content"]},
    }},
    {"type": "function", "function": {
        "name": "run_test",
        "description": "按仓库自己的 pytest 配置运行测试文件（断网，120 秒超时），返回输出。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "submit",
        "description": "提交当前的测试文件作为候选复现，由独立判定器放进全新的仓库副本里核对。",
        "parameters": {"type": "object", "properties": {
            "claim": {"type": "string", "description": "一句话：这个测试复现了什么、怎么失败"},
        }, "required": ["claim"]},
    }},
    _BY_NAME["give_up"],
]


class AgentTask(BaseModel):
    repo: str
    number: int | None = None
    issue: IssueContext
    reported_traceback: str | None = None
    reported_version_text: str | None = None
    # issue 创建时间：报告的版本装不到时，用它之前最新的正式版
    created_at: datetime | None = None


class Attempt(BaseModel):
    n: int
    name: str
    claim: str
    script: str
    kind: VerdictKind
    reason: str
    match: float | None = None


Status = Literal["reproduced", "not_reproduced", "gave_up", "budget", "steps", "error"]


class AgentResult(BaseModel):
    status: Status
    attempts: list[Attempt] = Field(default_factory=list)
    final_script: str | None = None
    final_run: VersionRun | None = None
    steps: int = 0
    tool_counts: dict[str, int] = Field(default_factory=dict)
    cost_usd: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    give_up_reason: str | None = None
    suspect: str | None = None
    error: str | None = None
    transcript_path: str | None = None
    duration_s: float = 0.0
    # source 模式：final_script 是测试文件的内容，test_path 是它在仓库里的路径
    test_path: str | None = None


def clip(text: str, head: int = 30, tail: int = 50, max_chars: int = 8000) -> str:
    """长输出只保留开头和结尾若干行。"""
    lines = text.splitlines()
    if len(lines) > head + tail:
        omitted = len(lines) - head - tail
        lines = lines[:head] + [f"…[省略 {omitted} 行]…"] + lines[-tail:]
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[: max_chars // 3] + "\n…[省略]…\n" + out[-(max_chars * 2 // 3):]
    return out


class _Done(Exception):
    """工具调用决定结束循环。"""


class ReproAgent:
    def __init__(
        self,
        llm: LLMClient,
        model: str,
        reproducer: PackageReproducer | TestReproducer,
        *,
        max_steps: int = 40,
        max_attempts: int = 4,
        budget_usd: float = 0.5,
        artifacts_dir: Path | None = None,
    ) -> None:
        self.llm = llm
        self.model = model
        self.reproducer = reproducer
        self.sandbox = reproducer.sandbox
        self.max_steps = max_steps
        self.max_attempts = max_attempts
        self.budget_usd = budget_usd
        self.artifacts_dir = artifacts_dir

    # ---------------------------------------------------------------- 主循环

    async def run(self, task: AgentTask, prepared: Prepared | SourcePrepared) -> AgentResult:
        started = time.monotonic()
        session: _BaseSession
        key = f"agent-{prepared.env.key[:8]}"
        if isinstance(prepared, SourcePrepared):
            # 按 prepared 的类型区分模式；reproducer 用鸭子类型（测试里会注入假的）
            tester = cast(TestReproducer, self.reproducer)
            session = _TestSession(self, task, prepared, tester)
            # 工作区里先放一份仓库源码副本，run_test 在这份副本里跑
            session.volume = await tester.open_workspace(prepared, key)
        else:
            session = _Session(self, task, prepared)
            session.volume = await self.sandbox.create_workspace(key)
        try:
            await session.loop()
        finally:
            await self.sandbox.remove_workspace(session.volume)
        result = session.result
        result.duration_s = round(time.monotonic() - started, 1)
        result.transcript_path = await asyncio.to_thread(session.save_transcript)
        return result


class _BaseSession:
    """两种会话共用的部分：主循环、草稿脚本、试运行、放弃、记录。

    子类决定：系统提示词、可用工具、代码工具看哪里、任务说明、提交后怎么判定。
    """

    prompt_name = f"repro_agent_v{PROMPT_VERSION}"
    tool_defs: list[dict[str, Any]] = TOOLS

    def __init__(self, agent: ReproAgent, task: AgentTask, env: Env) -> None:
        self.agent = agent
        self.task = task
        self.env = env
        self.volume = ""
        self.scratch: dict[str, str] = {}
        self.synced: dict[str, str] = {}
        self.usage = Usage()
        self.result = AgentResult(status="steps")
        self.warned = False
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": load_prompt(self.prompt_name)},
            {"role": "user", "content": self._task_message()},
        ]

    @property
    def tools(self) -> CodeTools:
        raise NotImplementedError

    def _task_message(self) -> str:
        raise NotImplementedError

    def _issue_parts(self) -> list[str]:
        """任务说明里和 issue 有关的部分：正文、堆栈或预期 / 实际行为、限制。"""
        t = self.task
        parts = [untrusted("issue", "issue", f"{t.issue.title}\n\n{t.issue.body[:12000]}")]
        if t.reported_traceback:
            parts.append(
                "程序从报告里提取到的报错堆栈（判定器会用它比对你的脚本的失败）：\n"
                + untrusted("traceback", "traceback", t.reported_traceback)
            )
        else:
            parts.append(
                "报告里没有报错堆栈。判定器会由一个独立的评委对照 issue 描述和脚本的实际输出打分。"
                f"\nIntake 提取的预期行为：{t.issue.expected or '（无）'}"
                f"\nIntake 提取的实际行为：{t.issue.actual or '（无）'}"
            )
        parts.append(
            f"限制：最多 {self.agent.max_steps} 次工具调用、{self.agent.max_attempts} 次提交。"
        )
        return parts

    def _cost(self) -> float:
        return priced(self.agent.model, self.usage)

    async def loop(self) -> None:
        a = self.agent
        nudges = 0
        while self.result.steps < a.max_steps:
            if self._cost() >= a.budget_usd:
                self.result.status = "budget"
                return
            try:
                resp = await a.llm.chat(self.messages, model=a.model, tools=self.tool_defs)
            except LLMError as e:
                self.result.status, self.result.error = "error", str(e)
                return
            self.usage = self.usage + resp.usage
            self._sync_usage()
            msg: dict[str, Any] = {"role": "assistant", "content": resp.text or ""}
            if resp.tool_calls:
                msg["tool_calls"] = [tc.as_message() for tc in resp.tool_calls]
            self.messages.append(msg)
            if not resp.tool_calls:
                nudges += 1
                if nudges >= MAX_NUDGES:
                    self.result.status, self.result.error = "error", "模型连续不调用工具"
                    return
                self.messages.append({
                    "role": "user",
                    "content": "请调用一个工具继续。确认无法复现时调用 give_up。",
                })
                continue
            nudges = 0
            done = False
            for call in resp.tool_calls:
                if done:
                    # 结束后同一轮里剩下的调用也要有回应，否则消息序列不合法
                    content = "已结束，忽略这次调用。"
                else:
                    self.result.steps += 1
                    counts = self.result.tool_counts
                    counts[call.name] = counts.get(call.name, 0) + 1
                    try:
                        content = await self._dispatch(call)
                    except _Done as d:
                        content, done = str(d), True
                self.messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": content}
                )
            if done:
                return
            left = a.max_steps - self.result.steps
            if not self.result.attempts and 0 < left <= STEP_WARNING and not self.warned:
                # 预算快用完还没提交：提醒它把目前最好的脚本交上去（降级拿到最好的证据）
                self.warned = True
                self.messages[-1]["content"] += (
                    f"\n\n[提醒] 只剩 {left} 次工具调用，你还没有提交过。请尽快把目前最好的脚本"
                    "用 submit 提交；确认无法复现就调用 give_up。"
                )
            if self.result.steps >= a.max_steps:
                self.result.status = "steps"

    def _sync_usage(self) -> None:
        self.result.cost_usd = round(self._cost(), 6)
        self.result.prompt_tokens = self.usage.prompt_tokens
        self.result.completion_tokens = self.usage.completion_tokens
        self.result.cached_tokens = self.usage.cached_tokens

    # ---------------------------------------------------------------- 工具

    async def _dispatch(self, call: ToolCall) -> str:
        try:
            args = json.loads(call.arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError("参数必须是 JSON 对象")
        except ValueError as e:
            return f"参数不是合法的 JSON 对象：{e}"
        handler = getattr(self, f"_tool_{call.name}", None)
        if handler is None:
            return f"没有这个工具：{call.name}"
        try:
            result: str = await handler(**args)
        except TypeError as e:
            return f"参数不对：{e}"
        except SandboxError as e:
            return f"沙箱出错：{e}"
        return result

    async def _tool_list_files(self, path: str = "") -> str:
        r = await self.tools.list_files(path)
        if "error" in r:
            return str(r["error"])
        return clip("\n".join(r["files"])) + f"\n（共 {r['total']} 个文件）"

    async def _tool_search_code(self, pattern: str, path: str = "") -> str:
        r = await self.tools.search(pattern, path)
        if "error" in r:
            return str(r["error"])
        if not r["hits"]:
            return "没有匹配。"
        more = "\n（结果太多，已截断，请缩小范围）" if r["truncated"] else ""
        return "\n".join(r["hits"]) + more

    async def _tool_read_file(self, path: str, start: int = 1, end: int | None = None) -> str:
        name = path.removeprefix("/workspace/").removeprefix(f"{SCRATCH_DIR}/")
        if path.startswith((f"{SCRATCH_DIR}/", f"/workspace/{SCRATCH_DIR}/")):
            if name not in self.scratch:
                return f"没有这个文件：{path}"
            lines = self.scratch[name].splitlines()
            s = max(int(start), 1)
            e = min(int(end or len(lines)), len(lines))
            return "\n".join(f"{i:>5}  {lines[i - 1]}" for i in range(s, e + 1))
        r = await self.tools.read(path, int(start), int(end) if end else None)
        if "error" in r:
            return str(r["error"])
        return "\n".join(r["lines"]) + f"\n（文件共 {r['total_lines']} 行）"

    async def _tool_write_scratch(self, name: str, content: str) -> str:
        name = name.removeprefix("/workspace/").removeprefix(f"{SCRATCH_DIR}/")
        if not _SCRATCH_NAME.match(name):
            return "文件名不合法：只能是 .warden/ 下的 .py 或 .txt 文件名，不能带目录。"
        if len(content.encode()) > MAX_SCRATCH_BYTES:
            return f"文件太大（上限 {MAX_SCRATCH_BYTES} 字节），请写最小复现。"
        self.scratch[name] = content
        return f"已写入 {SCRATCH_DIR}/{name}（{len(content.splitlines())} 行）"

    async def _sync_scratch(self) -> None:
        """把有变化的草稿文件复制进工作区卷。"""
        if self.scratch == self.synced:
            return
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp, SCRATCH_DIR)
            await asyncio.to_thread(d.mkdir)
            for name, content in self.scratch.items():
                await asyncio.to_thread((d / name).write_text, content, encoding="utf-8")
            await self.agent.sandbox.copy_in(self.volume, Path(tmp), self.env.image)
        self.synced = dict(self.scratch)

    async def _tool_run(self, command: str) -> str:
        try:
            argv = shlex.split(command)
        except ValueError as e:
            return f"命令解析失败：{e}"
        if not argv or argv[0] not in ("python", "python3"):
            return "只允许以 python 开头的命令（不经过 shell，管道和重定向不可用）。"
        await self._sync_scratch()
        res = await self.agent.sandbox.run(
            self.env.image, self.volume, argv, timeout_s=RUN_TIMEOUT_S,
            allowed=(("python",), ("python3",)),
        )
        head = f"exit={res.exit_code}"
        if res.timed_out:
            head += "（超时）"
        if res.oom_killed:
            head += "（内存超限）"
        body = clip((res.stdout + ("\n[stderr]\n" + res.stderr if res.stderr else "")).strip())
        log = f"\n（完整日志：{res.log_dir}）" if res.log_dir and res.truncated else ""
        return f"{head}\n{body}{log}"

    def _judged(self, name: str, claim: str, script: str, run: VersionRun, noun: str) -> str:
        """记下一次提交的判定；复现了就结束，否则把判定理由反馈给模型。noun：脚本 / 测试。"""
        a = self.agent
        v = run.verdict
        attempt = Attempt(
            n=len(self.result.attempts) + 1, name=name, claim=claim, script=script,
            kind=v.kind, reason=v.reason, match=v.match,
        )
        self.result.attempts.append(attempt)
        if v.reproduced:
            self.result.status = "reproduced"
            self.result.final_script, self.result.final_run = script, run
            raise _Done(f"判定：{v.kind}。{v.reason}。复现成功，结束。")
        feedback = [f"判定：{v.kind}。{v.reason}。"]
        if v.kind == VerdictKind.NOT_REPRODUCED:
            feedback.append(
                # L1 的反馈保持原文（留出集评测用的就是这句）；L2 强调"只断言 issue 说了的"
                "脚本正常退出了。bug 存在时脚本必须失败：让包自己抛出报告里的异常，"
                "或者对 issue 描述的预期行为写 assert。" if noun == "脚本" else
                f"{noun}正常通过了。bug 存在时{noun}必须失败：让包自己抛出报告里的异常，"
                "或者对 issue 明确描述的预期行为写 assert。"
            )
        elif v.kind == VerdictKind.UNRELATED_FAILURE:
            if v.observed is not None:
                feedback.append(
                    f"你的{noun}的失败：{v.observed.exc_type}，包内栈帧 {v.observed.frames}，"
                    f"消息 {v.observed.message!r}。"
                )
            if v.reported is not None:
                feedback.append(
                    f"报告的失败：{v.reported.exc_type}，包内栈帧 {v.reported.frames}，"
                    f"消息 {v.reported.message!r}。"
                )
            if run.semantic is not None:
                feedback.append(f"评委的理由：{run.semantic.reason}")
        feedback.append(f"{noun}输出结尾：\n{clip(run.output_tail, 10, 25)}")
        if len(self.result.attempts) >= a.max_attempts:
            self.result.status = "not_reproduced"
            raise _Done("\n".join(feedback) + "\n提交次数已用完，结束。")
        left = a.max_attempts - len(self.result.attempts)
        feedback.append(f"还可以提交 {left} 次。")
        return "\n".join(feedback)

    async def _tool_give_up(self, reason: str, suspect: str = "") -> str:
        self.result.status = "gave_up"
        self.result.give_up_reason, self.result.suspect = reason, suspect or None
        raise _Done("已记录，结束。")

    # ---------------------------------------------------------------- 记录

    def save_transcript(self) -> str | None:
        d = self.agent.artifacts_dir
        if d is None:
            return None
        d.mkdir(parents=True, exist_ok=True)
        ref = f"{self.task.repo.replace('/', '__')}-{self.task.number or 'x'}"
        path = d / f"agent-{ref}-{int(time.time())}.json"
        path.write_text(
            json.dumps(
                {"task": self.task.model_dump(mode="json"),
                 "result": self.result.model_dump(mode="json"),
                 "messages": self.messages},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        return str(path)


class _Session(_BaseSession):
    """package 模式（L1）：看 site-packages 里的包源码，交付一个独立脚本。"""

    def __init__(self, agent: ReproAgent, task: AgentTask, prepared: Prepared) -> None:
        self.prepared = prepared
        super().__init__(agent, task, prepared.env)

    @property
    def tools(self) -> CodeTools:
        return CodeTools(self.agent.sandbox, self.env.image, self.volume, self.prepared.cfg.module)

    def _task_message(self) -> str:
        p, t = self.prepared, self.task
        parts = [
            f"仓库：{t.repo}" + (f"，issue #{t.number}" if t.number else ""),
            f"包：{p.cfg.name}（import 名 `{p.cfg.module}`），沙箱里装的版本：{p.resolved.version}"
            f"（报告原文：{t.reported_version_text!r}），Python {p.python}",
            *(
                [f"注意：报告的版本装不到，沙箱里装的是 issue 提交前最新的正式版 "
                 f"{p.resolved.version}。bug 可能是在这之后才引入的；如果确认这个版本上"
                 f"不存在该问题，调用 give_up 并说明。"]
                if p.resolved.substituted_for else []
            ),
        ]
        return "\n\n".join(parts + self._issue_parts())

    async def _tool_submit(self, name: str, claim: str) -> str:
        name = name.removeprefix("/workspace/").removeprefix(f"{SCRATCH_DIR}/")
        if name not in self.scratch or not name.endswith(".py"):
            return f"没有这个脚本：{SCRATCH_DIR}/{name}。先用 write_scratch 写好。"
        reproducer, p = cast(PackageReproducer, self.agent.reproducer), self.prepared
        script = self.scratch[name]
        run = await reproducer.evaluate(
            p.cfg, p.env, str(p.resolved.version), script,
            reported_traceback=self.task.reported_traceback, issue=self.task.issue,
        )
        return self._judged(name, claim, script, run, "脚本")


class _TestSession(_BaseSession):
    """source 模式（L2）：看仓库源码树（含 tests/），交付仓库测试目录里的一个 pytest 文件。"""

    prompt_name = f"repro_test_v{TEST_PROMPT_VERSION}"
    tool_defs = TEST_TOOLS

    def __init__(
        self, agent: ReproAgent, task: AgentTask, prepared: SourcePrepared,
        tester: TestReproducer,
    ) -> None:
        self.prepared = prepared
        self.tester = tester
        self.test_code: str | None = None
        super().__init__(agent, task, prepared.env)

    @property
    def tools(self) -> CodeTools:
        # 读镜像里的原始源码（不是工作区副本）：Agent 看到的永远是这个提交本来的样子
        return CodeTools(self.agent.sandbox, self.env.image, self.volume, SRC_DIR)

    def _task_message(self) -> str:
        p, t = self.prepared, self.task
        day = f"{p.tree.committed_at:%Y-%m-%d}" if p.tree.committed_at else "日期未知"
        parts = [
            f"仓库：{t.repo}" + (f"，issue #{t.number}" if t.number else ""),
            f"源码：{p.tree.repo} 的提交 {p.tree.sha[:10]}（{day}），已从源码装好"
            f"（包 {p.cfg.name}，import 名 `{p.cfg.module}`），Python {p.python}，{p.pytest}。"
            f"用户报告的版本原文：{t.reported_version_text!r}。",
            f"你要交付的测试文件：`{p.test_path}`（相对仓库根，路径固定）。"
            f"运行方式：`{shlex.join(pytest_argv(p.test_path))}`，"
            "即按仓库自己的 pytest 配置运行（conftest、filterwarnings 等都生效）。",
        ]
        return "\n\n".join(parts + self._issue_parts())

    async def _tool_write_test(self, content: str) -> str:
        if len(content.encode()) > MAX_SCRATCH_BYTES:
            return f"文件太大（上限 {MAX_SCRATCH_BYTES} 字节），请写最小的测试。"
        self.test_code = content
        return f"已写入 {self.prepared.test_path}（{len(content.splitlines())} 行）"

    async def _tool_run_test(self) -> str:
        if self.test_code is None:
            return "还没有测试文件，先用 write_test 写好。"
        await self.tester.write_test(self.volume, self.prepared, self.test_code)
        res = await self.tester.run_test(self.volume, self.prepared, timeout_s=RUN_TIMEOUT_S)
        head = f"exit={res.exit_code}"
        if res.timed_out:
            head += "（超时）"
        elif res.exit_code in PYTEST_INVALID:
            head += f"（{PYTEST_INVALID[res.exit_code]}；判定器会判为无关失败）"
        body = clip((res.stdout + ("\n[stderr]\n" + res.stderr if res.stderr else "")).strip())
        return f"{head}\n{body}"

    async def _tool_submit(self, claim: str) -> str:
        if self.test_code is None:
            return "还没有测试文件，先用 write_test 写好。"
        code = self.test_code
        run = await self.tester.evaluate(
            self.prepared, code,
            reported_traceback=self.task.reported_traceback, issue=self.task.issue,
        )
        self.result.test_path = self.prepared.test_path
        return self._judged(self.prepared.test_path, claim, code, run, "测试")


async def reproduce_with_tests(
    agent: ReproAgent,
    cfg: PackageConfig,
    task: AgentTask,
    tree: SourceTree,
    *,
    python: str | None = None,
    version: str | None = None,
) -> tuple[SourceRepro, AgentResult | None]:
    """source 模式：在某个提交上准备环境（含预检）→ Agent 写仓库内的失败测试 → L2。"""
    tester = cast(TestReproducer, agent.reproducer)
    out = SourceRepro(repo=tree.repo, sha=tree.sha, committed_at=tree.committed_at,
                      package=cfg.name, module=cfg.module)
    try:
        prepared = await tester.prepare(cfg, tree, number=task.number, python=python,
                                        version=version)
    except (*SETUP_ERRORS, SourceError, L2Unsupported) as e:
        out.error = str(e)[:1000]
        return out, None
    out.python, out.version, out.pytest = prepared.python, prepared.version, prepared.pytest
    out.test_path = prepared.test_path
    result = await agent.run(task, prepared)
    if result.final_run is None:
        out.error = None if result.attempts else f"Agent 没有提交测试（{result.status}）"
        return out, result
    out.run = result.final_run
    out.level = EvidenceLevel.L2
    return out, result


async def reproduce_with_agent(
    agent: ReproAgent,
    cfg: PackageConfig,
    task: AgentTask,
    *,
    env_python: str | None = None,
    preferred_python: str | None = None,
    check_latest: bool = True,
) -> tuple[PackageRepro, AgentResult | None]:
    """准备报告版本的环境 → 复现 Agent 写脚本并提交 → 复现了再到最新版上复查。"""
    reproducer = cast(PackageReproducer, agent.reproducer)
    out = PackageRepro(package=cfg.name, module=cfg.module)
    try:
        prepared = await reproducer.prepare(
            cfg, reported_version=task.reported_version_text, env_python=env_python,
            preferred_python=preferred_python, fallback_before=task.created_at,
        )
    except SETUP_ERRORS as e:
        out.error = str(e)
        return out, None
    out.reported_version = str(prepared.resolved.version)
    out.substituted_for = prepared.resolved.substituted_for
    out.latest_version = str(prepared.resolved.latest)
    result = await agent.run(task, prepared)
    if result.final_run is None:
        # 没复现：最后一次提交的判定就是报告版本上的结果（没提交过则为空）
        out.error = None if result.attempts else f"Agent 没有提交脚本（{result.status}）"
        return out, result
    out.reported = result.final_run
    assert result.final_script is not None
    await reproducer.finish(
        out, prepared, result.final_script, reported_traceback=task.reported_traceback,
        issue=task.issue, check_latest=check_latest,
    )
    return out, result
