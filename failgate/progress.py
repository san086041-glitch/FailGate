"""命令行实时进度（ADR 0038）：用 OpenTelemetry 的 span 事件驱动，不改引擎。

核验、修复各阶段本来就有 span（ADR 0025；修复 Agent 的阶段和工具 span 是这次加的），
CLI 只要在全局 TracerProvider 上挂一个处理器，就能在 span 开始 / 结束时更新屏幕：

- 阶段 span（`verify layer1`、`fix plan` …）→ 一行一个阶段，进行中转圈、结束打勾和耗时；
- `chat …`（LLM 调用）→ 次数、token、花费（span 结束时的 failgate.cost_usd）；
- `sandbox …`（容器执行）→ 次数和正在跑的那个；
- `fix tool …` → 修复 Agent 的步数和最近一次工具调用。

输出不是终端时不用 Live 刷新，阶段结束时打一行文字（日志、CI 里也能看）。
OTel 的全局 provider 只能设一次：装一个转发处理器，之后每个命令注册 / 注销自己的监听者
（交互模式里连着跑几条命令也不会叠加）。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from failgate.i18n import t

STAGES = {  # span 名 → (中文, 英文)
    "verify prepare base": ("准备修复前的环境（合并基点）", "prepare the base environment"),
    "verify prepare head": ("准备修复后的环境（PR）", "prepare the PR environment"),
    "verify layer1": ("① 考卷：修复前失败、修复后通过", "① exam: fails before, passes after"),
    "verify layer3": ("③ 相关测试：有没有新增失败", "③ related tests: any new failures"),
    "verify hidden exam": ("隐藏考卷", "hidden exam"),
    "verify strength": ("考卷强度（变异测试）", "exam strength (mutation testing)"),
    "fix plan": ("规划", "plan"),
    "fix edit": ("修改", "edit"),
    "fix verify": ("验收：全新工作区里跑考卷", "accept: run the exam in a fresh workspace"),
    "fix reflect": ("反思", "reflect"),
}


@dataclass
class Stage:
    label: str
    started: float
    ended: float | None = None
    ok: bool = True


@dataclass
class Tracker:
    """收 span 事件、渲染成进度面板。span 回调在事件循环线程里，渲染在 Live 的线程里。"""

    title: str
    started: float = field(default_factory=time.monotonic)
    stages: dict[int, Stage] = field(default_factory=dict)  # span_id → 阶段（保持开始顺序）
    llm_calls: int = 0
    llm_running: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    sandbox_runs: int = 0
    sandbox_now: str = ""
    steps: int = 0
    last_tool: str = ""
    plain: Console | None = None  # 不是终端时：阶段结束打一行
    lock: threading.Lock = field(default_factory=threading.Lock)

    # ---- span 事件
    def on_start(self, span: Span) -> None:
        name, attrs = span.name, dict(span.attributes or {})
        with self.lock:
            if name in STAGES:
                label = t(*STAGES[name])
                rnd = attrs.get("failgate.fix.round")
                if isinstance(rnd, int) and rnd > 1:
                    label += t(f"（第 {rnd} 轮）", f" (round {rnd})")
                self.stages[span.context.span_id] = Stage(label, time.monotonic())
            elif name.startswith("chat"):
                self.llm_running += 1
            elif name.startswith("sandbox "):
                self.sandbox_now = str(attrs.get("failgate.sandbox.argv") or name)[:60]
            elif name.startswith("fix tool "):
                self.steps += 1
                target = attrs.get("failgate.fix.target") or ""
                self.last_tool = f"{name[9:]} {target}".strip()[:70]

    def on_end(self, span: ReadableSpan) -> None:
        name, attrs = span.name, dict(span.attributes or {})
        ok = span.status.is_ok
        line: str | None = None
        with self.lock:
            ctx = span.context
            stage = self.stages.get(ctx.span_id) if ctx is not None else None
            if stage is not None:
                stage.ended, stage.ok = time.monotonic(), ok
                line = f"{'✓' if ok else '✗'} {stage.label}  {stage.ended - stage.started:.1f}s"
            elif name.startswith("chat"):
                self.llm_running = max(0, self.llm_running - 1)
                self.llm_calls += 1
                self.tokens_in += int(attrs.get("gen_ai.usage.input_tokens") or 0)
                self.tokens_out += int(attrs.get("gen_ai.usage.output_tokens") or 0)
                self.cost += float(attrs.get("failgate.cost_usd") or 0.0)
            elif name.startswith("sandbox "):
                self.sandbox_runs += 1
                self.sandbox_now = ""
        if line and self.plain is not None:
            self.plain.print(line)

    # ---- 渲染
    def __rich__(self) -> RenderableType:
        with self.lock:
            now = time.monotonic()
            head = Text(f"{self.title}", style="bold")
            head.append(t("  · 已用 ", "  · elapsed ") + _clock(now - self.started), style="dim")
            rows = Table.grid(padding=(0, 1))
            rows.add_column(width=1)
            rows.add_column()
            rows.add_column(justify="right", style="dim")
            for st in self.stages.values():
                if st.ended is None:
                    rows.add_row(Spinner("dots", style="cyan"), st.label,
                                 f"{now - st.started:.1f}s")
                else:
                    rows.add_row(Text("✓" if st.ok else "✗", style="green" if st.ok else "red"),
                                 Text(st.label, style="dim" if st.ok else "red"),
                                 f"{st.ended - st.started:.1f}s")
            parts: list[RenderableType] = [head, rows]
            stats = Text(style="dim")
            if self.llm_calls or self.llm_running:
                running = (t(f"（进行中 {self.llm_running}）", f" ({self.llm_running} running)")
                           if self.llm_running else "")
                stats.append(t(f"LLM {self.llm_calls} 次", f"LLM {self.llm_calls} calls") + running
                             + t(f" · 输入 {self.tokens_in:,} / 输出 {self.tokens_out:,} token",
                                 f" · in {self.tokens_in:,} / out {self.tokens_out:,} tokens")
                             + f" · ${self.cost:.4f}   ")
            if self.sandbox_runs or self.sandbox_now:
                stats.append(t(f"容器 {self.sandbox_runs} 次", f"containers {self.sandbox_runs}"))
                if self.sandbox_now:
                    stats.append(t(f"（正在跑：{self.sandbox_now}）",
                                   f" (running: {self.sandbox_now})"))
            if stats.plain:
                parts.append(stats)
            if self.steps:
                parts.append(Text(t(f"第 {self.steps} 步 · 最近：{self.last_tool}",
                                    f"step {self.steps} · last: {self.last_tool}"), style="dim"))
            return Group(*parts)


def _clock(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


class _Relay(SpanProcessor):
    """挂在全局 provider 上的唯一处理器，把事件转给当前注册的监听者。"""

    def __init__(self) -> None:
        self.listeners: list[Tracker] = []

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        for listener in list(self.listeners):
            listener.on_start(span)

    def on_end(self, span: ReadableSpan) -> None:
        for listener in list(self.listeners):
            listener.on_end(span)

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


_RELAY: _Relay | None = None


def relay() -> _Relay:
    """全局 provider 上的转发处理器（没有 provider 就装一个只做进度、不导出的）。"""
    global _RELAY
    if _RELAY is None:
        provider: Any = trace.get_tracer_provider()
        if not isinstance(provider, TracerProvider):
            mine = TracerProvider()
            mine.failgate_progress_only = True  # type: ignore[attr-defined]  # tracing.setup 认这个
            trace.set_tracer_provider(mine)
            provider = trace.get_tracer_provider()  # 别人抢先设过就用别人的
        _RELAY = _Relay()
        if isinstance(provider, TracerProvider):
            provider.add_span_processor(_RELAY)
    return _RELAY


@contextmanager
def live_progress(title: str, console: Console) -> Iterator[Tracker]:
    """在 with 块里跑的核验 / 修复会实时显示进度；块结束时面板定格在最后的状态。"""
    tracker = Tracker(title)
    hub = relay()
    hub.listeners.append(tracker)
    try:
        if console.is_terminal:
            with Live(tracker, console=console, refresh_per_second=8, transient=False):
                yield tracker
        else:
            tracker.plain = console
            console.print(title)
            yield tracker
    finally:
        hub.listeners.remove(tracker)


def progress_console() -> Console:
    """进度画在 stderr：stdout 留给结果（verify > report.md 时进度照样在终端里看得到）。"""
    return Console(stderr=True, highlight=False)
