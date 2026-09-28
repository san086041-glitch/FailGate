"""文档索引：把仓库里的 README、docs/、CHANGELOG 等切成小块，供 Answer 模块检索引用。

切块原则（技术方案第 9 节"按标题层级切块"）：
- 按 Markdown 的 # 标题、reStructuredText 的下划线标题切成"节"；每块带上标题路径
  （"Usage › Configuration"），检索时标题权重 ×2，引用时显示成人能看懂的出处；
- 一节太长（> MAX_CHUNK_CHARS）就在空行处继续切，但不会切断代码块；
- 链接用 commit SHA 的永久链接 + 标题锚点：文档后来改了，引用仍然指向当时的内容。
"""

from __future__ import annotations

import io
import logging
import re
import tarfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from failgate.db import Database, DocChunk

from .bm25 import BM25
from .embed import Embedder, cosine
from .store import CHANNEL_WEIGHTS, DEFAULT_RRF_K, rrf
from .text import tokenize

log = logging.getLogger(__name__)

MAX_CHUNK_CHARS = 1500
MIN_CHUNK_CHARS = 40
MAX_FILE_BYTES = 300_000
MAX_FILES = 400
DOC_SUFFIXES = frozenset({".md", ".markdown", ".rst", ".txt"})
# 根目录下这些名字开头的文件也算文档（大小写不敏感）
ROOT_DOC_PREFIXES = ("readme", "changelog", "changes", "history", "contributing", "faq")
DOC_DIRS = ("docs/", "doc/", "documentation/")

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_RST_UNDERLINE = re.compile(r"^([=\-~^\"'`#*+<>:.])\1{2,}\s*$")


@dataclass(frozen=True)
class Chunk:
    path: str
    heading: str  # 标题路径，例如 "Usage › Configuration"
    anchor: str
    text: str


def github_anchor(title: str) -> str:
    """GitHub 渲染 Markdown 时给标题生成的锚点：小写、去掉标点、空格变连字符。"""
    t = re.sub(r"[`*_\[\]()<>]", "", title).strip().lower()
    t = re.sub(r"[^\w\- ]", "", t)
    return t.replace(" ", "-")


def is_doc_path(path: str) -> bool:
    p = PurePosixPath(path)
    suffix = p.suffix.lower()
    if len(p.parts) == 1:
        return p.name.lower().startswith(ROOT_DOC_PREFIXES) and suffix in DOC_SUFFIXES | {""}
    return path.lower().startswith(DOC_DIRS) and suffix in DOC_SUFFIXES


def _sections(text: str, rst: bool) -> list[tuple[list[str], list[str]]]:
    """按标题切成 (标题路径, 正文行)。代码块里的 # 不算标题。"""
    sections: list[tuple[list[str], list[str]]] = []
    path: list[str] = []
    body: list[str] = []
    rst_levels: list[str] = []  # rst 的标题层级由"第一次出现的下划线字符"的顺序决定
    in_fence = False
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if _FENCE.match(line):
            in_fence = not in_fence
        heading: tuple[int, str] | None = None
        if not in_fence:
            m = _MD_HEADING.match(line) if not rst else None
            if m:
                heading = (len(m.group(1)), m.group(2))
            elif rst and i + 1 < len(lines) and line.strip() and _RST_UNDERLINE.match(lines[i + 1]):
                if len(lines[i + 1].strip()) >= len(line.strip()):
                    ch = lines[i + 1].strip()[0]
                    if ch not in rst_levels:
                        rst_levels.append(ch)
                    heading = (rst_levels.index(ch) + 1, line.strip())
                    i += 1  # 跳过下划线那一行
        if heading is not None:
            sections.append((list(path), body))
            level, title = heading
            path = path[: level - 1] + [title]
            body = []
        else:
            body.append(line)
        i += 1
    sections.append((list(path), body))
    return sections


def _split_long(lines: list[str]) -> list[str]:
    """一节太长时在空行处切开；代码块内部不切。"""
    pieces: list[str] = []
    buf: list[str] = []
    size = 0
    in_fence = False
    for line in lines:
        if _FENCE.match(line):
            in_fence = not in_fence
        buf.append(line)
        size += len(line) + 1
        if size >= MAX_CHUNK_CHARS and not in_fence and not line.strip():
            pieces.append("\n".join(buf).strip())
            buf, size = [], 0
    if buf:
        pieces.append("\n".join(buf).strip())
    return [p for p in pieces if p]


def chunk_document(path: str, text: str) -> list[Chunk]:
    rst = path.lower().endswith(".rst")
    out: list[Chunk] = []
    for heading_path, body in _sections(text, rst):
        heading = " › ".join(heading_path)
        anchor = github_anchor(heading_path[-1]) if heading_path else ""
        for piece in _split_long(body):
            if len(piece) >= MIN_CHUNK_CHARS:
                out.append(Chunk(path=path, heading=heading, anchor=anchor, text=piece))
    return out


