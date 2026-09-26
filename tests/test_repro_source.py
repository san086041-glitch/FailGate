"""source 模式的环境：源码包的安全处理、版本与 Python 的推算、环境缓存、修复提交解析。

真实 Docker 的集成测试在最后（用本地 fixture 目录，不访问 GitHub；装构建依赖要访问 PyPI）。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from warden.platforms.github_rest import GitHubRest, GraphQLError, TarballTooLarge
from warden.replay.fixes import parse_closer
from warden.repro import source
from warden.repro.envcache import EnvBuildError, EnvCache, env_key
from warden.repro.pypi import Release
from warden.repro.sandbox import DockerSandbox, ExecResult
from warden.repro.source import (
    INSTALL_PREFIXES,
    INSTALLER,
    SourceError,
    SourceTree,
    check_tarball,
    pack_dir,
    pick_python_for_commit,
    pretend_version,
    run_script,
    source_env,
)


def make_tar(members: dict[str, bytes], *, links: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type, info.linkname = tarfile.SYMTYPE, target
            tar.addfile(info)
    return buf.getvalue()


def write_project(root: Path, *, requires: str = ">=3.9", version_line: bool = True) -> Path:
    (root / "mylib").mkdir(parents=True)
    (root / "mylib" / "__init__.py").write_text(
        "from importlib.metadata import version\n__version__ = version('mylib')\n"
        if version_line else "", encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        "[build-system]\nrequires = ['setuptools>=61']\nbuild-backend = 'setuptools.build_meta'\n"
        f"[project]\nname = 'mylib'\ndynamic = ['version']\nrequires-python = '{requires}'\n"
        "[tool.setuptools.dynamic]\nversion = {attr = 'mylib._v.V'}\n",
        encoding="utf-8",
    )
    (root / "mylib" / "_v.py").write_text("V = '1.0'\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (root / "mylib" / "__pycache__").mkdir()
    (root / "mylib" / "__pycache__" / "x.pyc").write_bytes(b"junk")
    return root


def tree_of(tarball: bytes, *, day: datetime | None = None) -> SourceTree:
    return SourceTree(repo="local:mylib", sha="deadbeef", committed_at=day, tarball=tarball)


# ---------------------------------------------------------------- 源码包


def test_pack_dir_has_single_top_dir_and_skips_junk(tmp_path: Path):
    tarball = pack_dir(write_project(tmp_path / "p"))
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
        names = tar.getnames()
    assert all(n.startswith("src/") for n in names)
    assert "src/pyproject.toml" in names and "src/mylib/__init__.py" in names
    assert not any(".git" in n or "__pycache__" in n for n in names)
    assert tree_of(tarball).top_dir == "src"


def test_requires_python_read_from_tar_without_extracting(tmp_path: Path):
    tree = tree_of(pack_dir(write_project(tmp_path / "p", requires=">=3.10,<3.13")))
    assert tree.requires_python() == SpecifierSet(">=3.10,<3.13")
    assert tree_of(make_tar({"top/setup.py": b""})).requires_python() is None
    assert tree_of(make_tar({"top/pyproject.toml": b"not = [toml"})).requires_python() is None


def test_check_tarball_rejects_bad_input(monkeypatch: pytest.MonkeyPatch):
    check_tarball(make_tar({"top/a.py": b"x"}))
    with pytest.raises(SourceError, match="一个顶层目录"):
        check_tarball(make_tar({"a/x.py": b"", "b/y.py": b""}))
    with pytest.raises(SourceError, match="tar.gz"):
        check_tarball(b"definitely not gzip")
    monkeypatch.setattr(source, "MAX_TARBALL_BYTES", 10)
    with pytest.raises(SourceError, match="超过上限"):
        check_tarball(make_tar({"top/a.py": b"x"}))


def run_installer(tmp_path: Path, tarball: bytes) -> tuple[subprocess.CompletedProcess[str], Path]:
    """在本机执行容器里用的同一段安装脚本。目标目录里没有项目，pip 那一步会马上失败，
    所以这里只看解压这一步的行为。"""
    work = tmp_path / "work"
    work.mkdir()
    (work / "src.tar.gz").write_bytes(tarball)
    dest = work / "out" / "src"
    proc = subprocess.run(
        [sys.executable, "-c", INSTALLER, str(work / "src.tar.gz"), str(dest)],
        capture_output=True, text=True, timeout=120,
    )
    return proc, dest


def test_installer_strips_top_dir_and_removes_tarball(tmp_path: Path):
    proc, dest = run_installer(tmp_path, make_tar({"black-abc/pkg/a.py": b"x = 1\n"}))
    assert (dest / "pkg" / "a.py").read_text() == "x = 1\n"
    assert not (tmp_path / "work" / "src.tar.gz").exists()


@pytest.mark.parametrize(
    "tarball",
    [
        make_tar({"top/ok.py": b"", "top/../../evil.txt": b"pwned"}),  # ../ 穿越
        make_tar({"top/ok.py": b""}, links={"top/link": "/etc/passwd"}),  # 指向外面的链接
        make_tar({"top/ok.py": b""}, links={"top/up": "../../.."}),
    ],
    ids=["dotdot", "abs-symlink", "rel-symlink-out"],
)
def test_installer_refuses_path_traversal(tmp_path: Path, tarball: bytes):
    proc, dest = run_installer(tmp_path, tarball)
    assert proc.returncode != 0
    assert "Error" in proc.stderr  # tarfile 的过滤器报错，没有走到 pip
    assert not (tmp_path / "evil.txt").exists() and not (tmp_path / "work" / "evil.txt").exists()


def test_installer_refuses_multiple_top_dirs(tmp_path: Path):
    proc, _ = run_installer(tmp_path, make_tar({"a/x.py": b"", "b/y.py": b""}))
    assert proc.returncode != 0 and "一个顶层目录" in proc.stderr


def test_install_whitelist_matches_only_the_exact_installer():
    from warden.repro.sandbox import SandboxError, check_command

    check_command(["python", "-c", INSTALLER, "/workspace/src.tar.gz", "/x"], INSTALL_PREFIXES)
    with pytest.raises(SandboxError):
        check_command(["python", "-c", "import os; os.system('id')"], INSTALL_PREFIXES)


# ---------------------------------------------------------------- 版本号与 Python


def rel(v: str, day: str, *, yanked: bool = False) -> tuple[Version, Release]:
    return Version(v), Release(
        version=Version(v), uploaded=datetime.fromisoformat(day).replace(tzinfo=UTC),
        requires_python=None, yanked=yanked,
    )


def test_pretend_version_is_next_dev_of_latest_release_before_commit():
    releases = dict([
        rel("23.10.0", "2023-10-05"), rel("23.10.1", "2023-10-23"),
        rel("23.11.0a1", "2023-10-28"),  # 预发布不算
        rel("23.12.0", "2023-10-29", yanked=True),  # 撤回的不算
        rel("23.11.0", "2023-11-09"),  # 在提交之后
    ])
    day = datetime(2023, 10, 30, tzinfo=UTC)
    assert pretend_version(releases, day) == "23.10.2.dev0"
    assert pretend_version({}, day) == "0.0.0.dev0"
    assert pretend_version(dict([rel("24.1", "2024-01-01")]), None) == "24.2.dev0"


def test_python_follows_commit_date_and_requires_python(tmp_path: Path):
    tarball = pack_dir(write_project(tmp_path / "p", requires=">=3.8"))
    day = datetime(2023, 10, 30, tzinfo=UTC)
    assert pick_python_for_commit(tree_of(tarball, day=day)) == "3.12"  # 3.12 发布于 2023-10-02
    assert pick_python_for_commit(tree_of(tarball, day=day), reported="3.9.18") == "3.9"
    old = pack_dir(write_project(tmp_path / "q", requires="<3.11"))
    assert pick_python_for_commit(tree_of(old, day=day)) == "3.10"


# ---------------------------------------------------------------- 环境缓存


def test_env_key_without_extra_is_unchanged():
    # 和加 extra 之前的算法逐字节一致：已缓存的 package 环境不能失效
    import hashlib

    kw = {"mode": "package", "upstream_id": "sha256:u", "python": "3.12",
          "install_argv": ["pip", "install", "x==1"], "index_url": ""}
    old = hashlib.sha256(json.dumps(
        {"v": 1, "mode": "package", "upstream": "sha256:u", "python": "3.12",
         "install": ["pip", "install", "x==1"], "index": ""}, sort_keys=True,
    ).encode()).hexdigest()
    assert env_key(**kw) == old  # type: ignore[arg-type]
    assert env_key(**kw, extra={"sha": "a"}) != old  # type: ignore[arg-type]
    assert env_key(**kw, extra={"sha": "a"}) != env_key(**kw, extra={"sha": "b"})  # type: ignore[arg-type]


class RecordingSandbox:
    def __init__(self, *, install_exit: int = 0) -> None:
        self.images: dict[str, tuple[str, int]] = {}
        self.calls: list[tuple[str, object]] = []
        self.install_exit = install_exit

    async def ensure_image(self, image: str) -> None:
        self.images.setdefault(image, (f"sha256:{image}", 100))

    async def image_info(self, ref: str) -> tuple[str, int] | None:
        return self.images.get(ref)

    async def build_image(self, tag: str, dockerfile: str, **_: object) -> None:
        self.images[tag] = (f"sha256:{tag}", 110)

    async def create_workspace(self, case_key: str) -> str:
        return f"ws-{case_key}"

    async def remove_workspace(self, volume: str) -> None:
        self.calls.append(("rm", volume))

    async def copy_in(self, volume: str, src: Path, image: str) -> None:
        self.calls.append(("copy_in", sorted(os.listdir(src))))

    async def install(self, image, volume, argv, *, env=(), allowed=(), commit_to=None, **_):
        self.calls.append(("install", {"argv": list(argv), "env": list(env), "allowed": allowed}))
        if self.install_exit == 0 and commit_to:
            self.images[commit_to] = (f"sha256:{commit_to}", 130)
        return ExecResult(phase="install", argv=list(argv), exit_code=self.install_exit,
                          stderr="ERROR: build failed" if self.install_exit else "")

    async def run(self, image, volume, argv, **_):
        self.calls.append(("run", list(argv)))
        return ExecResult(phase="run", argv=list(argv), exit_code=0)

    async def remove_image(self, ref: str) -> None:
        self.images.pop(ref, None)


async def test_source_env_copies_tarball_then_runs_whitelisted_installer(tmp_path: Path):
    sb = RecordingSandbox()
    cache = EnvCache(sb, tmp_path / "envcache.json")  # type: ignore[arg-type]
    tree = tree_of(pack_dir(write_project(tmp_path / "p")))
    env = await source_env(cache, tree, python="3.12", version="1.0.1.dev0")
    assert not env.cache_hit
    kinds = [c[0] for c in sb.calls]
    assert kinds.index("copy_in") < kinds.index("install")
    assert ("copy_in", ["src.tar.gz"]) in sb.calls
    install = next(c[1] for c in sb.calls if c[0] == "install")
    assert isinstance(install, dict)
    assert install["argv"][:3] == ["python", "-c", INSTALLER]
    assert "SETUPTOOLS_SCM_PRETEND_VERSION=1.0.1.dev0" in install["env"]
    assert install["allowed"] == INSTALL_PREFIXES

    again = await source_env(cache, tree, python="3.12", version="1.0.1.dev0")
    assert again.cache_hit and again.key == env.key
    other = SourceTree(repo=tree.repo, sha="cafebabe", committed_at=None, tarball=tree.tarball)
    assert (await source_env(cache, other, python="3.12", version="1.0.1.dev0")).key != env.key


async def test_fixture_content_change_gives_new_env(tmp_path: Path):
    cache = EnvCache(RecordingSandbox(), tmp_path / "envcache.json")  # type: ignore[arg-type]
    root = write_project(tmp_path / "p")
    first = await source_env(cache, tree_of(pack_dir(root)), python="3.12", version="1.0")
    (root / "mylib" / "_v.py").write_text("V = '1.1'\n", encoding="utf-8")
    second = await source_env(cache, tree_of(pack_dir(root)), python="3.12", version="1.0")
    assert first.key != second.key  # 同一个 "sha"，内容不同


async def test_source_env_build_failure_raises(tmp_path: Path):
    cache = EnvCache(RecordingSandbox(install_exit=1), tmp_path / "envcache.json")  # type: ignore[arg-type]
    with pytest.raises(EnvBuildError, match="build failed"):
        await source_env(cache, tree_of(make_tar({"t/a.py": b""})), python="3.12", version="1")


async def test_source_env_rejects_bad_tarball_before_touching_docker(tmp_path: Path):
    sb = RecordingSandbox()
    cache = EnvCache(sb, tmp_path / "envcache.json")  # type: ignore[arg-type]
    with pytest.raises(SourceError):
        await source_env(cache, tree_of(b"junk"), python="3.12", version="1")
    assert sb.calls == []


# ---------------------------------------------------------------- GitHub


def gh_with(handler) -> GitHubRest:  # type: ignore[no-untyped-def]
    return GitHubRest("t", transport=httpx.MockTransport(handler))


async def test_fetch_tarball_stops_at_size_limit():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/commits/abc"):
            return httpx.Response(200, json={"sha": "a" * 40})
        return httpx.Response(200, content=b"x" * 5000)

    gh = gh_with(handler)
    try:
        sha, data = await gh.fetch_tarball("o/r", "abc", max_bytes=10_000)
        assert sha == "a" * 40 and len(data) == 5000
        with pytest.raises(TarballTooLarge):
            await gh.fetch_tarball("o/r", "abc", max_bytes=1000)
    finally:
        await gh.aclose()


async def test_graphql_errors_raise():
    gh = gh_with(lambda req: httpx.Response(200, json={"errors": [{"message": "Bad"}]}))
    try:
        with pytest.raises(GraphQLError, match="Bad"):
            await gh.graphql("query{x}", {})
    finally:
        await gh.aclose()


def closer(node: dict | None) -> dict:  # type: ignore[type-arg]
    return {"repository": {"issue": {"timelineItems": {"nodes": [{"closer": node}]}}}}


def commit(oid: str, *parents: str) -> dict:  # type: ignore[type-arg]
    return {"oid": oid, "committedDate": "2026-07-21T00:54:33Z",
            "parents": {"nodes": [{"oid": p} for p in parents]}}


def test_parse_closer_variants():
    pr = parse_closer(closer({"__typename": "PullRequest", "number": 5241, "merged": True,
                              "mergeCommit": commit("42ed", "a20a")}))
    assert pr is not None and (pr.pr, pr.sha, pr.parent, pr.merge) == (5241, "42ed", "a20a", False)
    assert pr.committed_at == datetime(2026, 7, 21, 0, 54, 33, tzinfo=UTC)

    direct = parse_closer(closer({"__typename": "Commit", **commit("c1", "p1")}))
    assert direct is not None and direct.pr is None and direct.parent == "p1"

    merge = parse_closer(closer({"__typename": "Commit", **commit("m", "main", "topic")}))
    assert merge is not None and merge.parent == "main" and merge.merge

    unmerged = {"__typename": "PullRequest", "number": 1, "merged": False, "mergeCommit": None}
    assert parse_closer(closer(unmerged)) is None
    assert parse_closer(closer(None)) is None  # 手动关闭
    assert parse_closer({"repository": {"issue": {"timelineItems": {"nodes": []}}}}) is None
    assert parse_closer({}) is None


# ---------------------------------------------------------------- 真实 Docker


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory: pytest.TempPathFactory) -> DockerSandbox:
    sb = DockerSandbox(artifacts_dir=tmp_path_factory.mktemp("artifacts"))
    if asyncio.run(sb.server_version()) is None:
        pytest.skip("Docker 不可用")
    return sb


@pytest.mark.docker
async def test_real_source_env_from_local_dir(sandbox: DockerSandbox, tmp_path: Path):
    # 构建依赖 setuptools 要从 PyPI 下载
    cache = EnvCache(sandbox, tmp_path / "envcache.json")
    tree = tree_of(pack_dir(write_project(tmp_path / "p")))
    env = await source_env(cache, tree, python="3.12", version="9.9.dev0")
    try:
        probe = (
            "import mylib, os, sys\n"
            "print(mylib.__version__, os.getuid(), mylib.__file__.startswith(sys.prefix))\n"
            f"print(sorted(os.listdir('{source.SRC_DIR}')))\n"
        )
        r = await run_script(sandbox, env, probe, timeout_s=30)
        assert r.exit_code == 0, r.stderr
        first, second = r.stdout.splitlines()
        assert first.split() == ["1.0", "1000", "True"]  # 装进了 venv，不是从源码目录 import
        assert "pyproject.toml" in second and "mylib" in second  # 原始源码留在镜像里
    finally:
        await sandbox.remove_image(env.image)


@pytest.mark.docker
async def test_real_installer_refuses_traversal(sandbox: DockerSandbox, tmp_path: Path):
    cache = EnvCache(sandbox, tmp_path / "envcache.json")
    evil = make_tar({"top/ok.py": b"", "top/../../../home/warden/.bashrc": b"pwned"})
    with pytest.raises(EnvBuildError, match="OutsideDestination|outside"):
        await source_env(cache, tree_of(evil), python="3.12", version="1")


async def test_commit_before_sends_utc_with_z():
    # 回放库里的时间不带时区；不带时区发给 GitHub 会被按别的时区解释，取到"未来"的提交
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.params["until"])
        return httpx.Response(200, json=[{"sha": "a" * 40}])

    gh = gh_with(handler)
    try:
        await gh.commit_before("o/r", datetime(2023, 11, 21, 6, 18, 5))
        await gh.commit_before("o/r", datetime.fromisoformat("2023-11-21T15:18:05+09:00"))
    finally:
        await gh.aclose()
    assert seen == ["2023-11-21T06:18:05Z", "2023-11-21T06:18:05Z"]
