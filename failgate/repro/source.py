"""source 模式的环境：从某个提交的源码构建（技术方案 8.2 节）。

    GitHub /tarball/<sha>  ──宿主机只下载（有大小上限），不解压──┐
    fixture 目录            ──pack_dir 打包──────────────────────┤
                                                                  ▼
    install 沙箱（非 root、可出网）：INSTALLER 在容器里安全解压到 SRC_DIR，再 pip install
                                                                  ▼
    docker commit → failgate-env:<key>   key 含 仓库 + 提交 SHA + 源码包摘要 + Python

为什么不在宿主机解压：源码包是外部内容，tar 里的 "../"、绝对路径、符号链接都能把文件
写到解压目录之外（tar slip）。容器里解压用 tarfile 的 "data" 过滤器，就算过滤器被绕过，
能写的也只有沙箱用户自己的目录。

原始源码留在镜像的 SRC_DIR：L2（仓库内测试，下一步）要用仓库里的 tests/。
源码包里没有 .git，setuptools-scm / hatch-vcs 推算不出版本号，所以用
SETUPTOOLS_SCM_PRETEND_VERSION 给一个"该提交之前最近的正式版的下一个开发版"。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import tarfile
import tempfile
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version

from failgate.platforms.github_rest import GitHubRest, TarballTooLarge
from failgate.repro.envcache import Env, EnvCache
from failgate.repro.package import SCRIPT_NAME
from failgate.repro.pypi import Release, pick_python
from failgate.repro.sandbox import DockerSandbox, ExecResult

SRC_DIR = "/home/failgate/src"
TARBALL_NAME = "src.tar.gz"
MAX_TARBALL_BYTES = 50 * 1024 * 1024
_MAX_PYPROJECT_BYTES = 256 * 1024
_SKIP_DIRS = {".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".venv", ".tox"}
TEST_DIRS = ("tests", "test", "testing")

# 在 install 沙箱里执行。argv 固定为 python -c INSTALLER <tarball> <目标目录> [额外依赖…]，
# 白名单按整段脚本精确匹配，调用方改不了脚本内容。额外依赖（L2 要的 pytest==X）来自
# 程序自己算出的版本号，不来自 issue 或模型。
INSTALLER = r'''
import os, subprocess, sys, tarfile
src, dest, extra = sys.argv[1], sys.argv[2], sys.argv[3:]
if not hasattr(tarfile, "data_filter"):
    sys.exit("这个 Python 的 tarfile 没有 data 过滤器，拒绝解压不可信的源码包")
with tarfile.open(src, "r:gz") as tar:
    members = tar.getmembers()
    tops = {m.name.split("/", 1)[0] for m in members}
    if len(tops) != 1:
        sys.exit(f"源码包应只有一个顶层目录，实际是 {sorted(tops)[:5]}")
    top = tops.pop()
    keep = []
    for m in members:
        if m.name == top:
            continue
        m.name = m.name[len(top) + 1:]
        keep.append(m)
    os.makedirs(dest, exist_ok=True)
    tar.extractall(dest, members=keep, filter="data")
os.remove(src)
sys.exit(subprocess.call([sys.executable, "-m", "pip", "install", "--no-cache-dir", dest, *extra]))
'''
INSTALL_PREFIXES: tuple[tuple[str, ...], ...] = (("python", "-c", INSTALLER),)


class SourceError(RuntimeError):
    """源码拿不到或不合规（太大、不是 tar.gz、没有顶层目录）。"""


@dataclass(frozen=True)
class SourceTree:
    """某个提交的源码包。repo 是来源的名字（owner/name，或 fixture 的 local:名字）。"""

    repo: str
    sha: str
    committed_at: datetime | None
    tarball: bytes

    @property
    def top_dir(self) -> str:
        return _top_dir(self.tarball)

    def pyproject(self) -> dict[str, object] | None:
        """只读 tar 里的 pyproject.toml 这一个成员到内存，不往磁盘写任何东西。"""
        name = f"{self.top_dir}/pyproject.toml"
        with tarfile.open(fileobj=io.BytesIO(self.tarball), mode="r:gz") as tar:
            try:
                member = tar.getmember(name)
            except KeyError:
                return None
            if not member.isfile() or member.size > _MAX_PYPROJECT_BYTES:
                return None
            f = tar.extractfile(member)
            if f is None:
                return None
            try:
                return tomllib.loads(f.read().decode("utf-8", errors="replace"))
            except tomllib.TOMLDecodeError:
                return None

    def read_files(
        self, want: Callable[[str], bool], *, max_bytes: int = 256 * 1024, limit: int = 3000
    ) -> dict[str, str]:
        """在内存里读出 want(相对路径) 为真的普通文件（跳过过大的、链接等），不写磁盘。"""
        top = self.top_dir
        out: dict[str, str] = {}
        with tarfile.open(fileobj=io.BytesIO(self.tarball), mode="r:gz") as tar:
            for m in tar.getmembers():
                if not m.isfile() or m.size > max_bytes or not m.name.startswith(f"{top}/"):
                    continue
                rel = m.name[len(top) + 1:]
                if not want(rel):
                    continue
                f = tar.extractfile(m)
                if f is not None:
                    out[rel] = f.read().decode("utf-8", errors="replace")
                if len(out) >= limit:
                    break
        return out

    def test_dir(self) -> str:
        """仓库的测试目录（相对仓库根）：tests / test / testing 里第一个存在的，默认 tests。"""
        top = self.top_dir
        with tarfile.open(fileobj=io.BytesIO(self.tarball), mode="r:gz") as tar:
            dirs = {m.name.split("/")[1] for m in tar.getmembers()
                    if m.name.count("/") >= 2 and m.name.startswith(f"{top}/")}
        return next((d for d in TEST_DIRS if d in dirs), TEST_DIRS[0])

    def requires_python(self) -> SpecifierSet | None:
        project = (self.pyproject() or {}).get("project")
        raw = project.get("requires-python") if isinstance(project, dict) else None
        if not isinstance(raw, str):
            return None
        try:
            return SpecifierSet(raw)
        except InvalidSpecifier:
            return None


def _top_dir(tarball: bytes) -> str:
    try:
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
            tops = {m.name.split("/", 1)[0] for m in tar.getmembers()}
    except (tarfile.TarError, EOFError, OSError) as e:
        raise SourceError(f"源码包不是合法的 tar.gz：{e}") from e
    if len(tops) != 1:
        raise SourceError(f"源码包应只有一个顶层目录，实际是 {sorted(tops)[:5]}")
    return tops.pop()


def check_tarball(tarball: bytes) -> None:
    if len(tarball) > MAX_TARBALL_BYTES:
        raise SourceError(
            f"源码包 {len(tarball) / 1e6:.1f} MB，超过上限 {MAX_TARBALL_BYTES / 1e6:.0f} MB"
        )
    _top_dir(tarball)


def pack_dir(path: Path, *, top: str = "src") -> bytes:
    """把本地目录（fixture 仓库）打成和 GitHub 一样"只有一个顶层目录"的 tar.gz。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(path.rglob("*")):
            rel = p.relative_to(path)
            if any(part in _SKIP_DIRS for part in rel.parts) or not p.is_file():
                continue
            tar.add(p, arcname=f"{top}/{rel.as_posix()}", recursive=False)
    return buf.getvalue()


