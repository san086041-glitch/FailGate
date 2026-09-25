"""复现判定用的失败签名（技术方案 8.6 节）。

和查重用的 index/trace.py 的区别：
- 只看最后一段异常链（"During handling of the above exception…" 之前的是起因，不是最终失败）；
- 栈帧只保留属于目标包的，按包名前缀截路径，所以同一个文件不论装在 site-packages、
  /workspace/src 还是用户家目录下都得到同一个名字；用户脚本、标准库、第三方库的帧全部丢掉；
- 只取离抛出点最近的 3 层帧，越深的帧越能说明"在哪儿坏的"；
- 相似度三项加权：类型相同 0.5 + 栈帧 Jaccard × 0.3 + 消息相似度 × 0.2。
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from warden.index.trace import TraceSignature, normalize_message

_TB_START = "Traceback (most recent call last):"
_PY_FRAME = re.compile(r'File "([^"]+)", line \d+, in ([\w<>.]+)')
_EXC_LINE = re.compile(
    r"^([A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit|Interrupt))(?::\s?(.*))?$", re.M
)
TOP_FRAMES = 3
W_TYPE, W_FRAMES, W_MESSAGE = 0.5, 0.3, 0.2


def last_traceback(text: str) -> str:
    """只保留最后一个 Traceback 块（含异常行）；没有 Traceback 头时原样返回。"""
    idx = text.rfind(_TB_START)
    return text[idx:] if idx >= 0 else text


def package_path(path: str, package: str) -> str | None:
    """把路径截成以包名开头的形式；不属于该包返回 None。

    /usr/lib/python3.12/site-packages/black/linegen.py → black/linegen.py
    /workspace/src/black/__init__.py                   → black/__init__.py
    """
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    pkg = package.replace("-", "_").lower()
    for i, part in enumerate(parts):
        if part.lower() == pkg or part.lower() == f"{pkg}.py":
            return "/".join(parts[i:])
    return None


def failure_signature(text: str | None, package: str | None) -> TraceSignature | None:
    if not text:
        return None
    block = last_traceback(text)
    frames: list[str] = []
    for path, fn in _PY_FRAME.findall(block):
        short = package_path(path, package) if package else path.replace("\\", "/")
        if short is not None and fn != "<module>":
            frames.append(f"{short}:{fn}")
    excs = _EXC_LINE.findall(block)
    if not excs and not frames:
        return None
    exc_type, message = excs[-1] if excs else (None, "")
    return TraceSignature(
        exc_type=exc_type.rsplit(".", 1)[-1] if exc_type else None,
        message=normalize_message(message or ""),
        frames=list(dict.fromkeys(frames[-TOP_FRAMES:])),
    )


def match_score(observed: TraceSignature | None, reported: TraceSignature | None) -> float:
    if observed is None or reported is None:
        return 0.0
    type_score = 1.0 if observed.exc_type and observed.exc_type == reported.exc_type else 0.0
    fo, fr = set(observed.frames), set(reported.frames)
    if fo or fr:
        frame_score = len(fo & fr) / len(fo | fr)
    else:
        # 两边都没有包内帧（比如直接在 C 扩展里抛出）：这一项不扣分，由类型和消息决定
        frame_score = type_score
    msg_score = (
        SequenceMatcher(None, observed.message, reported.message).ratio()
        if observed.message or reported.message
        else type_score
    )
    return round(W_TYPE * type_score + W_FRAMES * frame_score + W_MESSAGE * msg_score, 4)
