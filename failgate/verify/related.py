"""第三层：挑出和 PR 改动相关的已有测试，以及解析 pytest 的结果摘要（技术方案 9.2 节）。

挑选只做静态分析，按相关程度排序后取前 N 个文件：
1. 同名：改了 `pkg/text.py`，就选 `test_text.py`；
2. 直接 import 了改动的模块：`import pkg.text`、`from pkg.text import …`、`from pkg import text`；
3. import 了改动模块所在的包：`from pkg import slugify`（包的 __init__ 往往会再导出）。
考卷文件本身不选，它由第一层负责。没有覆盖率映射，所以会漏掉"通过别的模块间接依赖"的测试。
"""

from __future__ import annotations

import ast
import re

from .tamper import PullFile, is_test_file

MAX_FILES = 10
SRC_LAYOUT = "src/"


def module_of(path: str) -> str | None:
    """仓库内的 .py 路径 → 模块名：`pkg/text.py` → `pkg.text`，src 布局去掉 `src/`。"""
    if not path.endswith(".py"):
        return None
    if path.startswith(SRC_LAYOUT):
        path = path[len(SRC_LAYOUT):]
    parts = path[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts or not all(p.isidentifier() for p in parts):
        return None
    return ".".join(parts)


def changed_modules(files: list[PullFile]) -> list[str]:
    mods: list[str] = []
    for f in files:
        for path in (f.filename, f.previous_filename):
            if not path or is_test_file(path) or "/tests/" in f"/{path}":
                continue
            m = module_of(path)
            if m and m not in mods:
                mods.append(m)
    return mods


def _imports(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
    return names


def select_related_tests(
    files: list[PullFile], tests: dict[str, str], *, exclude: str, limit: int = MAX_FILES
) -> list[str]:
    """tests：head 上的测试文件 {路径: 源码}。返回按相关程度排好的测试文件路径。"""
    mods = changed_modules(files)
    if not mods:
        return []
    stems = {m.rsplit(".", 1)[-1] for m in mods}
    packages = {m.rsplit(".", 1)[0] for m in mods if "." in m} | {m for m in mods if "." not in m}
    ranked: list[tuple[int, str]] = []
    for path, source in tests.items():
        if path == exclude or path.rsplit("/", 1)[-1] == "conftest.py":
            continue
        name = path.rsplit("/", 1)[-1][:-3]
        imported = _imports(source)
        if name.removeprefix("test_").removesuffix("_test") in stems:
            rank = 0
        elif any(i == m or i.startswith(f"{m}.") for i in imported for m in mods):
            rank = 1
        elif imported & packages:
            rank = 2
        else:
            continue
        ranked.append((rank, path))
    return [p for _, p in sorted(ranked)][:limit]


# ---------------------------------------------------------------- pytest 的 -rA / -rfE 摘要

_SUMMARY = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(?:\[\d+\]\s+)?(\S+)", re.M)


def _node(raw: str, prefix: str) -> str:
    raw = raw.removeprefix(prefix)
    # SKIPPED 的格式是 "path:行号: 原因"，只留路径
    if "::" not in raw and re.search(r"\.py:\d+:?$", raw):
        raw = raw.rsplit(":", 2)[0] if raw.endswith(":") else raw.rsplit(":", 1)[0]
    return raw


def outcomes(output: str, *, prefix: str = "src/") -> dict[str, str]:
    """pytest 结果摘要 → {节点 ID: 结果}。--rootdir=src 时节点 ID 已经不带 src/，这里两种都认。"""
    return {_node(m.group(2), prefix): m.group(1) for m in _SUMMARY.finditer(output)}


def failed_nodes(output: str, *, prefix: str = "src/") -> set[str]:
    return {n for n, s in outcomes(output, prefix=prefix).items() if s in ("FAILED", "ERROR")}
