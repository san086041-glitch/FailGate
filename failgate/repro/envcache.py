"""环境缓存（技术方案 8.3 节）：同一个"包@版本 + Python"只装一次，之后直接用镜像。

两层镜像：

    python:3.12-slim                         官方镜像
      └─ failgate-base:py3.12-<上游 ID>        可信的 Dockerfile：建 1000 用户和属于它的 venv
           └─ failgate-env:<env_key 前 16 位>   install 阶段沙箱里 pip install 后 docker commit

为什么要 base 层：install 阶段以 uid 1000 运行，官方镜像的 site-packages 属于 root，
直接 pip install 没有权限；而让 pip 以 root 运行，等于让被安装包的构建脚本拿到 root。
所以先用可信步骤把 venv 的属主交给 1000，不可信的安装只在非 root 沙箱里做。

env_key 覆盖所有会影响环境内容的输入：模式、官方镜像的 ID（上游更新了镜像，key 就变）、
Python 版本、安装命令（含包名和版本）、镜像源；source 模式再加上仓库、提交和源码包摘要
（见 source.py）。任何一项变了都会得到新的环境。

淘汰：索引文件记录每个环境的大小和最后使用时间，总量超过上限时按 LRU 删镜像。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from failgate.repro.sandbox import DEFAULT_INSTALL_PREFIXES, DockerSandbox, ExecResult, SandboxError

VENV = "/opt/venv"

BASE_DOCKERFILE = """\
FROM {upstream}
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin failgate \\
 && python -m venv {venv} \\
 && chown -R 1000:1000 {venv}
