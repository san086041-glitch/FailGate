"""从 GitHub 构建情景记忆（ADR 0032）：合并的 PR → Episode。

1. GraphQL 分页拉合并的 PR（标题、描述、合并时间、关闭的 issue、改动文件）；
2. 只留改了源码的 PR（测试、文档、CI 配置不算）；
3. 对这些 PR 用 REST 取每个源码文件的 diff（截断），提取函数名。

不调 LLM。black 约两千个合并 PR，第 3 步是主要耗时（每个 PR 一次请求）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from failgate.memory.episodic import Episode, FileChange, IssueRef, hunk_functions, trim_patch
from failgate.replay.fix_eval import is_test_change

BODY_CHARS = 1500
ISSUE_BODY_CHARS = 800
NON_SOURCE_PREFIXES = ("docs/", ".github/", "scripts/", "profiling/", "gallery/", "action/")
NON_SOURCE_FILES = ("CHANGES.md", "AUTHORS.md", "README.md", ".pre-commit-config.yaml",
                    "pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "Dockerfile")

PRS_QUERY = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: MERGED, first: 50, after: $after,
                 orderBy: {field: CREATED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title body mergedAt
        mergeCommit { oid }
        closingIssuesReferences(first: 5) { nodes { number title body } }
        files(first: 100) { nodes { path } }
      }
    }
  }
}
"""


class _GitHub(Protocol):
    async def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]: ...

    async def pull_files(self, full_name: str, number: int) -> list[dict[str, Any]]: ...


def is_source(path: str, test_dir: str = "tests") -> bool:
    return not (is_test_change(path, test_dir) or path in NON_SOURCE_FILES
                or path.startswith(NON_SOURCE_PREFIXES))


def episode_from(node: dict[str, Any], test_dir: str = "tests") -> Episode | None:
    """GraphQL 的一个 PR 节点 → 还没有 diff 的 Episode；没改源码的返回 None。"""
    files = [f["path"] for f in ((node.get("files") or {}).get("nodes") or [])]
    if not node.get("mergedAt") or not any(is_source(p, test_dir) for p in files):
        return None
    issues = [IssueRef(number=i["number"], title=i.get("title") or "",
                       body=(i.get("body") or "")[:ISSUE_BODY_CHARS])
              for i in ((node.get("closingIssuesReferences") or {}).get("nodes") or [])]
    return Episode(
        pr=node["number"], title=node.get("title") or "",
        body=(node.get("body") or "")[:BODY_CHARS],
        merged_at=datetime.fromisoformat(node["mergedAt"].replace("Z", "+00:00")),
        commit=(node.get("mergeCommit") or {}).get("oid"), issues=issues, files=files)


def attach_patches(ep: Episode, raw: list[dict[str, Any]], test_dir: str = "tests") -> Episode:
    changes = []
    for f in raw:
        path = f["filename"]
        if not is_source(path, test_dir):
            continue
        patch = f.get("patch") or ""
        changes.append(FileChange(path=path, functions=hunk_functions(patch),
                                  patch=trim_patch(patch)))
    return ep.model_copy(update={"changes": changes})


async def fetch_episodes(gh: _GitHub, repo: str, *, concurrency: int = 4,
                         progress: Callable[[str], None] | None = None) -> list[Episode]:
    owner, name = repo.split("/", 1)
    pending: list[Episode] = []
    after: str | None = None
    while True:
        data = await gh.graphql(PRS_QUERY, {"owner": owner, "name": name, "after": after})
        prs = data["repository"]["pullRequests"]
        for node in prs["nodes"]:
            if ep := episode_from(node):
                pending.append(ep)
        if progress:
            progress(f"已拉 PR 列表，到 #{prs['nodes'][-1]['number'] if prs['nodes'] else '-'}，"
                     f"改了源码的 {len(pending)} 个")
        if not prs["pageInfo"]["hasNextPage"]:
            break
        after = prs["pageInfo"]["endCursor"]
    sem = asyncio.Semaphore(concurrency)
    done = 0

    async def one(ep: Episode) -> Episode:
        nonlocal done
        async with sem:
            out = attach_patches(ep, await gh.pull_files(repo, ep.pr))
        done += 1
        if progress and done % 100 == 0:
            progress(f"diff {done}/{len(pending)}")
        return out

    return list(await asyncio.gather(*(one(e) for e in pending)))