def extract_docs(tarball: bytes) -> list[tuple[str, str]]:
    """从 GitHub 的仓库 tarball 里挑出文档文件，返回 (仓库内路径, 文本)。"""
    files: list[tuple[str, str]] = []
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
        for member in tar:
            if not member.isfile() or member.size > MAX_FILE_BYTES:
                continue
            # tarball 顶层目录是 "owner-repo-<sha>/"，去掉它
            parts = member.name.split("/", 1)
            if len(parts) < 2 or not is_doc_path(parts[1]):
                continue
            f = tar.extractfile(member)
            if f is None:
                continue
            files.append((parts[1], f.read().decode("utf-8", errors="replace")))
            if len(files) >= MAX_FILES:
                break
    return sorted(files)


@dataclass
class DocHit:
    id: int
    path: str
    heading: str
    text: str
    url: str
    score: float


class DocIndex:
    """文档块检索：BM25（词法）+ 可选的向量通道（语义），用 RRF 融合，和查重的召回同一套做法。

    向量通道解决"用词不同"：用户写 "optional comma"，文档叫 "magic trailing comma"。
    向量第一次检索时按需计算并存进 doc_chunks.embedding，之后复用。
    """

    def __init__(self, db: Database, embedder: Embedder | None = None) -> None:
        self.db = db
        self.embedder = embedder
        # 仓库 → (语料版本, 文档块, BM25)。重建索引后版本号变化，自动失效
        self._cache: dict[int, tuple[tuple[int, int], list[DocChunk], BM25]] = {}

    async def replace(
        self, s: AsyncSession, repo_id: int, full_name: str, sha: str, chunks: Sequence[Chunk]
    ) -> int:
        """整体替换一个仓库的文档块（文档规模小，全量重建比增量对账简单可靠）。"""
        await s.execute(delete(DocChunk).where(DocChunk.repo_id == repo_id))
        for c in chunks:
            url = f"https://github.com/{full_name}/blob/{sha}/{c.path}"
            s.add(
                DocChunk(
                    repo_id=repo_id,
                    path=c.path,
                    heading=c.heading,
                    text=c.text,
                    url=f"{url}#{c.anchor}" if c.anchor else url,
                    commit_sha=sha,
                )
            )
        return len(chunks)

    async def search(self, repo_id: int, query: str, *, k: int) -> list[DocHit]:
        async with self.db.session() as s:
            rows = list(
                (
                    await s.scalars(
                        select(DocChunk).where(DocChunk.repo_id == repo_id).order_by(DocChunk.id)
                    )
                ).all()
            )
        if not rows:
            return []
        version = (len(rows), rows[-1].id)
        cached = self._cache.get(repo_id)
        if cached is None or cached[0] != version:
            cached = (version, rows, BM25([_chunk_tokens(r) for r in rows]))
            self._cache[repo_id] = cached
        _, rows, bm25 = cached

        lexical = bm25.scores(tokenize(query))
        rankings = {"lexical": _ranked(lexical)}
        if self.embedder is not None:
            try:
                vectors = await self._vectors(rows)
                (qv,) = await self.embedder.embed([query[:MAX_EMBED_CHARS]])
                rankings["semantic"] = _ranked([cosine(qv, v) for v in vectors])
            except Exception:
                # 向量通道是增强项，失败时退回纯词法
                log.exception("doc semantic channel failed; falling back to BM25")
        fused = rrf(rankings, DEFAULT_RRF_K, CHANNEL_WEIGHTS)
        top = sorted(fused, key=lambda i: fused[i], reverse=True)[:k]
        return [
            DocHit(rows[i].id, rows[i].path, rows[i].heading, rows[i].text, rows[i].url, fused[i])
            for i in top
        ]

    async def _vectors(self, rows: list[DocChunk]) -> list[list[float]]:
        assert self.embedder is not None
        missing = [r for r in rows if not r.embedding]
        for start in range(0, len(missing), 64):
            batch = missing[start : start + 64]
            vecs = await self.embedder.embed([_embed_text(r) for r in batch])
            async with self.db.session() as s, s.begin():
                for r, v in zip(batch, vecs, strict=True):
                    r.embedding = v
                    await s.merge(r)
        return [r.embedding or [] for r in rows]


MAX_EMBED_CHARS = 4000
CHANNEL_DEPTH = 50


def _ranked(values: list[float]) -> list[int]:
    return sorted((i for i, v in enumerate(values) if v > 0), key=lambda i: -values[i])[
        :CHANNEL_DEPTH
    ]


def _embed_text(row: DocChunk) -> str:
    return f"{row.path} {row.heading}\n{row.text}"[:MAX_EMBED_CHARS]


def _chunk_tokens(row: DocChunk) -> list[str]:
    head = tokenize(f"{row.path} {row.heading}")
    return head + head + tokenize(row.text)