ENV PATH={venv}/bin:$PATH VIRTUAL_ENV={venv}
USER 1000
WORKDIR /workspace
"""


def upstream_image(python: str) -> str:
    return f"python:{python}-slim"


def base_tag(python: str, upstream_id: str) -> str:
    # 带上官方镜像 ID：上游镜像更新后自动得到新的 base，不会和 env_key 对不上
    return f"failgate-base:py{python}-{upstream_id.removeprefix('sha256:')[:12]}"


def env_key(
    *,
    mode: str,
    upstream_id: str,
    python: str,
    install_argv: list[str],
    index_url: str = "",
    extra: Mapping[str, str] | None = None,
) -> str:
    """extra：安装命令之外、同样决定环境内容的输入（source 模式的仓库、提交、源码摘要）。
    没有 extra 时 payload 和以前完全一样，已缓存的 package 环境不会失效。"""
    data: dict[str, object] = {
        "v": 1, "mode": mode, "upstream": upstream_id, "python": python,
        "install": install_argv, "index": index_url,
    }
    if extra:
        data["extra"] = dict(extra)
    payload = json.dumps(data, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def env_tag(key: str) -> str:
    return f"failgate-env:{key[:16]}"


@dataclass
class Env:
    key: str
    image: str
    python: str
    cache_hit: bool
    install: ExecResult | None = None  # 缓存未命中时的安装记录


class EnvBuildError(RuntimeError):
    def __init__(self, message: str, result: ExecResult | None = None) -> None:
        super().__init__(message)
        self.result = result


class EnvCache:
    def __init__(
        self,
        sandbox: DockerSandbox,
        index_path: Path,
        *,
        max_bytes: int = 20 * 1024**3,
        index_url: str = "",
    ) -> None:
        self.sandbox = sandbox
        self.index_path = index_path
        self.max_bytes = max_bytes
        self.index_url = index_url
        # 同一个 key 并发构建时只构建一次（进程内）
        self._locks: dict[str, asyncio.Lock] = {}

    # ---- 索引（key → {image, bytes, last_used}）

    def _load(self) -> dict[str, dict[str, object]]:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, index: dict[str, dict[str, object]]) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(index, indent=2), encoding="utf-8")
        tmp.replace(self.index_path)

    def _touch(self, key: str, image: str, size: int) -> None:
        index = self._load()
        index[key] = {"image": image, "bytes": size, "last_used": time.time()}
        self._save(index)

    # ---- 对外接口

    async def ensure_base(self, python: str) -> tuple[str, str]:
        """返回 (官方镜像 ID, base 镜像标签)；base 镜像不存在就构建。"""
        upstream = upstream_image(python)
        await self.sandbox.ensure_image(upstream)
        info = await self.sandbox.image_info(upstream)
        if info is None:
            raise SandboxError(f"找不到镜像：{upstream}")
        upstream_id = info[0]
        tag = base_tag(python, upstream_id)
        if await self.sandbox.image_info(tag) is None:
            await self.sandbox.build_image(
                tag, BASE_DOCKERFILE.format(upstream=upstream, venv=VENV)
            )
        return upstream_id, tag

    async def get(
        self,
        *,
        python: str,
        install_argv: list[str],
        mode: str = "package",
        timeout_s: int = 900,
        key_extra: Mapping[str, str] | None = None,
        prepare_dir: Path | None = None,
        env: Sequence[str] = (),
        allowed: Sequence[Sequence[str]] = DEFAULT_INSTALL_PREFIXES,
    ) -> Env:
        """prepare_dir：安装前复制进工作区的目录（source 模式的源码包）；
        env / allowed：安装命令的额外环境变量和白名单（source 模式用自己的安装脚本）。"""
        upstream_id, base = await self.ensure_base(python)
        key = env_key(mode=mode, upstream_id=upstream_id, python=python,
                      install_argv=install_argv, index_url=self.index_url, extra=key_extra)
        tag = env_tag(key)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            info = await self.sandbox.image_info(tag)
            if info is not None:
                await asyncio.to_thread(self._touch, key, tag, await self._own_bytes(info, base))
                return Env(key=key, image=tag, python=python, cache_hit=True)

            install_env = [f"PIP_INDEX_URL={self.index_url}"] if self.index_url else []
            install_env += env
            ws = await self.sandbox.create_workspace(f"install-{key[:8]}")
            try:
                if prepare_dir is not None:
                    await self.sandbox.copy_in(ws, prepare_dir, base)
                res = await self.sandbox.install(
                    base, ws, install_argv, timeout_s=timeout_s,
                    env=install_env, commit_to=tag, allowed=allowed,
                )
            finally:
                await self.sandbox.remove_workspace(ws)
            if res.exit_code != 0:
                raise EnvBuildError(
                    f"安装失败（exit={res.exit_code}）：{res.output_tail(15)}", res
                )
            info = await self.sandbox.image_info(tag)
            if info is None:
                raise EnvBuildError("安装成功但没有生成环境镜像", res)
            await asyncio.to_thread(self._touch, key, tag, await self._own_bytes(info, base))
        await self.evict(keep={key})
        return Env(key=key, image=tag, python=python, cache_hit=False, install=res)

    async def _own_bytes(self, info: tuple[str, int], base: str) -> int:
        """镜像自己新增的字节数。docker 报告的 Size 含共享的 base 层，直接相加会严重高估。"""
        base_info = await self.sandbox.image_info(base)
        return max(info[1] - (base_info[1] if base_info else 0), 0)

    async def evict(self, keep: set[str] | None = None) -> list[str]:
        """总大小超过上限时，按最后使用时间从旧到新删除。keep 里的不删。"""
        index = await asyncio.to_thread(self._load)
        keep = keep or set()
        total = sum(int(str(v.get("bytes", 0))) for v in index.values())
        removed: list[str] = []
        for key, meta in sorted(index.items(), key=lambda kv: float(str(kv[1]["last_used"]))):
            if total <= self.max_bytes:
                break
            if key in keep:
                continue
            await self.sandbox.remove_image(str(meta["image"]))
            total -= int(str(meta.get("bytes", 0)))
            removed.append(key)
        if removed:
            for key in removed:
                index.pop(key, None)
            await asyncio.to_thread(self._save, index)
        return removed
