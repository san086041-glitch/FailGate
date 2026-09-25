"""环境缓存和 package 运行器：用假沙箱测逻辑；真实 Docker + PyPI 的集成测试在最后。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest

from warden.repro.config import PackageConfig
from warden.repro.envcache import EnvBuildError, EnvCache, env_key
from warden.repro.evidence import EvidenceLevel
from warden.repro.judge import VerdictKind
from warden.repro.package import PackageReproducer
from warden.repro.pypi import PyPIClient
from warden.repro.sandbox import DockerSandbox, ExecResult


class FakeSandbox:
    """只记账的沙箱：镜像是个字典；install 成功就"commit"出一个镜像。"""

    def __init__(self, *, install_exit: int = 0, failing_images: Sequence[str] = ()) -> None:
        self.images: dict[str, tuple[str, int]] = {"python:3.12-slim": ("sha256:up312", 100)}
        self.install_exit = install_exit
        self.failing_images = set(failing_images)
        self.installs: list[list[str]] = []
        self.runs: list[str] = []
        self.removed: list[str] = []
        self.volumes = 0

    async def ensure_image(self, image: str) -> None:
        self.images.setdefault(image, (f"sha256:{image}", 100))

    async def image_info(self, ref: str) -> tuple[str, int] | None:
        return self.images.get(ref)

    async def build_image(self, tag: str, dockerfile: str, **_: object) -> None:
        assert "USER 1000" in dockerfile and "chown -R 1000:1000" in dockerfile
        self.images[tag] = (f"sha256:{tag}", 110)

    async def create_workspace(self, case_key: str) -> str:
        self.volumes += 1
        return f"ws-{case_key}"

    async def remove_workspace(self, volume: str) -> None:
        self.volumes -= 1

    async def copy_in(self, volume: str, src: Path, image: str) -> None:
        assert (src / "repro.py").exists()

    async def install(self, image, volume, argv, *, commit_to=None, **_: object) -> ExecResult:
        assert image.startswith("warden-base:py")
        self.installs.append(list(argv))
        if self.install_exit == 0 and commit_to:
            self.images[commit_to] = (f"sha256:{commit_to}", 130)
        return ExecResult(phase="install", argv=list(argv), exit_code=self.install_exit,
                          stderr="ERROR: No matching distribution" if self.install_exit else "")

    async def run(self, image, volume, argv, **_: object) -> ExecResult:
        self.runs.append(image)
        if image in self.failing_images:
            return ExecResult(phase="run", argv=list(argv), exit_code=1, stderr=TRACE)
        return ExecResult(phase="run", argv=list(argv), exit_code=0)

    async def remove_image(self, ref: str) -> None:
        self.removed.append(ref)
        self.images.pop(ref, None)


TRACE = """\
Traceback (most recent call last):
  File "/workspace/repro.py", line 3, in <module>
  File "/opt/venv/lib/python3.12/site-packages/mylib/core.py", line 9, in parse
