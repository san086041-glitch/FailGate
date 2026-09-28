"""第二层：PR 有没有动考卷、测试配置（技术方案 9.2 节）。

只看 PR 的改动文件（GitHub 的 PR 文件接口）和 head 上考卷文件的内容，不执行任何代码。

- 高危（直接驳回）：考卷文件被删除、改名，或者内容和封存的哈希不一致（换行归一后）；
- 中危（只标注，交给维护者）：改 conftest.py、pytest 配置文件，给别的测试加 skip / xfail，
  删除别的测试文件。这些修改大多是正常的；真把考卷跳过了，第一层的运行结果会发现。

运行时用的永远是封存的考卷，所以这里的"考卷被改"只是篡改信号，不影响第一层的结果。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

from .receipt import code_sha256

Level = Literal["high", "medium"]

PYTEST_CONFIGS = frozenset({"pytest.ini", "tox.ini", "setup.cfg"})
_SKIP = re.compile(
    r"pytest\.mark\.(?:skip|skipif|xfail)\b|pytest\.(?:skip|xfail|importorskip)\(|unittest\.skip"
)


class PullFile(BaseModel):
    filename: str
    status: str  # added / removed / modified / renamed / copied / changed / unchanged
    previous_filename: str | None = None
    patch: str | None = None  # 大文件 GitHub 不给 patch


class Signal(BaseModel):
    level: Level
    kind: str
    path: str
    detail: str


def is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py")
                                     or name == "conftest.py")


def _added_lines(patch: str | None) -> list[str]:
    if not patch:
        return []
    return [ln[1:] for ln in patch.splitlines() if ln.startswith("+") and not ln.startswith("+++")]


def _changed_lines(patch: str | None) -> list[str]:
    if not patch:
        return []
    return [ln[1:] for ln in patch.splitlines()
            if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]


def tamper_signals(
    files: list[PullFile], *, test_path: str, sealed_sha256: str, head_code: str | None
) -> list[Signal]:
    """head_code：head 上考卷文件的内容（没有这个文件为 None）。"""
    out: list[Signal] = []
    touched = False
    for f in files:
        if f.filename == test_path or f.previous_filename == test_path:
            touched = True
            if f.status == "removed":
                out.append(Signal(level="high", kind="exam_removed", path=test_path,
                                  detail="PR 删除了封存的考卷"))
            elif f.status == "renamed" and f.filename != test_path:
                out.append(Signal(level="high", kind="exam_renamed", path=test_path,
                                  detail=f"PR 把考卷改名为 {f.filename}"))
            continue
        name = f.filename.rsplit("/", 1)[-1]
        if name == "conftest.py":
            out.append(Signal(level="medium", kind="conftest", path=f.filename,
                              detail="改动了 conftest.py（可能影响测试的收集和运行）"))
        elif name in PYTEST_CONFIGS or (
            name == "pyproject.toml" and any("pytest" in ln for ln in _changed_lines(f.patch))
        ):
            out.append(Signal(level="medium", kind="pytest_config", path=f.filename,
                              detail="改动了 pytest 配置"))
        elif is_test_file(f.filename):
            if f.status == "removed":
                out.append(Signal(level="medium", kind="test_removed", path=f.filename,
                                  detail="删除了测试文件"))
            elif any(_SKIP.search(ln) for ln in _added_lines(f.patch)):
                out.append(Signal(level="medium", kind="skip_added", path=f.filename,
                                  detail="给测试加了 skip / xfail"))
    # PR 把考卷原样加进仓库是好事；内容和封存的不一样才是篡改（换行符不算）
    if head_code is not None and code_sha256(head_code) != sealed_sha256:
        out.append(Signal(level="high", kind="exam_modified", path=test_path,
                          detail="head 上的考卷和封存的版本不一致"))
    elif head_code is None and touched and not any(s.kind.startswith("exam_") for s in out):
        out.append(Signal(level="high", kind="exam_removed", path=test_path,
                          detail="PR 改动了考卷，head 上已经没有这个文件"))
    return out