def pick_python_for_commit(
    tree: SourceTree, *, reported: str | None = None, preferred: str | None = None
) -> str:
    """和 package 模式同一套规则：用户报告的 → 配置的 → 提交当时已有的最新 Python。"""
    pseudo = Release(
        version=Version("0"), uploaded=tree.committed_at,
        requires_python=tree.requires_python(), yanked=False,
    )
    return pick_python(pseudo, reported, preferred)


def pretend_version(releases: Mapping[Version, Release], committed_at: datetime | None) -> str:
    """该提交之前最近的正式版的下一个开发版，如 23.10.1 → 23.10.2.dev0。

    和 setuptools-scm 默认的 guess-next-dev 一致，版本比较（>= 某版本才有的特性）不会乱。
    """
    older = [
        v for v, r in releases.items()
        if not v.is_prerelease and not r.yanked and r.uploaded is not None
        and (committed_at is None or r.uploaded <= committed_at)
    ]
    if not older:
        return "0.0.0.dev0"
    base = max(older).release
    return ".".join(str(x) for x in (*base[:-1], base[-1] + 1)) + ".dev0"


async def fetch_github_tree(
    gh: GitHubRest, repo: str, ref: str | None = None, *, before: datetime | None = None
) -> SourceTree:
    """下载 GitHub 上某个提交的源码包（边下边数，超过上限就中止）。

    ref 和 before 二选一：before 表示默认分支上不晚于这个时间的最后一个提交。
    """
    if ref is None:
        if before is None:
            raise ValueError("ref 和 before 至少给一个")
        found = await gh.commit_before(repo, before)
        if found is None:
            raise SourceError(f"{repo} 在 {before:%Y-%m-%d} 之前没有提交")
        ref = str(found["sha"])
    info = await gh.commit(repo, ref)
    date = (info.get("commit") or {}).get("committer", {}).get("date")
    try:
        sha, tarball = await gh.fetch_tarball(repo, info["sha"], max_bytes=MAX_TARBALL_BYTES)
    except TarballTooLarge as e:
        raise SourceError(str(e)) from e
    return SourceTree(
        repo=repo, sha=sha, tarball=tarball,
        committed_at=datetime.fromisoformat(date.replace("Z", "+00:00")) if date else None,
    )


async def run_script(
    sandbox: DockerSandbox, env: Env, script: str, *, timeout_s: int = 120
) -> ExecResult:
    """在只放了这份脚本的全新工作区里运行一次（断网、只读根文件系统）。"""
    ws = await sandbox.create_workspace(f"src-{env.key[:8]}")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            await asyncio.to_thread(Path(tmp, SCRIPT_NAME).write_text, script, encoding="utf-8")
            await sandbox.copy_in(ws, Path(tmp), env.image)
        return await sandbox.run(env.image, ws, ["python", SCRIPT_NAME], timeout_s=timeout_s)
    finally:
        await sandbox.remove_workspace(ws)


async def source_env(
    cache: EnvCache,
    tree: SourceTree,
    *,
    python: str,
    version: str,
    extra_requirements: Sequence[str] = (),
) -> Env:
    """准备（或命中缓存）某个提交的源码环境。失败时抛 EnvBuildError / SandboxError。

    extra_requirements：和项目一起装的依赖（L2 的 pytest==X），会进入安装命令和缓存 key。
    """
    check_tarball(tree.tarball)
    with tempfile.TemporaryDirectory() as tmp:
        await asyncio.to_thread(Path(tmp, TARBALL_NAME).write_bytes, tree.tarball)
        return await cache.get(
            python=python,
            install_argv=["python", "-c", INSTALLER, f"/workspace/{TARBALL_NAME}", SRC_DIR,
                          *extra_requirements],
            mode="source",
            # 摘要：fixture 目录没有真正的提交 SHA，内容一变就必须得到新环境
            key_extra={"repo": tree.repo, "sha": tree.sha, "version": version,
                       "digest": hashlib.sha256(tree.tarball).hexdigest()},
            prepare_dir=Path(tmp),
            env=[f"SETUPTOOLS_SCM_PRETEND_VERSION={version}"],
            allowed=INSTALL_PREFIXES,
        )
