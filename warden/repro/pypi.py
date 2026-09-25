"""package 模式的版本解析：Intake 抽出的版本 → PEP 440 规范化 → 到 PyPI 确认存在 → 选 Python 版本。

用户写版本号的方式五花八门："black 23.1.0"、"v23.1"、"23.1.0 (compiled: yes)"、
"black, 24.4.3.dev27+g7fa1faf"。规范化之后才能和 PyPI 上的发布列表比较；
开发版（.devN、+local）不是发布包，装不到，返回 None 交给 source 模式或分析模式。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

import httpx
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

_VER = r"\d+(?:\.\d+)+(?:(?:a|b|rc)\d+)?(?:\.post\d+)?(?:\.dev\d+)?(?:\+[\w.]+)?"
_VERSION_TOKEN = re.compile(rf"(?<![\w.])v?({_VER})", re.I)
_AFTER_PYTHON = re.compile(r"(?:python|cpython|py)\W{0,12}$", re.I)
_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")

# 沙箱可用的 Python 版本（官方 python:X.Y-slim 镜像）及其首次发布日期。
# 用户没报 Python 版本时，选"该包版本发布时已经存在的最新 Python"：老版本的包
# 往往没有为之后的 Python 出过 wheel，用新 Python 装会退回源码编译、甚至装不上。
PYTHON_RELEASES: dict[str, date] = {
    "3.8": date(2019, 10, 14),
    "3.9": date(2020, 10, 5),
    "3.10": date(2021, 10, 4),
    "3.11": date(2022, 10, 24),
    "3.12": date(2023, 10, 2),
    "3.13": date(2024, 10, 7),
    "3.14": date(2025, 10, 7),
}


def valid_package_name(name: str) -> bool:
    return bool(_NAME.match(name))


def normalize_version(raw: str | None, package: str | None = None) -> Version | None:
    """从一段自由文本里找出包的版本号并规范化；开发版和本地版本返回 None。

    优先取紧跟在包名后面的版本（"black, 23.1.0"）；否则取第一个前面不是
    "Python" 的版本号，避免把 "Python 3.12, black 23.1" 里的 3.12 当成包版本。
    """
    if not raw:
        return None
    candidates: list[str] = []
    if package:
        name = re.escape(package).replace(r"\-", "[-_.]").replace("_", "[-_.]")
        m = re.search(rf"\b{name}\b\W{{0,20}}?(?:version\W{{0,3}})?v?({_VER})", raw, re.I)
        if m:
            candidates.append(m.group(1))
    candidates += [
        m.group(1) for m in _VERSION_TOKEN.finditer(raw)
        if not _AFTER_PYTHON.search(raw[: m.start()])
    ]
    for token in candidates:
        try:
            v = Version(token)
        except InvalidVersion:
            continue
        if v.is_devrelease or v.local:
            return None
        return v
    return None


@dataclass(frozen=True)
class Release:
    version: Version
    uploaded: datetime | None
    requires_python: SpecifierSet | None
    yanked: bool


@dataclass(frozen=True)
class ResolvedVersion:
    name: str
    version: Version
    release: Release
    latest: Version  # 最新的正式版（不含预发布、不含撤回的）


class PyPIError(RuntimeError):
    pass


class PyPIClient:
    """只读 PyPI JSON API 客户端；同一个包的发布列表在进程内缓存。"""

    def __init__(self, base_url: str = "https://pypi.org", client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=30)
        self._cache: dict[str, dict[Version, Release]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def releases(self, name: str) -> dict[Version, Release]:
        key = canonicalize_name(name)
        if key in self._cache:
            return self._cache[key]
        resp = await self._client.get(f"{self.base_url}/pypi/{key}/json")
        if resp.status_code == 404:
            raise PyPIError(f"PyPI 上没有这个包：{name}")
        resp.raise_for_status()
        out: dict[Version, Release] = {}
        for ver, files in resp.json().get("releases", {}).items():
            try:
                v = Version(ver)
            except InvalidVersion:
                continue
            if not files:  # 只有版本号、没有任何文件的发布，装不了
                continue
            uploads = [u for f in files if (u := f.get("upload_time_iso_8601"))]
            spec = next((r for f in files if (r := f.get("requires_python"))), None)
            out[v] = Release(
                version=v,
                uploaded=min(datetime.fromisoformat(u.replace("Z", "+00:00")) for u in uploads)
                if uploads else None,
                requires_python=_spec(spec),
                yanked=all(f.get("yanked") for f in files),
            )
        self._cache[key] = out
        return out

    async def resolve(self, name: str, raw_version: str | None) -> ResolvedVersion:
        if not valid_package_name(name):
            raise PyPIError(f"包名不合法：{name!r}")
        v = normalize_version(raw_version, name)
        if v is None:
            raise PyPIError(f"无法从 {raw_version!r} 解析出可安装的发布版本")
        rel = await self.releases(name)
        stable = [r for r in rel.values() if not r.version.is_prerelease and not r.yanked]
        if not stable:
            raise PyPIError(f"{name} 没有正式发布的版本")
        latest = max(r.version for r in stable)
        # Version 比较按 PEP 440：23.1 == 23.1.0
        match = next((r for r in rel.values() if r.version == v), None)
        if match is None:
            raise PyPIError(f"PyPI 上没有 {name}=={v}")
        return ResolvedVersion(name=canonicalize_name(name), version=match.version,
                               release=match, latest=latest)


def _spec(raw: str | None) -> SpecifierSet | None:
    if not raw:
        return None
    try:
        return SpecifierSet(raw)
    except InvalidSpecifier:
        return None


def pick_python(release: Release, reported: str | None = None, preferred: str | None = None) -> str:
    """选沙箱用的 Python 版本（major.minor）。

    优先级：用户报告的版本 → 仓库配置的版本 → 发布当时已有的最新 Python。
    每一步都要满足该发布声明的 requires_python，且在沙箱支持的范围内。
    """

    def ok(py: str) -> bool:
        if py not in PYTHON_RELEASES:
            return False
        return release.requires_python is None or release.requires_python.contains(
            f"{py}.0", prereleases=True
        )

    for cand in (_major_minor(reported), _major_minor(preferred)):
        if cand and ok(cand):
            return cand
    supported = [py for py in PYTHON_RELEASES if ok(py)]
    if not supported:
        raise PyPIError(
            f"{release.version} 要求 Python {release.requires_python}，沙箱没有合适的镜像"
        )
    if release.uploaded is not None:
        day = release.uploaded.date()
        older = [py for py in supported if PYTHON_RELEASES[py] <= day]
        if older:
            return older[-1]
        return supported[0]
    return supported[-1]


def _major_minor(raw: str | None) -> str | None:
    if not raw:
        return None
    m = re.search(r"\b3\.(\d{1,2})\b", raw)
    return f"3.{m.group(1)}" if m else None
