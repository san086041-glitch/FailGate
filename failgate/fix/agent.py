"""修复 Agent 的图（技术方案 10 节，ADR 0027）。

        ┌───────────────────────────── 重规划 ──────────────────────────────┐
        ▼                                                                    │
    plan ──→ edit ──→ verify ──→ 通过 / 没有考卷 ──→ END                    │
    (只读探索,    (改代码、跑测试、   (全新工作区跑封存的考卷)                │
     交计划)       写草稿脚本)            │ 没通过                            │
                                          ▼                                 │
                                       reflect ──→ 还有预算 ────────────────┘
                                       (引用真实输出)   └→ 用完 → END

为什么用 LangGraph（而不是像复现 Agent 那样手写循环）：规划、反思、重规划是显式的分支，
状态（计划、每一轮尝试、预算）要能落检查点、能逐步查看；两个 Agent 共用一套框架，
不重复造轮子。图只管"控制流"，每个节点里的工具调用循环是有边界的小循环。

权限（差异点 E）：写只有 edit_file，先过 WriteGuard，只能改被测源码；最终验收在全新工作区里
用宿主机上的改动清单重跑封存的考卷（workspace.py）。对照组没有考卷，verify 直接结束。

规划 → 修改的交接（handoff，ADR 0030）。W9 先量发现修改阶段 64% 的读取是规划阶段读过的：
- reset：修改阶段从空白开始，只拿 3 行计划（原来的做法）；
- notes：交计划时指定要改 / 要参照的代码范围，系统把这些行的当前内容钉进修改阶段，再附上读过的范围；
- continue：同一轮里修改阶段接着规划阶段的对话继续。
三种都在轮与轮之间重置（只带小结、补丁、验收输出和反思），不让失败的探索一路累积。

情景记忆（ADR 0032）：给了 memory 就多一个 recall_fixes 工具，查这个仓库以前合并的修改
（只含 issue 创建之前合并的）；规划阶段开头还会自动列出几条最相关的（不带 diff）。
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from failgate.fix.guard import Denied, GuardError
from failgate.fix.workspace import FixWorkspace
from failgate.llm import LLMClient, LLMError, ToolCall, Usage
from failgate.memory.episodic import EpisodicMemory
from failgate.memory.episodic import render as render_episodes
from failgate.repro.package import IssueContext
from failgate.repro.sandbox import ExecResult, SandboxError
from failgate.skills.base import load_prompt, priced, untrusted

PROMPT_VERSION = "1"
MAX_NUDGES = 3
STEP_WARNING = 4


def _fn(name: str, description: str, props: dict[str, Any] | None = None,
        required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props or {}, "required": required or []},
    }}


_LIST = _fn("list_files", "列出仓库里的 .py 文件（路径相对仓库根）。",
            {"path": {"type": "string", "description": "子目录；留空为整个仓库"}})
_SEARCH = _fn("search_code", "在仓库里按正则搜索，返回 路径:行号: 内容（最多 60 条）。",
              {"pattern": {"type": "string"}, "path": {"type": "string"}}, ["pattern"])
_READ = _fn("read_file", "读取仓库里的文件若干行（一次最多 250 行），看到的是含你改动的当前状态。",
            {"path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}},
            ["path"])
EXPLORE = [_LIST, _SEARCH, _READ]
RECALL = _fn(
    "recall_fixes",
    "查这个仓库以前合并过的相关修改（维护者写的，只包含这个 issue 之前合并的）："
    "PR 标题、关闭的 issue、改了哪些函数和 diff。可以按描述查，也可以加 path 只看改过某个文件的。",
    {"query": {"type": "string", "description": "用自然语言或函数名描述要找的修改"},
     "path": {"type": "string", "description": "只看改过这个文件的（可选）"}},
    ["query"])
RECALL_K = 3
AUTO_RECALL_K = 3

SUBMIT_PLAN = _fn(
    "submit_plan", "交出修复计划，进入修改阶段。",
    {"hypothesis": {"type": "string", "description": "根因假设：哪段代码为什么出错"},
     "files": {"type": "array", "items": {"type": "string"}, "description": "打算改的文件"},
     "approach": {"type": "string", "description": "打算怎么改（一两句）"}},
    ["hypothesis", "approach"])
PIN_MAX_LINES = 300  # notes 交接最多钉进多少行代码
PIN_MAX_RANGES = 6
SUBMIT_PLAN_NOTES = _fn(
    "submit_plan", "交出修复计划，进入修改阶段。",
    {**SUBMIT_PLAN["function"]["parameters"]["properties"],
     "key_code": {
         "type": "array",
         "description": (f"修改阶段要改、要对照的代码范围（最多 {PIN_MAX_RANGES} 段、"
                         f"共 {PIN_MAX_LINES} 行）。系统会把这些行的当前内容直接交给修改阶段，"
                         "不用再读。"),
         "items": {"type": "object",
                   "properties": {"path": {"type": "string"}, "start": {"type": "integer"},
                                  "end": {"type": "integer"}},
                   "required": ["path", "start", "end"]}}},
    ["hypothesis", "approach", "key_code"])
EDIT_TOOLS = [
    *EXPLORE,
    _fn("edit_file",
        "字符串替换式编辑：把文件里唯一出现的 old 片段换成 new。old 传空字符串表示新建文件。"
        "只能改被测源码；测试、conftest、pytest 配置和项目元数据会被拒绝。",
        {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
        ["path", "old", "new"]),
    _fn("run_tests", "在断网沙箱里按仓库的 pytest 配置运行测试（多个文件或 路径::节点）。",
        {"targets": {"type": "array", "items": {"type": "string"}}}, ["targets"]),
    _fn("run_python", "运行一段临时 python 脚本观察代码的实际行为（不是交付物，不进补丁）。",
        {"code": {"type": "string"}}, ["code"]),
    _fn("reset_edits", "撤销到目前为止的所有改动，回到原始代码。"),
    _fn("finish_edit", "改完了，提交给验收。", {"summary": {"type": "string"}}, ["summary"]),
]

Status = Literal["passed", "done", "failed", "gave_up", "budget", "error"]
Handoff = Literal["reset", "notes", "continue"]
HANDOFFS: tuple[str, ...] = ("reset", "notes", "continue")


class FixTask(BaseModel):
    repo: str
    number: int | None = None
    issue: IssueContext
    reported_traceback: str | None = None
    test_path: str | None = None  # 验收测试在仓库里的路径（对照组为 None）
    test_code: str | None = None  # 验收测试的完整代码（对照组为 None）
    # 按驳回理由重修（ADR 0029）：上一版补丁被 ClaimVerify 驳回的理由，以及必须一起通过的测试
    feedback: str | None = None
    must_pass: list[str] = Field(default_factory=list)
    # 情景记忆（ADR 0032）：只能看到这个时间之前合并的修改（issue 的创建时间）；
    # exclude_prs 是回放时额外排除的上游修复 PR（时间上本来就看不到，再保险一次）
    memory_before: datetime | None = None
    exclude_prs: list[int] = Field(default_factory=list)


class FixAttempt(BaseModel):
    n: int
    hypothesis: str = ""
    approach: str = ""
    summary: str = ""
    patch: str = ""
    exit_code: int | None = None  # 验收测试的退出码（没有考卷为 None）
    output: str = ""
    reflection: str = ""


class FixResult(BaseModel):
    status: Status
    passed: bool = False  # 封存的验收测试在全新工作区里通过
    patch: str = ""
    files: list[str] = Field(default_factory=list)
    edits: dict[str, str] = Field(default_factory=dict)  # 改过的文件的完整内容（给金标准判定用）
    attempts: list[FixAttempt] = Field(default_factory=list)
    steps: int = 0
    tool_counts: dict[str, int] = Field(default_factory=dict)
    cost_usd: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    denied: int = 0  # 被工具层拒绝的写入次数（角色隔离实验用）
    # 交接实验（ADR 0030）：修改阶段的 read_file 次数、其中和之前阶段读过的范围重叠的次数、
    # 第一次 edit_file 在第几步（全程计数）
    handoff: str = "reset"
    memory: bool = False
    recalled_prs: list[int] = Field(default_factory=list)  # 给 Agent 看过的历史 PR（含自动列出的）
    edit_reads: int = 0
    rereads: int = 0
    first_edit_step: int | None = None
    error: str | None = None
    give_up_reason: str | None = None
    duration_s: float = 0.0
    transcript_path: str | None = None


def merge_attempts(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按轮次合并：同一轮的更新覆盖旧的，新的一轮追加。状态里放普通 dict，检查点好序列化。"""
    by_n = {a["n"]: a for a in old}
    for a in new:
        by_n[a["n"]] = a
    return [by_n[n] for n in sorted(by_n)]