KeyError: 'name'
"""
REPORTED = TRACE.replace("/opt/venv/lib/python3.12", "C:/Users/u/venv/Lib")


def make_cache(sb: FakeSandbox, tmp_path: Path, **kw: object) -> EnvCache:
    return EnvCache(sb, tmp_path / "envcache.json", **kw)  # type: ignore[arg-type]


def test_env_key_changes_with_every_input():
    base = {"mode": "package", "upstream_id": "sha256:a", "python": "3.12",
            "install_argv": ["pip", "install", "x==1"], "index_url": ""}
    k = env_key(**base)  # type: ignore[arg-type]
    for field, value in [("upstream_id", "sha256:b"), ("python", "3.11"),
                         ("install_argv", ["pip", "install", "x==2"]),
                         ("index_url", "https://mirror/simple")]:
        assert env_key(**{**base, field: value}) != k  # type: ignore[arg-type]
    assert env_key(**base) == k  # type: ignore[arg-type]


async def test_miss_builds_then_hit_reuses(tmp_path: Path):
    sb = FakeSandbox()
    cache = make_cache(sb, tmp_path)
    argv = ["pip", "install", "--no-cache-dir", "mylib==1.0"]
    first = await cache.get(python="3.12", install_argv=argv)
    second = await cache.get(python="3.12", install_argv=argv)
    assert not first.cache_hit and second.cache_hit and first.image == second.image
    assert sb.installs == [argv] and sb.volumes == 0  # 只装了一次，工作区都删了
    index = json.loads((tmp_path / "envcache.json").read_text())
    # 记录的是自己新增的字节（130 - base 的 110），不含共享的 base 层
    assert index[first.key]["bytes"] == 20


async def test_concurrent_requests_build_once(tmp_path: Path):
    sb = FakeSandbox()
    cache = make_cache(sb, tmp_path)
    argv = ["pip", "install", "mylib==1.0"]
    envs = await asyncio.gather(*(cache.get(python="3.12", install_argv=argv) for _ in range(3)))
    assert len(sb.installs) == 1 and sum(not e.cache_hit for e in envs) == 1


async def test_failed_install_raises_with_log(tmp_path: Path):
    sb = FakeSandbox(install_exit=1)
    with pytest.raises(EnvBuildError, match="No matching distribution") as ei:
        await make_cache(sb, tmp_path).get(python="3.12", install_argv=["pip", "install", "x==9"])
    assert ei.value.result is not None and sb.volumes == 0


async def test_lru_eviction(tmp_path: Path):
    sb = FakeSandbox()
    cache = make_cache(sb, tmp_path, max_bytes=45)  # 每个环境 20 字节，只放得下 2 个
    envs = []
    for v in ("1", "2"):
        envs.append(await cache.get(python="3.12", install_argv=["pip", "install", f"x=={v}"]))
    await cache.get(python="3.12", install_argv=["pip", "install", "x==1"])  # 1 变成最近使用
    third = await cache.get(python="3.12", install_argv=["pip", "install", "x==3"])
    assert sb.removed == [envs[1].image]  # 删的是最久没用的 2，而不是刚用过的 1
    index = json.loads((tmp_path / "envcache.json").read_text())
    assert set(index) == {envs[0].key, third.key}


# ---------------------------------------------------------------- package 运行器

PYPI = {
    "releases": {
        "1.0": [{"upload_time_iso_8601": "2023-11-01T00:00:00Z", "requires_python": ">=3.8"}],
        "2.0": [{"upload_time_iso_8601": "2025-01-01T00:00:00Z", "requires_python": ">=3.9"}],
    }
}


def pypi() -> PyPIClient:
    return PyPIClient(client=httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=PYPI))
    ))


async def reproduce(sb: FakeSandbox, tmp_path: Path, version: str = "mylib 1.0"):
    cache = make_cache(sb, tmp_path)
    return await PackageReproducer(sb, cache, pypi()).reproduce(  # type: ignore[arg-type]
        PackageConfig(name="mylib"), reported_version=version, script="import mylib",
        reported_traceback=REPORTED,
    ), cache


def image_for(tmp_path: Path, version: str) -> str:
    # 两个版本的环境镜像名要先算出来，才能告诉假沙箱"哪个镜像会失败"
    from warden.repro.envcache import env_tag

    key = env_key(mode="package", upstream_id="sha256:up312", python="3.12",
                  install_argv=PackageConfig(name="mylib").install_argv(version))
    return env_tag(key)


async def test_reproduced_and_fixed_in_latest(tmp_path: Path):
    sb = FakeSandbox(failing_images=[image_for(tmp_path, "1.0")])
    result, _ = await reproduce(sb, tmp_path)
    assert result.level == EvidenceLevel.L1 and result.fixed_in_latest is True
    assert result.reported and result.reported.verdict.kind == VerdictKind.REPRODUCED
    assert result.reported.python == "3.12"
    # 最新版沿用同一个 Python
    assert result.latest and result.latest.python == "3.12" and result.latest.version == "2.0"
    assert "可能已经修复" in result.summary()
    assert sb.runs.count(image_for(tmp_path, "1.0")) == 4  # 1 次 + 3 次稳定性重跑


async def test_still_broken_in_latest(tmp_path: Path):
    sb = FakeSandbox(failing_images=[image_for(tmp_path, "1.0"), image_for(tmp_path, "2.0")])
    result, _ = await reproduce(sb, tmp_path)
    assert result.fixed_in_latest is False and "仍然复现" in result.summary()


async def test_not_reproduced_skips_latest(tmp_path: Path):
    sb = FakeSandbox()
    result, _ = await reproduce(sb, tmp_path)
    assert result.level == EvidenceLevel.NONE and result.latest is None
    assert result.reported and result.reported.verdict.kind == VerdictKind.NOT_REPRODUCED
    assert sb.runs == [image_for(tmp_path, "1.0")]


async def test_setup_errors_are_reported_not_raised(tmp_path: Path):
    result, _ = await reproduce(FakeSandbox(), tmp_path, version="mylib 9.9")
    assert result.error and "9.9" in result.error and result.level == EvidenceLevel.NONE
    result, _ = await reproduce(FakeSandbox(install_exit=1), tmp_path)
    assert result.error and "安装失败" in result.error


# ---------------------------------------------------------------- 真实 Docker + PyPI


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory: pytest.TempPathFactory) -> DockerSandbox:
    sb = DockerSandbox(artifacts_dir=tmp_path_factory.mktemp("artifacts"))
    if asyncio.run(sb.server_version()) is None:
        pytest.skip("Docker 不可用")
    return sb


@pytest.mark.docker
async def test_real_env_build_and_offline_readonly_run(sandbox: DockerSandbox, tmp_path: Path):
    # six 是纯 Python 的小包，装起来很快；需要能访问 PyPI
    cache = EnvCache(sandbox, tmp_path / "envcache.json")
    argv = PackageConfig(name="six").install_argv("1.16.0")
    env = await cache.get(python="3.12", install_argv=argv)
    try:
        assert not env.cache_hit and env.install is not None and env.install.exit_code == 0
        again = await cache.get(python="3.12", install_argv=argv)
        assert again.cache_hit and again.image == env.image
        ws = await sandbox.create_workspace("test-env")
        try:
            probe = (
                "import os, six, sys; print(six.__version__, os.getuid(), sys.prefix);"
                "open(six.__file__, 'a')"  # 环境在运行阶段应该是只读的
            )
            r = await sandbox.run(env.image, ws, ["python", "-c", probe], timeout_s=30)
        finally:
            await sandbox.remove_workspace(ws)
        assert r.stdout.split() == ["1.16.0", "1000", "/opt/venv"]
        assert r.failed and "Read-only file system" in r.stderr
    finally:
        await sandbox.remove_image(env.image)
