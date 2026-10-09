"""复现配置与模式选择（技术方案 8.2 节、16 节 .failgate/config.yml 的 repro 段）。"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable
from enum import StrEnum
from typing import Literal

from packaging.version import Version
from pydantic import BaseModel, Field, field_validator

from failgate.repro.pypi import normalize_version, valid_package_name


class ReproMode(StrEnum):
    PACKAGE = "package"
    SOURCE = "source"
    ANALYZE_ONLY = "analyze-only"


DEFAULT_INSTALL = "pip install --no-cache-dir {name}=={version}"
# 模板里只允许出现这两个占位符；渲染后按 argv 执行，不经过 shell
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_SUBDIR = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*")


class PackageConfig(BaseModel):
    name: str
    # import 时用的名字：pyyaml → yaml、scikit-learn → sklearn。判定器按它过滤栈帧
    import_name: str | None = None
    install: str = DEFAULT_INSTALL
    # monorepo 里包所在的子目录（如 LangChain 的 "libs/core"，ADR 0045）：source 模式装这个
    # 目录、读它的 pyproject、测试放在它的测试目录下。None = 包就在仓库根，行为和以前一样
    subdir: str | None = None
    # L2 / 修复环境里和项目一起装的测试依赖（conftest 要 import 的插件），按提交日期锁版本
    test_deps: list[str] = Field(default_factory=list)

    @field_validator("subdir")
    @classmethod
    def _subdir_ok(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if v.startswith("/"):
            raise ValueError(f"subdir 要写成仓库内的相对路径（如 libs/core），不能以 / 开头：{v!r}")
        v = v.rstrip("/")
        if not v:
            return None
        if not _SUBDIR.fullmatch(v) or any(p in (".", "..") for p in v.split("/")):
            raise ValueError(f"subdir 要写成仓库内的相对路径（如 libs/core）：{v!r}")
        return v

    @field_validator("test_deps")
    @classmethod
    def _deps_ok(cls, v: list[str]) -> list[str]:
        # 只收包名：版本由程序按提交日期选，不让配置里混进 URL、路径或 pip 选项
        bad = [d for d in v if not valid_package_name(d)]
        if bad:
            raise ValueError(f"test_deps 只能写包名：{bad}")
        return v

    @field_validator("name")
    @classmethod
    def _name_ok(cls, v: str) -> str:
        if not valid_package_name(v):
            raise ValueError(f"包名不合法：{v!r}")
        return v

    @field_validator("install")
    @classmethod
    def _template_ok(cls, v: str) -> str:
        unknown = set(_PLACEHOLDER.findall(v)) - {"name", "version"}
        if unknown:
            raise ValueError(f"install 模板里有未知占位符：{sorted(unknown)}")
        return v

    @property
    def module(self) -> str:
        return self.import_name or self.name.replace("-", "_").lower()

    def install_argv(self, version: str) -> list[str]:
        # 先切词再替换：包名和版本号已经过校验，不会含空白，也就不可能多切出参数
        return [
            tok.replace("{name}", self.name).replace("{version}", version)
            for tok in shlex.split(self.install)
        ]


class ReproConfig(BaseModel):
    mode: Literal["auto", "package", "source", "analyze-only"] = "auto"
    package: PackageConfig | None = None
    python: str | None = None  # 技术方案里的 runtime.python
    skip_if_labels: list[str] = Field(default_factory=lambda: ["gpu", "distributed"])


def needs_source(raw_version: str | None, package: str, released: Iterable[Version]) -> bool:
    """用户报告了版本、但这个版本 PyPI 上装不到（开发版、main@sha、没发布的版本号）。

    这时 package 模式只能退回"issue 之前最新的正式版"，而 source 模式能用 issue 创建时的
    源码复现，更接近用户实际用的代码。没报版本的沿用 package 模式（还能查最新版是否已修复）。
    """
    if not raw_version or not raw_version.strip():
        return False
    v = normalize_version(raw_version, package)
    return v is None or v not in set(released)


_SPECIAL_HW = re.compile(r"\b(cuda|gpu|npu|rocm|tpu|nccl|multi[- ]?gpu)\b", re.I)


def needs_special_hardware(text: str) -> bool:
    return bool(_SPECIAL_HW.search(text))


def choose_mode(
    cfg: ReproConfig,
    *,
    labels: Iterable[str],
    reported_version: str | None,
    issue_text: str = "",
) -> ReproMode:
    """技术方案 8.2 节的 choose_mode。source 模式还没实现，auto 下暂不会选到它。"""
    skip = {s.lower() for s in cfg.skip_if_labels}
    if any(lb.lower() in skip for lb in labels) or needs_special_hardware(issue_text):
        return ReproMode.ANALYZE_ONLY
    if cfg.mode == "auto":
        if cfg.package is not None and reported_version:
            return ReproMode.PACKAGE
        return ReproMode.ANALYZE_ONLY
    return ReproMode(cfg.mode)
