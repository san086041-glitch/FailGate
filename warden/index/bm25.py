"""Okapi BM25（纯 Python，内存索引）。

score(q, d) = Σ_t IDF(t) · tf(t,d)·(k1+1) / (tf(t,d) + k1·(1 − b + b·|d|/avgdl))
IDF(t)      = ln(1 + (N − df(t) + 0.5) / (df(t) + 0.5))      # Lucene 变体，恒为正

k1 控制词频饱和速度（同一个词出现第 10 次比第 1 次贡献小得多），b 控制文档长度归一化
（长文档天然包含更多词，需要惩罚）。取常用默认值 k1=1.5、b=0.75。

一个仓库几千个 issue 时，每次查询现建索引的开销在几十毫秒量级；再往上可以换成
SQLite FTS5 的 bm25() 或 PostgreSQL 全文检索，接口不变。
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence


class BM25:
    def __init__(self, docs: Sequence[Sequence[str]], *, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.tfs = [Counter(d) for d in docs]
        self.lens = [len(d) for d in docs]
        self.n = len(docs)
        self.avgdl = (sum(self.lens) / self.n) if self.n else 0.0
        df: Counter[str] = Counter()
        for tf in self.tfs:
            df.update(tf.keys())
        self.idf = {t: math.log(1 + (self.n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: Sequence[str]) -> list[float]:
        terms = [t for t in dict.fromkeys(query) if t in self.idf]
        out = [0.0] * self.n
        if not terms or not self.avgdl:
            return out
        for i, tf in enumerate(self.tfs):
            norm = self.k1 * (1 - self.b + self.b * self.lens[i] / self.avgdl)
            s = 0.0
            for t in terms:
                f = tf.get(t)
                if f:
                    s += self.idf[t] * f * (self.k1 + 1) / (f + norm)
            out[i] = s
        return out
