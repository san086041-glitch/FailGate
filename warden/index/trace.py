"""报错堆栈签名：把"同一个 bug"在不同机器上产生的堆栈归一成可比较的形状。

同一个 bug 在不同用户那里，堆栈的绝对路径、行号、内存地址、具体数值都会不同，
但"异常类型 + 经过的模块和函数"基本一致。签名只保留这部分：

    File "/home/u/.venv/lib/python3.12/site-packages/pandas/io/parquet.py", line 667, in read
    → pandas/io/parquet.py:read

相似度 = 0.5 × [异常类型相同] + 0.5 × Jaccard(栈帧集合)
"""

from __future__ import annotations

import re

from pydantic import BaseModel

_PY_FRAME = re.compile(r'File "([^"]+)", line \d+, in ([\w<>.]+)')
_JS_FRAME = re.compile(r"at (?:([\w$.<>]+) )?\(?([^():\s]+):\d+:\d+\)?")
_EXC_LINE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit))\b[:\s]?(.*)$", re.M)
_ROOT_MARKERS = ("site-packages/", "dist-packages/", "node_modules/", "/src/", "/lib/")


class TraceSignature(BaseModel):
    exc_type: str | None = None
    message: str = ""
    frames: list[str] = []


def _short_path(path: str) -> str:
    path = path.replace("\\", "/")
    for marker in _ROOT_MARKERS:
        if marker in path:
            return path.rsplit(marker, 1)[1]
    parts = [p for p in path.split("/") if p]
    return "/".join(parts[-2:])


def normalize_message(msg: str) -> str:
    msg = re.sub(r"0x[0-9a-fA-F]+", "<ADDR>", msg)
    msg = re.sub(r"(?:[A-Za-z]:)?[/\\][^\s'\"]+", "<PATH>", msg)
    msg = re.sub(r"\d+", "<N>", msg)
    return msg.strip()[:200]


def signature(traceback: str | None) -> TraceSignature | None:
    if not traceback:
        return None
    frames = [f"{_short_path(p)}:{fn}" for p, fn in _PY_FRAME.findall(traceback)]
    if not frames:
        frames = [f"{_short_path(p)}:{fn or '?'}" for fn, p in _JS_FRAME.findall(traceback)]
    excs = _EXC_LINE.findall(traceback)
    exc_type, message = (excs[-1] if excs else (None, ""))
    if not frames and exc_type is None:
        return None
    # 去掉用户自己的脚本入口（repro.py:<module>），它每个人都不一样
    frames = [f for f in frames if not f.endswith(":<module>")]
    return TraceSignature(
        exc_type=exc_type.rsplit(".", 1)[-1] if exc_type else None,
        message=normalize_message(message),
        frames=list(dict.fromkeys(frames)),
    )


def similarity(a: TraceSignature | None, b: TraceSignature | None) -> float:
    if a is None or b is None:
        return 0.0
    type_score = 1.0 if a.exc_type and a.exc_type == b.exc_type else 0.0
    fa, fb = set(a.frames), set(b.frames)
    frame_score = len(fa & fb) / len(fa | fb) if (fa or fb) else 0.0
    return 0.5 * type_score + 0.5 * frame_score
