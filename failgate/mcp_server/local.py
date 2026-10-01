"""本地 git 仓库 → 源码包（SourceTree）和改动清单（ADR 0033）。

MCP 进程本身不执行仓库里的任何代码：这里只调 git 读文件，代码一律在沙箱里跑。

- 工作区：`git ls-files -co --exclude-standard` 列出已跟踪 + 未跟踪但没被忽略的文件，
  所以未提交的修改、新加的文件都算进去；.gitignore 里的（虚拟环境、构建产物）不算；
- 某个提交：`git archive`，和 GitHub 的源码包一样只有一个顶层目录；
- 改动清单：直接比较两个源码包里的文件内容（未跟踪的新文件也能比到），生成和 GitHub PR
  一样的 PullFile（状态 + unified diff），交给 ClaimVerify 的防篡改检查。
"""

from __future__ import annotations

import difflib
import hashlib
import io
import subprocess
import tarfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from failgate.repro.source import SourceTree
from failgate.verify.tamper import PullFile

TOP = "src"
MAX_TARBALL_BYTES = 50 * 1024 * 1024  # 和 GitHub 源码包同一个上限
MAX_FILE_BYTES = 5 * 1024 * 1024
GIT_TIMEOUT_S = 60


class LocalRepoError(RuntimeError):
    """不是 git 仓库、提交不存在、仓库太大……"""


def _git(root: Path, *args: str) -> bytes:
    try:
        res = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                             timeout=GIT_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise LocalRepoError(f"git {' '.join(args[:2])} 失败：{e}") from e
    if res.returncode != 0:
        msg = res.stderr.decode("utf-8", errors="replace").strip()
        raise LocalRepoError(f"git {' '.join(args[:2])} 失败：{msg[:300]}")
    return res.stdout


def repo_root(path: str | Path) -> Path:
    p = Path(path).expanduser()
    if not p.is_dir():
        raise LocalRepoError(f"{p} 不是目录")
    out = _git(p, "rev-parse", "--show-toplevel").decode("utf-8").strip()
    return Path(out)


def resolve(root: Path, ref: str) -> tuple[str, datetime]:
    """提交号和提交时间。"""
    out = _git(root, "log", "-1", "--format=%H %cI", ref, "--").decode("utf-8").split()
    if len(out) != 2:
        raise LocalRepoError(f"找不到提交 {ref}")
    return out[0], datetime.fromisoformat(out[1]).astimezone(UTC)


def is_dirty(root: Path) -> bool:
    return bool(_git(root, "status", "--porcelain", "--untracked-files=normal").strip())


def worktree_files(root: Path) -> list[str]:
    raw = _git(root, "ls-files", "-co", "--exclude-standard", "-z")
    names = [n for n in raw.decode("utf-8", errors="replace").split("\0") if n]
    return sorted(n for n in dict.fromkeys(names) if (root / n).is_file())


def worktree_tree(root: Path, label_repo: str | None = None) -> SourceTree:
    """当前工作区（含未提交的修改）。

    sha 用内容摘要（报告里截前 7 位显示），内容不变环境缓存就能复用。"""
    files = worktree_files(root)
    buf = io.BytesIO()
    digest = hashlib.sha256()
    total = 0
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in files:
            p = root / rel
            size = p.stat().st_size
            if size > MAX_FILE_BYTES:
                continue
            total += size
            if total > MAX_TARBALL_BYTES:
                raise LocalRepoError(f"工作区超过 {MAX_TARBALL_BYTES // 2**20} MB")
            data = p.read_bytes()
            digest.update(rel.encode("utf-8") + b"\0" + data + b"\0")
            info = tarfile.TarInfo(f"{TOP}/{rel}")
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))
    _, head_at = resolve(root, "HEAD")
    return SourceTree(repo=label_repo or f"local:{root.name}",
                      sha=f"{digest.hexdigest()[:12]}-worktree", committed_at=head_at,
                      tarball=buf.getvalue())


def ref_tree(root: Path, ref: str, label_repo: str | None = None) -> SourceTree:
    sha, at = resolve(root, ref)
    data = _git(root, "archive", "--format=tar.gz", f"--prefix={TOP}/", sha)
    if len(data) > MAX_TARBALL_BYTES:
        raise LocalRepoError(f"{ref} 的源码包超过 {MAX_TARBALL_BYTES // 2**20} MB")
    return SourceTree(repo=label_repo or f"local:{root.name}", sha=sha, committed_at=at,
                      tarball=data)


def _all_files(tree: SourceTree) -> dict[str, str]:
    return tree.read_files(lambda _: True, max_bytes=MAX_FILE_BYTES, limit=100_000)


def _norm_eol(text: str) -> str:
    return text.replace("\r\n", "\n")


def tree_diff(base: SourceTree, head: SourceTree) -> list[PullFile]:
    """两个源码包之间改了哪些文件（和 GitHub PR 的 files 同一个结构）。

    比较前统一换行：Windows 上 git 的 autocrlf 让工作区是 CRLF，而 git archive 给的是仓库里的 LF，
    不统一的话所有文件都会被当成改过（考卷哈希本来就做了换行归一，见 receipt.normalize_code）。"""
    a = {p: _norm_eol(t) for p, t in _all_files(base).items()}
    b = {p: _norm_eol(t) for p, t in _all_files(head).items()}
    out: list[PullFile] = []
    for path in sorted(set(a) | set(b)):
        old, new = a.get(path), b.get(path)
        if old == new:
            continue
        status = "added" if old is None else "removed" if new is None else "modified"
        patch = "".join(difflib.unified_diff(
            (old or "").splitlines(keepends=True), (new or "").splitlines(keepends=True),
            fromfile=f"a/{path}", tofile=f"b/{path}", n=3))
        out.append(PullFile(filename=path, status=status, patch=_hunks_only(patch)))
    return out


def _hunks_only(patch: str) -> str:
    """GitHub 的 patch 字段不带 ---/+++ 头，只有 @@ 开始的块。"""
    lines = patch.splitlines(keepends=True)
    return "".join(lines[2:]) if len(lines) >= 2 and lines[0].startswith("---") else patch


def changed_paths(files: Sequence[PullFile]) -> list[str]:
    return [f.filename for f in files]
