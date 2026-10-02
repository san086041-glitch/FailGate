"""命令行界面的中英文（ADR 0039）。

中英文就地成对写：`t("环境", "Environment")`，改一处时另一种语言就在旁边，不会漏改。
语言在 CLI 入口定一次（`--lang` > `FAILGATE_LANG` > 默认中文），之后各模块直接调 `t()`。
只管命令行界面；GitHub 评论的语言照旧按 issue / PR 正文判断（verify/report.py）。
"""

from __future__ import annotations

from typing import Literal

Lang = Literal["zh", "en"]
_lang: Lang = "zh"


def set_lang(value: str | None) -> Lang:
    """zh / en（大小写、zh-CN、en_US 之类都认）；认不出来就用中文。"""
    global _lang
    _lang = "en" if (value or "").strip().lower().startswith("en") else "zh"
    return _lang


def lang() -> Lang:
    return _lang


def t(zh: str, en: str) -> str:
    return en if _lang == "en" else zh
