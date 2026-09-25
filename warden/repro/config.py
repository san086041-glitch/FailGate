"""复现配置与模式选择（技术方案 8.2 节、16 节 .warden/config.yml 的 repro 段）。"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from warden.repro.pypi import valid_package_name


class ReproMode(StrEnum):
    PACKAGE = "package"
    SOURCE = "source"
    ANALYZE_ONLY = "analyze-only"


DEFAULT_INSTALL = "pip install --no-cache-dir {name}=={version}"
# 模板里只允许出现这两个占位符；渲染后按 argv 执行，不经过 shell
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


class PackageConfig(BaseModel):
    name: str
    # import 时用的名字：pyyaml → yaml、scikit-learn → sklearn。判定器按它过滤栈帧
    import_name: str | None = None
    install: str = DEFAULT_INSTALL

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
