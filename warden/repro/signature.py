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
# 没有 Traceback 头的纯文本输出里，只能靠类名后缀认出异常行（"AssertionError: ..."）
_EXC_LINE = re.compile(
    r"^([A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit|Interrupt))(?::\s?(.*))?$", re.M
)
# 有 Traceback 头时，异常行就是栈帧之后第一个不缩进的行，类名可以是任何名字
# （black 的 InvalidInput、NothingChanged 都不以 Error 结尾，回放里因此误判过）
_EXC_NAME = re.compile(r"^([A-Za-z_][\w.]*)(?::\s?(.*))?$")
TOP_FRAMES = 3
W_TYPE, W_FRAMES, W_MESSAGE = 0.5, 0.3, 0.2


_TB_BLOCK = re.compile(
    r"Traceback \(most recent call last\):\n"
    r"(?:[ \t].*\n|\n)*?"
    r"[A-Za-z_][\w.]*(?::[^\n]*)?",
)
_CHAIN_SEP = re.compile(
    r"\s*\n\s*(?:During handling of the above exception, another exception occurred:"
    r"|The above exception was the direct cause of the following exception:)\s*\n\s*"
)
MAX_CHAIN_CHARS = 8000


def extract_traceback_chain(text: str) -> str | None:
    """从 issue 正文里提取完整的异常链（第一个 Traceback 一直到链上最后一个异常）。

    Intake 的 extract_traceback 只取到第一个异常行，碰到异常链时拿到的是"起因"，
    而用户实际看到的最终失败在链的最后。判定器比较的是最后一段，所以复现要用完整的链。
    （Intake 的提取结果还被查重签名使用，这里不改它，避免影响 M1 的评测数据。）
    """
    text = text.replace("\r\n", "\n")
    m = _TB_BLOCK.search(text)
    if m is None:
        return None
    start, end = m.start(), m.end()
    while True:
        sep = _CHAIN_SEP.match(text, end)
        if sep is None:
            break
        nxt = _TB_BLOCK.match(text, sep.end())
        if nxt is None:
            break
        end = nxt.end()
    return text[start:end].strip()[:MAX_CHAIN_CHARS]


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
    exc = _exception_line(block)
    if exc is None and not frames:
        return None
    exc_type, message = exc or (None, "")
    return TraceSignature(
        exc_type=exc_type.rsplit(".", 1)[-1] if exc_type else None,
        message=normalize_message(message or ""),
        frames=list(dict.fromkeys(frames[-TOP_FRAMES:])),
    )


def _exception_line(block: str) -> tuple[str, str] | None:
    """最后一个 Traceback 块里的异常行 (类名, 消息)。"""
    if block.startswith(_TB_START):
        for line in block.splitlines()[1:]:
            if not line.strip() or line[0] in " \t":
                continue  # 栈帧、源码行、^^^ 标记
            m = _EXC_NAME.match(line.rstrip())
            return (m.group(1), m.group(2) or "") if m else None
        return None
    excs = _EXC_LINE.findall(block)
    return excs[-1] if excs else None


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