class FixState(TypedDict):
    plan: str
    round: int
    attempts: Annotated[list[dict[str, Any]], merge_attempts]
    status: str
    ended: str  # 为什么提前结束：budget / steps / error / gave_up / ""


def clip(text: str, head: int = 25, tail: int = 45, max_chars: int = 7000) -> str:
    lines = text.splitlines()
    if len(lines) > head + tail:
        lines = lines[:head] + [f"…[省略 {len(lines) - head - tail} 行]…"] + lines[-tail:]
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[: max_chars // 3] + "\n…[省略]…\n" + out[-(max_chars * 2 // 3):]
    return out


Range = tuple[str, int, int]


def _norm_path(path: str) -> str:
    p = path.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def overlaps(r: Range, seen: list[Range]) -> bool:
    p, s, e = r
    return any(p == q and s <= qe and qs <= e for q, qs, qe in seen)


def merge_ranges(ranges: list[Range]) -> dict[str, list[tuple[int, int]]]:
    """按文件合并重叠或相邻的行范围。"""
    out: dict[str, list[tuple[int, int]]] = {}
    for p, s, e in sorted(ranges):
        spans = out.setdefault(p, [])
        if spans and s <= spans[-1][1] + 1:
            spans[-1] = (spans[-1][0], max(spans[-1][1], e))
        else:
            spans.append((s, e))
    return out


def pin_ranges(raw: Any) -> list[Range]:
    """submit_plan 的 key_code → 有效范围，按上限截断（段数、总行数）。"""
    out: list[Range] = []
    budget = PIN_MAX_LINES
    for item in raw if isinstance(raw, list) else []:
        if len(out) >= PIN_MAX_RANGES or budget <= 0 or not isinstance(item, dict):
            continue
        try:
            path = _norm_path(str(item["path"]))
            start = max(int(item["start"]), 1)
            end = int(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not path or end < start:
            continue
        end = min(end, start + budget - 1)
        out.append((path, start, end))
        budget -= end - start + 1
    return out


def _exec_text(res: ExecResult) -> str:
    head = f"exit={res.exit_code}" + ("（超时）" if res.timed_out else "") \
        + ("（内存超限）" if res.oom_killed else "")
    body = (res.stdout + ("\n[stderr]\n" + res.stderr if res.stderr else "")).strip()
    return f"{head}\n{clip(body)}"


class _Stop(Exception):
    """终止工具被调用：带着它的参数结束当前阶段的循环。"""

    def __init__(self, args: dict[str, Any]) -> None:
        super().__init__("stop")
        self.args_ = args


class FixAgent:
    def __init__(
        self,
        llm: LLMClient,
        model: str,
        ws: FixWorkspace,
        task: FixTask,
        *,
        max_rounds: int = 3,
        plan_steps: int = 20,
        edit_steps: int = 40,
        budget_usd: float = 0.5,
        thinking: str | None = None,
        artifacts_dir: Path | None = None,
        handoff: Handoff = "reset",
        memory: EpisodicMemory | None = None,
    ) -> None:
        if handoff not in HANDOFFS:
            raise ValueError(f"未知的交接方式：{handoff}")
        self.llm, self.model, self.ws, self.task = llm, model, ws, task
        self.max_rounds, self.plan_steps, self.edit_steps = max_rounds, plan_steps, edit_steps
        self.budget_usd, self.thinking, self.artifacts_dir = budget_usd, thinking, artifacts_dir
        self.handoff: Handoff = handoff
        self.memory = memory
        self.usage = Usage()
        self.result = FixResult(status="failed", handoff=handoff, memory=memory is not None)
        self.log: list[dict[str, Any]] = []  # 每个阶段的完整消息，写进 transcript
        self.exhausted = ""  # budget / error / steps
        # 读取记录：之前各阶段读过的范围、当前阶段读的范围（交接和重读统计用）
        self._phase = ""
        self._seen: list[Range] = []
        self._phase_reads: list[Range] = []
        # 本轮规划阶段留给修改阶段的东西（图按轮顺序执行，放实例上即可）
        self._plan_msgs: list[dict[str, Any]] = []
        self._plan_reads: list[Range] = []
        self._pins: list[Range] = []


    # ---------------------------------------------------------------- 图

    def build(self) -> Any:
        g = StateGraph(FixState)
        g.add_node("plan", self.plan_node)
        g.add_node("edit", self.edit_node)
        g.add_node("verify", self.verify_node)
        g.add_node("reflect", self.reflect_node)
        g.add_edge(START, "plan")
        g.add_conditional_edges("plan", lambda s: "end" if s["ended"] else "edit",
                                {"end": END, "edit": "edit"})
        g.add_edge("edit", "verify")
        g.add_conditional_edges("verify", self._after_verify,
                                {"end": END, "reflect": "reflect"})
        g.add_conditional_edges("reflect", self._after_reflect,
                                {"end": END, "plan": "plan"})
        return g.compile(checkpointer=InMemorySaver())

    async def run(self) -> FixResult:
        started = time.monotonic()
        graph = self.build()
        init: FixState = {"plan": "", "round": 0, "attempts": [], "status": "", "ended": ""}
        cfg: Any = {"configurable": {"thread_id": f"fix-{self.task.repo}-{self.task.number}"},
                    "recursion_limit": 6 * self.max_rounds + 6}
        try:
            final = await graph.ainvoke(init, cfg)
        except (LLMError, SandboxError) as e:
            final = {"attempts": [], "status": "error", "ended": "error"}
            self.result.error = str(e)[:500]
        r = self.result
        r.attempts = [FixAttempt(**a) for a in final.get("attempts", [])]
        r.status = self._final_status(final)
        r.passed = r.status == "passed"
        r.patch, r.files = self.ws.patch(), self.ws.changed_files()
        r.edits = {p: self.ws.edits[p] for p in r.files}
        self._sync_usage()
        r.duration_s = round(time.monotonic() - started, 1)
        r.transcript_path = self._save()
        return r

    def _final_status(self, final: dict[str, Any]) -> Status:
        if final.get("status") in ("passed", "done"):
            return final["status"]  # type: ignore[no-any-return]
        ended = final.get("ended") or self.exhausted
        if ended == "gave_up":
            return "gave_up"
        if ended == "budget":
            return "budget"
        if ended == "error":
            return "error"
        return "failed"

    # ---------------------------------------------------------------- 节点

    async def plan_node(self, state: FixState) -> dict[str, Any]:
        msgs = [{"role": "system", "content": load_prompt(f"fix_agent_v{PROMPT_VERSION}")},
                {"role": "user", "content": self._task_message(state, "plan")}]
        submit = SUBMIT_PLAN_NOTES if self.handoff == "notes" else SUBMIT_PLAN
        tools = [*self._explore(), submit,
                 _fn("give_up", "确认无法在这份代码里修复时调用。", {"reason": {"type": "string"}},
                     ["reason"])]
        self._pins = []
        got = await self._loop("plan", msgs, tools, ("submit_plan", "give_up"), self.plan_steps)
        self._plan_msgs, self._plan_reads = msgs, list(self._phase_reads)
        if got is None:
            if self.exhausted:
                return {"ended": self.exhausted}
            # 步数用完还没交计划：不结束，带着已经读到的内容直接进入修改阶段
            return {"plan": "（规划阶段步数用完，没有交计划：请根据已读到的代码直接修改）"}
        name, args = got
        if name == "give_up":
            self.result.give_up_reason = str(args.get("reason", ""))[:500]
            return {"ended": "gave_up"}
        files = args.get("files") or []
        plan = (f"根因假设：{args.get('hypothesis', '')}\n打算改：{', '.join(map(str, files))}\n"
                f"做法：{args.get('approach', '')}")
        if self.handoff == "notes":
            self._pins = pin_ranges(args.get("key_code"))
        return {"plan": plan}

    async def edit_node(self, state: FixState) -> dict[str, Any]:
        prefix = 0
        if self.handoff == "continue" and self._plan_msgs:
            # 接着规划阶段的对话：读过的代码都还在上下文里（换了工具列表，这一次缓存会失效）
            prefix = len(self._plan_msgs)
            msgs = [*self._plan_msgs,
                    {"role": "user", "content": self._edit_instruction(state)}]
        else:
            msgs = [{"role": "system", "content": load_prompt(f"fix_agent_v{PROMPT_VERSION}")},
                    {"role": "user", "content": self._task_message(state, "edit")}]
            if self.handoff == "notes":
                msgs[-1]["content"] += "\n\n" + await self._notes()
        edit_tools = EDIT_TOOLS + ([RECALL] if self.memory is not None else [])
        got = await self._loop("edit", msgs, edit_tools, ("finish_edit",), self.edit_steps,
                               prefix=prefix)
        summary = str(got[1].get("summary", "")) if got else "（没有正常结束修改阶段）"
        att = FixAttempt(n=state["round"] + 1, summary=summary[:600], patch=self.ws.patch(),
                         hypothesis=state["plan"][:800])
        return {"attempts": [att.model_dump()]}

    async def verify_node(self, state: FixState) -> dict[str, Any]:
        last = dict(state["attempts"][-1])
        if self.task.test_code is None:
            if not self.ws.changed_files():
                return {"status": "", "ended": "no_patch"}  # 对照组没交出任何改动
            return {"status": "done"}  # 对照组：没有考卷可验，交给外部的金标准判定
        if not self.ws.changed_files():
            last["output"] = "没有任何改动。"
            return {"status": "", "attempts": [last]}
        res = await self.ws.verify_acceptance(self.task.must_pass)
        last["exit_code"], last["output"] = res.exit_code, _exec_text(res)
        ok = res.exit_code == 0 and not res.timed_out and not res.oom_killed
        return {"status": "passed" if ok else "", "attempts": [last]}

    async def reflect_node(self, state: FixState) -> dict[str, Any]:
        last = dict(state["attempts"][-1])
        msgs = [
            {"role": "system", "content": load_prompt(f"fix_agent_v{PROMPT_VERSION}")},
            {"role": "user", "content": self._task_message(state, "reflect")},
        ]
        try:
            resp = await self.llm.chat(msgs, model=self.model, json_mode=True,
                                       thinking=self.thinking)
        except LLMError as e:
            self.result.error = str(e)[:500]
            self.exhausted = "error"
            return {"ended": "error"}
        self._add(resp.usage)
        self.log.append({"phase": "reflect", "messages": msgs, "reply": resp.text})
        analysis, verdict = _parse_reflection(resp.text or "")
        last["reflection"] = analysis[:1500]
        out: dict[str, Any] = {"round": state["round"] + 1, "attempts": [last]}
        if verdict == "give_up":
            self.result.give_up_reason = analysis[:500]
            out["ended"] = "gave_up"
        return out

    # ---------------------------------------------------------------- 路由

    def _after_verify(self, state: FixState) -> str:
        if state["status"] in ("passed", "done") or state["ended"]:
            return "end"
        return "reflect" if not self.exhausted else "end"

    def _after_reflect(self, state: FixState) -> str:
        if state["ended"] or self._cost() >= self.budget_usd or state["round"] >= self.max_rounds:
            if not state["ended"] and self._cost() >= self.budget_usd:
                self.exhausted = "budget"
            return "end"
        return "plan"

    # ---------------------------------------------------------------- 提示

    def _task_message(self, state: FixState, phase: str) -> str:
        t = self.task
        parts = [f"仓库：{t.repo}" + (f"，issue #{t.number}" if t.number else ""),
                 untrusted("issue", "issue", f"{t.issue.title}\n\n{t.issue.body[:12000]}")]
        if t.reported_traceback:
            parts.append("报告里的报错堆栈：\n" + untrusted("traceback", "traceback",
                                                          t.reported_traceback))
        if t.test_code is not None:
            parts.append(
                f"验收测试（只读，已放在 `{t.test_path}`，修复前失败）。你的改动要让它通过：\n"
                f"```python\n{t.test_code}\n```\n可以用 run_tests([\"{t.test_path}\"]) 运行它。"
                "它只是判断标准的一部分：不要为了让它通过而写只对这个输入有效的特判。")
        else:
            parts.append("这次没有现成的验收测试：请根据 issue 自己判断行为是否正确，"
                         "可以用 run_python 观察，用 run_tests 跑现有的相关测试防止回归。")
        if t.feedback:
            parts.append(
                "上一版补丁已经交给独立的核验程序（ClaimVerify），被驳回了。驳回理由：\n"
                + t.feedback
                + "\n当前代码里已经带着上一版的改动（read_file 看到的就是）。请在它的基础上修改，"
                "让验收测试和下面这些测试都通过；不要回退到原来的代码再重写。")
            if t.must_pass:
                parts.append("必须一起通过的测试（验收时会在全新工作区里重跑）：\n"
                             + "\n".join(f"- {n}" for n in t.must_pass))
        if self.memory is not None and phase == "plan":
            parts.append(self._auto_recall())
        atts = [FixAttempt(**a) for a in state["attempts"]]
        if atts and phase in ("plan", "reflect"):
            parts.append("此前的尝试：\n" + "\n\n".join(self._attempt_text(a) for a in atts))
        if phase == "plan":
            first = "" if atts else "先读代码找出根因，"
            parts.append(f"现在是规划阶段：{first}然后调用 submit_plan。"
                         f"最多 {self.plan_steps} 次工具调用。")
        elif phase == "edit":
            parts.append(self._edit_instruction(state))
        else:
            parts.append(
                "现在是反思阶段。上面最后一次尝试没有通过验收。请对照它的**实际输出**分析原因，"
                "必须引用输出里的具体内容（异常、断言、行号）。只输出一个 JSON 对象："
                '{"analysis": "...", "next": "replan" 或 "give_up"}。'
                "next=replan 表示换一个思路再试；确认无法修复才用 give_up。")
        return "\n\n".join(parts)

    def _explore(self) -> list[dict[str, Any]]:
        return [*EXPLORE, RECALL] if self.memory is not None else list(EXPLORE)

    def _before(self) -> datetime:
        return self.task.memory_before or datetime.now(UTC)

    def _recall(self, query: str, path: str | None = None,
                k: int = RECALL_K) -> list[Any]:
        assert self.memory is not None
        eps = self.memory.recall(query, before=self._before(), path=path, k=k,
                                 exclude_prs=self.task.exclude_prs)
        for e in eps:
            if e.pr not in self.result.recalled_prs:
                self.result.recalled_prs.append(e.pr)
        return eps

    def _auto_recall(self) -> str:
        """规划阶段开头：按 issue 标题和正文找几条最相关的历史修改，只列标题和改了哪些函数。"""
        t = self.task
        eps = self._recall(f"{t.issue.title}\n{t.issue.body[:3000]}", k=AUTO_RECALL_K)
        listing = render_episodes(eps, with_patch=False)
        return ("这个仓库以前合并过的、可能相关的修改（维护者写的，只含这个 issue 之前合并的；"
                "不一定相关，自己判断）：\n" + listing
                + "\n需要看具体怎么改的，用 recall_fixes 查（可以加 path 只看改过某个文件的）。")

    def _edit_instruction(self, state: FixState) -> str:
        cont = "（上面规划阶段读过的代码都还在，不用重读）" if self.handoff == "continue" else ""
        patch = f"\n当前累计补丁：\n{clip(self.ws.patch(), 60, 60)}" if state["attempts"] else ""
        return (f"当前计划：\n{state['plan']}\n\n现在是修改阶段{cont}：按计划改代码、跑测试确认，"
                f"改完调用 finish_edit。最多 {self.edit_steps} 次工具调用。" + patch)

    async def _notes(self) -> str:
        """notes 交接：钉进去的代码（当前内容，含已有改动）+ 规划阶段读过的范围。"""
        parts = ["## 规划阶段的交接笔记"]
        for path, start, end in self._pins:
            r = await self.ws.read(path, start, end)
            if "error" in r:
                parts.append(f"`{path}` {start}–{end}：{r['error']}")
                continue
            parts.append(f"`{path}` 第 {start}–{end} 行（当前内容，行号只是标注，edit_file 的 old "
                         f"不要带行号）：\n```\n" + "\n".join(r["lines"]) + "\n```")
        if not self._pins:
            parts.append("（规划阶段没有指定要钉进来的代码）")
        merged = merge_ranges(self._plan_reads)
        if merged:
            parts.append("规划阶段读过的范围（需要时可以再读）：\n" + "\n".join(
                f"- {p}: " + ", ".join(f"{s}–{e}" for s, e in spans)
                for p, spans in merged.items()))
        return "\n\n".join(parts)

    @staticmethod
    def _attempt_text(a: FixAttempt) -> str:
        out = [f"第 {a.n} 轮：{a.summary or '（无小结）'}"]
        if a.patch:
            out.append("补丁：\n" + clip(a.patch, 40, 40, 3500))
        if a.output:
            out.append("验收输出：\n" + clip(a.output, 15, 30, 3500))
        if a.reflection:
            out.append("反思：" + a.reflection)
        return "\n".join(out)

    # ---------------------------------------------------------------- 工具循环

    def _cost(self) -> float:
        return priced(self.model, self.usage)

    def _add(self, usage: Usage) -> None:
        self.usage = self.usage + usage
        self._sync_usage()

    def _sync_usage(self) -> None:
        r, u = self.result, self.usage
        r.cost_usd = round(self._cost(), 6)
        r.prompt_tokens, r.completion_tokens = u.prompt_tokens, u.completion_tokens
        r.reasoning_tokens, r.cached_tokens = u.reasoning_tokens, u.cached_tokens

    async def _loop(self, phase: str, msgs: list[dict[str, Any]], tools: list[dict[str, Any]],
                    terminal: tuple[str, ...], max_steps: int,
                    prefix: int = 0) -> tuple[str, dict[str, Any]] | None:
        """有边界的工具调用循环：调到终止工具就返回它的参数；步数、预算、模型出错时返回 None。

        prefix：msgs 开头有几条是接着上一阶段的（continue 交接），transcript 里标出来，
        统计时跳过。"""
        steps = nudges = 0
        warned = False
        found: tuple[str, dict[str, Any]] | None = None
        self._phase, self._phase_reads = phase, []
        try:
            while steps < max_steps:
                if self._cost() >= self.budget_usd:
                    self.exhausted = "budget"
                    return None
                try:
                    resp = await self.llm.chat(msgs, model=self.model, tools=tools,
                                               thinking=self.thinking)
                except LLMError as e:
                    self.result.error, self.exhausted = str(e)[:500], "error"
                    return None
                self._add(resp.usage)
                msg: dict[str, Any] = {"role": "assistant", "content": resp.text or ""}
                if resp.tool_calls:
                    msg["tool_calls"] = [tc.as_message() for tc in resp.tool_calls]
                msgs.append(msg)
                if not resp.tool_calls:
                    nudges += 1
                    if nudges >= MAX_NUDGES:
                        self.result.error, self.exhausted = "模型连续不调用工具", "error"
                        return None
                    msgs.append({"role": "user", "content": "请调用一个工具继续。"})
                    continue
                nudges = 0
                for call in resp.tool_calls:
                    if found is not None:
                        content = "已结束，忽略这次调用。"
                    else:
                        steps += 1
                        self.result.steps += 1
                        counts = self.result.tool_counts
                        counts[call.name] = counts.get(call.name, 0) + 1
                        try:
                            content = await self._dispatch(call, terminal)
                        except _Stop as s:
                            found, content = (call.name, s.args_), "已记录。"
                    msgs.append({"role": "tool", "tool_call_id": call.id, "content": content})
                if found is not None:
                    return found
                left = max_steps - steps
                if 0 < left <= STEP_WARNING and not warned:
                    # 快没步数了还没结束这个阶段：提醒一次（拿到多少交多少，比什么都没有强）
                    warned = True
                    msgs[-1]["content"] += (
                        f"\n\n[提醒] 这个阶段只剩 {left} 次工具调用。请尽快调用 {terminal[0]}。")
            return None
        finally:
            self._seen += self._phase_reads
            entry: dict[str, Any] = {"phase": phase, "messages": list(msgs)}
            if prefix:
                entry["prefix"] = prefix
            self.log.append(entry)

    async def _dispatch(self, call: ToolCall, terminal: tuple[str, ...]) -> str:
        try:
            args = json.loads(call.arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError("参数必须是 JSON 对象")
        except ValueError as e:
            return f"参数不是合法的 JSON 对象：{e}"
        if call.name in terminal:
            raise _Stop(args)
        handler = getattr(self, f"_tool_{call.name}", None)
        if handler is None:
            return f"当前阶段没有这个工具：{call.name}"
        try:
            out: str = await handler(**args)
        except GuardError as e:
            if isinstance(e, Denied):
                self.result.denied += 1
            return f"被拒绝：{e}"
        except TypeError as e:
            return f"参数不对：{e}"
        except SandboxError as e:
            return f"沙箱出错：{e}"
        return out

    async def _tool_list_files(self, path: str = "") -> str:
        r = await self.ws.list_files(path)
        if "error" in r:
            return str(r["error"])
        return clip("\n".join(r["files"])) + f"\n（共 {r['total']} 个文件）"

    async def _tool_search_code(self, pattern: str, path: str = "") -> str:
        r = await self.ws.search(pattern, path)
        if "error" in r:
            return str(r["error"])
        if not r["hits"]:
            return "没有匹配。"
        return "\n".join(r["hits"]) + ("\n（结果太多，已截断）" if r["truncated"] else "")

    async def _tool_read_file(self, path: str, start: int = 1, end: int | None = None) -> str:
        r = await self.ws.read(path, int(start), int(end) if end else None)
        if "error" in r:
            return str(r["error"])
        if r["lines"]:
            s = max(int(start), 1)
            self._note_read((_norm_path(path), s, s + len(r["lines"]) - 1))
        return "\n".join(r["lines"]) + f"\n（文件共 {r['total_lines']} 行）"

    def _note_read(self, rng: Range) -> None:
        if self._phase == "edit":
            self.result.edit_reads += 1
            if overlaps(rng, self._seen):
                self.result.rereads += 1
        self._phase_reads.append(rng)

    async def _tool_edit_file(self, path: str, old: str, new: str) -> str:
        if self.result.first_edit_step is None:
            self.result.first_edit_step = self.result.steps
        return self.ws.edit(path, old, new)

    async def _tool_run_tests(self, targets: list[str]) -> str:
        if isinstance(targets, str):
            targets = [targets]
        return _exec_text(await self.ws.run_tests([str(t) for t in targets]))

    async def _tool_run_python(self, code: str) -> str:
        return _exec_text(await self.ws.run_python(code))

    async def _tool_recall_fixes(self, query: str, path: str | None = None) -> str:
        if self.memory is None:
            return "当前没有可用的历史修改记录。"
        eps = self._recall(str(query), _norm_path(path) if path else None)
        return clip(render_episodes(eps), 120, 120, 9000)

    async def _tool_reset_edits(self) -> str:
        await self.ws.revert()
        return "已撤销所有改动。"

    # ---------------------------------------------------------------- 记录

    def _save(self) -> str | None:
        d = self.artifacts_dir
        if d is None:
            return None
        d.mkdir(parents=True, exist_ok=True)
        ref = f"{self.task.repo.replace('/', '__')}-{self.task.number or 'x'}"
        path = d / f"fix-{ref}-{int(time.time())}.json"
        path.write_text(json.dumps({
            "task": self.task.model_dump(mode="json", exclude={"test_code"}),
            "result": self.result.model_dump(mode="json"),
            "phases": self.log,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)


def _parse_reflection(text: str) -> tuple[str, str]:
    """(分析, replan | give_up)。解析不了就当作 replan：宁可多试一轮，也别因为格式丢掉尝试。"""
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                nxt = "give_up" if obj.get("next") == "give_up" else "replan"
                return str(obj.get("analysis", ""))[:1500], nxt
        except ValueError:
            pass
    return text[:1500], "replan"
