"""复现 Agent 的只读代码工具：在环境镜像里（断网、只读）列出、搜索、读取被测包的源码。

为什么在沙箱里做而不是把源码拷到宿主机：包是从 PyPI 装的外部代码，宿主机上不落任何
不可信文件；而且沙箱里看到的就是实际运行的那份代码（包括 PyPI 包里带的其他顶层包，
比如 black 带的 blib2to3）。

辅助程序只用 importlib.util.find_spec 定位包，不 import 它：import 会执行包的代码。
路径一律相对 site-packages，读文件时校验解析后的真实路径仍在 site-packages 之内。
source 模式（L2）传一个目录（仓库源码树），路径改为相对这个目录，同样做越界校验。
"""

from __future__ import annotations

import json
from typing import Any

from failgate.repro.sandbox import DockerSandbox

HELPER = r'''
import importlib.util, json, os, re, sys

def out(obj):
    print("FAILGATE_TOOL" + json.dumps(obj, ensure_ascii=False))
    sys.exit(0)

mode, module = sys.argv[1], sys.argv[2]
if module.startswith("/"):
    # 目录模式（source 模式的仓库源码树）：路径相对这个目录
    if not os.path.isdir(module):
        out({"error": f"目录不存在：{module}"})
    pkg = base = module
else:
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError) as e:
        out({"error": f"找不到模块 {module}: {e}"})
    if spec is None or spec.origin is None and not spec.submodule_search_locations:
        out({"error": f"找不到模块 {module}"})
    if spec.submodule_search_locations:
        pkg = list(spec.submodule_search_locations)[0]
        base = os.path.dirname(pkg)
    else:
        pkg = spec.origin
        base = os.path.dirname(pkg)
base_real = os.path.realpath(base)

def inside(rel):
    p = os.path.realpath(os.path.join(base, rel))
    if p != base_real and not p.startswith(base_real + os.sep):
        out({"error": "路径必须在代码目录之内"})
    return p

def walk(root):
    if os.path.isfile(root):
        yield root
        return
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x != "__pycache__")
        for f in sorted(files):
            if f.endswith((".py", ".pyi")):
                yield os.path.join(d, f)

if mode == "list":
    root = inside(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] else pkg
    files = [os.path.relpath(p, base) for p in walk(root)]
    out({"files": files[:400], "total": len(files)})
elif mode == "search":
    pattern, scope, limit = sys.argv[3], sys.argv[4], int(sys.argv[5])
    root = inside(scope) if scope else pkg
    try:
        rx = re.compile(pattern)
    except re.error:
        rx = re.compile(re.escape(pattern))
    hits = []
    for p in walk(root):
        try:
            lines = open(p, encoding="utf-8", errors="replace").read().splitlines()
        except OSError:
            continue
        rel = os.path.relpath(p, base)
        for i, line in enumerate(lines, 1):
            if rx.search(line):
                hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                if len(hits) >= limit:
                    out({"hits": hits, "truncated": True})
    out({"hits": hits, "truncated": False})
elif mode == "read":
    p = inside(sys.argv[3])
    start, end = int(sys.argv[4]), int(sys.argv[5])
    try:
        lines = open(p, encoding="utf-8", errors="replace").read().splitlines()
    except OSError as e:
        out({"error": f"读不了 {sys.argv[3]}: {e}"})
    start = max(start, 1)
    chunk = [f"{i:>5}  {lines[i - 1]}" for i in range(start, min(end, len(lines)) + 1)]
    out({"path": sys.argv[3], "lines": chunk, "total_lines": len(lines)})
'''

SEARCH_LIMIT = 60
READ_MAX_LINES = 250


class CodeTools:
    """一个环境镜像上的只读代码工具。每次调用起一个断网的 run 阶段容器。"""

    def __init__(self, sandbox: DockerSandbox, image: str, volume: str, module: str) -> None:
        """module：import 名（在 site-packages 里找包），或以 / 开头的目录（仓库源码树）。"""
        self.sandbox = sandbox
        self.image = image
        self.volume = volume
        self.module = module

    async def _call(self, *args: str) -> dict[str, Any]:
        res = await self.sandbox.run(
            self.image, self.volume, ["python", "-c", HELPER, *args], timeout_s=60
        )
        line = next(
            (ln for ln in reversed(res.stdout.splitlines()) if ln.startswith("FAILGATE_TOOL")), None
        )
        if line is None:
            return {"error": f"工具执行失败（exit={res.exit_code}）：{res.output_tail(8)}"}
        result: dict[str, Any] = json.loads(line[len("FAILGATE_TOOL"):])
        return result

    async def list_files(self, path: str = "") -> dict[str, Any]:
        return await self._call("list", self.module, path)

    async def search(self, pattern: str, path: str = "") -> dict[str, Any]:
        return await self._call("search", self.module, pattern, path, str(SEARCH_LIMIT))

    async def read(self, path: str, start: int = 1, end: int | None = None) -> dict[str, Any]:
        start = max(int(start), 1)
        end = min(int(end or start + 150), start + READ_MAX_LINES - 1)
        return await self._call("read", self.module, path, str(start), str(end))
