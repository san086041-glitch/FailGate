"""检索用的分词。

- 英文与代码：按非字母数字切分并转小写；带下划线、点号的标识符同时保留整体和拆开的部分
  （read_parquet → read_parquet, read, parquet），这样搜整体和搜局部都能命中。
- 中文：不引入分词器，用相邻两个汉字组成的 bigram（"读取分区" → 读取 取分 分区）。
  bigram 在中文检索里是经典的无词典方案，召回率高、实现零依赖，代价是会产生一些无意义的 bigram，
  但它们在语料里通常很稀有或很常见，BM25 的 IDF 会把影响压低。
"""

from __future__ import annotations

import re

_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")
_WORD = re.compile(r"[a-z0-9][a-z0-9_.]*[a-z0-9]|[a-z0-9]")
_SPLIT_IDENT = re.compile(r"[_.]+")

STOPWORDS = frozenset(
    "a an and are as at be but by can do does for from has have i if in is it its me my "
    "not of on or so that the this to was we when with you your".split()
)


_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


def _norm_line(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip().lower()


def boilerplate_lines(
    texts: list[str], *, min_ratio: float = 0.05, min_count: int = 3
) -> frozenset[str]:
    """从语料里学出 issue 模板的固定行：出现在 ≥5%（且至少 3 个）issue 中的行。

    GitHub issue 模板会让每个 issue 都带上 "**Describe the bug**"、"**To Reproduce**" 这类行。
    它们对区分 issue 毫无帮助，却会给 BM25 带来噪声、拉长文档。按"行的文档频率"过滤是数据驱动的，
    不需要为每个仓库手写模板规则；阈值取 5%，真实内容的行几乎不可能在 5% 的 issue 里逐字重复。
    """
    if not texts:
        return frozenset()
    df: dict[str, int] = {}
    for text in texts:
        for line in {_norm_line(x) for x in _HTML_COMMENT.sub("", text).splitlines()}:
            if line:
                df[line] = df.get(line, 0) + 1
    threshold = max(min_count, int(min_ratio * len(texts)))
    return frozenset(line for line, n in df.items() if n >= threshold)


def strip_boilerplate(text: str, boilerplate: frozenset[str]) -> str:
    """去掉 HTML 注释（模板里的填写说明）和模板固定行。"""
    lines = _HTML_COMMENT.sub("", text).splitlines()
    return "\n".join(x for x in lines if _norm_line(x) not in boilerplate)


def tokenize(text: str) -> list[str]:
    text = text.lower()
    tokens: list[str] = []
    for run in _CJK.findall(text):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    for word in _WORD.findall(_CJK.sub(" ", text)):
        if word in STOPWORDS:
            continue
        tokens.append(word)
        parts = [p for p in _SPLIT_IDENT.split(word) if p]
        if len(parts) > 1:
            tokens.extend(p for p in parts if p not in STOPWORDS)
    return tokens
