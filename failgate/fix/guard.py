"""修复 Agent 的写权限（差异点 E：按权限隔离，不靠提示词）。

Agent 只能改"被测代码"，不能碰考卷和测试环境：
  * 任何测试文件、tests / test / testing 目录、conftest.py；
  * pytest 配置（pytest.ini / tox.ini / setup.cfg / pyproject.toml）和 CI、依赖清单等仓库元数据；
  * 验收测试自己的路径；
  * 以 . 开头的路径段（.github、.git、.failgate 草稿目录）、sitecustomize / .pth 这类
    启动即执行的文件。
路径先归一化再比较，大小写不敏感，拒绝绝对路径、反斜杠和 ..。

这是第一道防线；第二道是最终验收在全新工作区里只应用宿主机上的改动清单（workspace.py），
第三道是 ClaimVerify 第二层（防篡改）对 PR 的检查。
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterable

from failgate.verify.tamper import PYTEST_CONFIGS, is_test_file

TEST_DIRS = frozenset({"tests", "test", "testing"})
META_NAMES = frozenset({
    "pyproject.toml", "noxfile.py", "tox.ini", "setup.cfg", "pytest.ini", "conftest.py",
    "sitecustomize.py", "usercustomize.py", "requirements.txt", "manifest.in",
    "mypy.ini", "ruff.toml", ".coveragerc",
}) | PYTEST_CONFIGS
ALLOWED_SUFFIXES = (".py", ".pyi", ".txt", ".md", ".rst", ".json")
MAX_FILES = 6
MAX_CHANGED_LINES = 300
MAX_FILE_BYTES = 300_000


class GuardError(ValueError):
    """这次写入被工具层拒绝；消息原样反馈给模型。"""


class Denied(GuardError):
    """写权限被拒（路径不允许）。角色隔离实验统计这一类，不含"old 没找到"之类的操作失误。"""


class WriteGuard:
    def __init__(self, protected: Iterable[str] = (), *, max_files: int = MAX_FILES,
                 max_lines: int = MAX_CHANGED_LINES) -> None:
        self.protected = frozenset(self._norm(p) for p in protected if p)
        self.max_files = max_files
        self.max_lines = max_lines

    @staticmethod
    def _norm(path: str) -> str:
        return posixpath.normpath(path.replace("\\", "/")).lower()

    def check(self, path: str) -> str:
        """通过时返回归一化后的路径（保留原大小写），否则抛 GuardError。"""
        raw = path.strip()
        if not raw or "\x00" in raw:
            raise Denied("路径为空或含非法字符。")
        if "\\" in raw or raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
            raise Denied("路径必须是相对仓库根的正斜杠路径。")
        parts = raw.split("/")
        if ".." in parts:
            raise Denied("路径不能含 ..。")
        norm = posixpath.normpath(raw)
        segs = norm.split("/")
        low = [s.lower() for s in segs]
        if any(s.startswith(".") for s in low):
            raise Denied(f"不能改隐藏路径（{norm}）：仓库元数据和 CI 配置不在修复范围内。")
        if self._norm(norm) in self.protected:
            raise Denied(f"{norm} 是验收测试，只读。")
        if any(s in TEST_DIRS for s in low[:-1]) or is_test_file(norm) or low[-1] in META_NAMES:
            raise Denied(
                f"不能改 {norm}：测试文件、conftest、pytest 配置和项目元数据只读。"
                "请修改被测的源代码，让现有测试通过。"
            )
        if low[-1].endswith(".pth") or not low[-1].endswith(ALLOWED_SUFFIXES):
            raise Denied(f"不支持改这种文件（{norm}）；只能改 {'/'.join(ALLOWED_SUFFIXES)}。")
        return norm

    def check_size(self, path: str, content: str) -> None:
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise GuardError(f"{path} 太大（上限 {MAX_FILE_BYTES} 字节）。")

    def check_totals(self, files: int, lines: int) -> None:
        if files > self.max_files:
            raise GuardError(f"这次会改 {files} 个文件，上限 {self.max_files}。请缩小改动范围。")
        if lines > self.max_lines:
            raise GuardError(f"这次改动共 {lines} 行，上限 {self.max_lines}。请缩小改动范围。")
