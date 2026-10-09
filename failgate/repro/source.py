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
# monorepo（ADR 0045）：整包照样解到 dest，只是 pip install 的目标换成 dest/<subdir>。
# 单独一段脚本而不是给 INSTALLER 加参数：INSTALLER 的内容进环境缓存 key，一改就让所有
# 已缓存的 source 环境失效；subdir 为空时继续用原脚本，行为和 key 都逐字节不变。
INSTALLER_SUBDIR = r'''
import os, re, subprocess, sys, tarfile
src, dest, subdir, extra = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
if not re.fullmatch(r"[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*", subdir) or ".." in subdir.split("/"):
    sys.exit(f"subdir 不合法：{subdir!r}")
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
target = os.path.join(dest, subdir)
if not os.path.isfile(os.path.join(target, "pyproject.toml")) and \
        not os.path.isfile(os.path.join(target, "setup.py")):
    sys.exit(f"subdir {subdir!r} 下没有 pyproject.toml 或 setup.py")
pip = [sys.executable, "-m", "pip", "install", "--no-cache-dir"]
sys.exit(subprocess.call([*pip, target, *extra]))
'''
INSTALL_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("python", "-c", INSTALLER), ("python", "-c", INSTALLER_SUBDIR),
)


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

    def pyproject(self, subdir: str | None = None) -> dict[str, object] | None:
        """只读 tar 里的 pyproject.toml 这一个成员到内存，不往磁盘写任何东西。

        subdir：monorepo 里包所在的子目录，读它自己的那份（ADR 0045）。"""
        base = f"{self.top_dir}/{subdir}" if subdir else self.top_dir
        name = f"{base}/pyproject.toml"
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

    def test_dir(self, subdir: str | None = None) -> str:
        """仓库的测试目录（相对仓库根）：tests / test / testing 里第一个存在的，默认 tests。

        subdir 非空时在子目录里找，返回值带上子目录前缀；测试目录下有 unit_tests 时用它
        （LangChain 的布局：tests/unit_tests 和需要外部服务的 tests/integration_tests 分开，
        conftest 也分开放，考卷要放进单元测试那一边）。"""
        top = self.top_dir
        base = f"{top}/{subdir}/" if subdir else f"{top}/"
        depth = base.count("/")
        with tarfile.open(fileobj=io.BytesIO(self.tarball), mode="r:gz") as tar:
            names = [m.name for m in tar.getmembers()
                     if m.name.startswith(base) and m.name.count("/") > depth]
        dirs = {n.split("/")[depth] for n in names}
        found = next((d for d in TEST_DIRS if d in dirs), TEST_DIRS[0])
        if not subdir:
            return found
        units = any(n.startswith(f"{base}{found}/unit_tests/") for n in names)
        return f"{subdir}/{found}" + ("/unit_tests" if units else "")

    def overlay(self, changes: Mapping[str, str | None], *, label: str) -> SourceTree:
        """在这个源码包上改文件，得到一个新的源码包（离线评测构造负例用）。

        changes：相对仓库根的路径 → 新内容（None 表示删除）。label 当作新包的"提交号"，
        环境缓存按源码摘要区分，所以不会和原提交的环境混在一起。

        新写的文件沿用原文件的修改时间（新增的用包里最晚的时间）：TarInfo 默认 mtime=0
        （1970 年），改的是会打进 wheel 的源码时，flit 等构建后端写 zip 会因为"早于 1980 年"
        直接失败（ADR 0041 的 break_other 在 packaging 上踩到）。"""
        top = self.top_dir
        out = io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(self.tarball), mode="r:gz") as src, \
                tarfile.open(fileobj=out, mode="w:gz") as dst:
            mtimes: dict[str, float] = {}
            latest = 0.0
            for m in src.getmembers():
                latest = max(latest, m.mtime)
                rel = m.name[len(top) + 1:] if m.name.startswith(f"{top}/") else None
                if rel is not None and rel in changes:
                    mtimes[rel] = m.mtime
                    continue
                dst.addfile(m, src.extractfile(m) if m.isfile() else None)
            for rel, content in changes.items():
                if content is None:
                    continue
                data = content.encode("utf-8")
                info = tarfile.TarInfo(f"{top}/{rel}")
                info.size, info.mode = len(data), 0o644
                info.mtime = mtimes.get(rel) or latest or 946684800.0  # 兜底 2000-01-01
                dst.addfile(info, io.BytesIO(data))
        return SourceTree(repo=self.repo, sha=label, committed_at=self.committed_at,
                          tarball=out.getvalue())

    def requires_python(self, subdir: str | None = None) -> SpecifierSet | None:
        project = (self.pyproject(subdir) or {}).get("project")
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
    tree: SourceTree, *, reported: str | None = None, preferred: str | None = None,
    subdir: str | None = None,
) -> str:
    """和 package 模式同一套规则：用户报告的 → 配置的 → 提交当时已有的最新 Python。"""
    pseudo = Release(
        version=Version("0"), uploaded=tree.committed_at,
        requires_python=tree.requires_python(subdir), yanked=False,
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
    subdir: str | None = None,
) -> Env:
    """准备（或命中缓存）某个提交的源码环境。失败时抛 EnvBuildError / SandboxError。

    extra_requirements：和项目一起装的依赖（L2 的 pytest==X），会进入安装命令和缓存 key。
    subdir：monorepo 里要装的子目录（ADR 0045）。它写在安装命令里，所以自然进入缓存 key；
    为空时安装命令和以前逐字节一致，已缓存的环境照常命中。
    """
    check_tarball(tree.tarball)
    if subdir:
        install = ["python", "-c", INSTALLER_SUBDIR, f"/workspace/{TARBALL_NAME}", SRC_DIR,
                   subdir]
    else:
        install = ["python", "-c", INSTALLER, f"/workspace/{TARBALL_NAME}", SRC_DIR]
    with tempfile.TemporaryDirectory() as tmp:
        await asyncio.to_thread(Path(tmp, TARBALL_NAME).write_bytes, tree.tarball)
        return await cache.get(
            python=python,
            install_argv=[*install, *extra_requirements],
            mode="source",
            # 摘要：fixture 目录没有真正的提交 SHA，内容一变就必须得到新环境
            key_extra={"repo": tree.repo, "sha": tree.sha, "version": version,
                       "digest": hashlib.sha256(tree.tarball).hexdigest()},
            prepare_dir=Path(tmp),
            env=[f"SETUPTOOLS_SCM_PRETEND_VERSION={version}"],
            allowed=INSTALL_PREFIXES,
        )
