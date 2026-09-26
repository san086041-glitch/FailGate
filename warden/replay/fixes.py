"""标准答案里的修复提交：issue 是被哪个 PR（或提交）关闭的，它的父提交是哪个。

严格 FB/PA（技术方案 18 节）需要"只差这一次修复"的两份代码：父提交上应该失败，
修复提交上应该通过。

为什么用 GraphQL：REST 时间线里 closed 事件的 commit_id 往往是空的（PR 合并关闭 issue 时，
提交出现在相邻的 referenced 事件里，还可能混着别的提交）；GraphQL 的 ClosedEvent.closer
直接给出"关闭它的是哪个 PR / 提交"。

合并提交有两个父提交时取第一个：那是主线上合并之前的状态。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from warden.platforms.github_rest import GitHubRest

CLOSER_QUERY = """
query($owner: String!, $name: String!, $n: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $n) {
      timelineItems(itemTypes: [CLOSED_EVENT], last: 1) {
        nodes {
          ... on ClosedEvent {
            closer {
              __typename
              ... on PullRequest {
                number merged
                mergeCommit { oid committedDate parents(first: 2) { nodes { oid } } }
              }
              ... on Commit { oid committedDate parents(first: 2) { nodes { oid } } }
            }
          }
        }
      }
    }
  }
}
"""


class FixCommit(BaseModel):
    pr: int | None  # 关闭 issue 的 PR；直接由提交关闭时为 None
    sha: str
    parent: str
    committed_at: datetime | None = None
    merge: bool = False  # 有两个父提交（真正的合并提交），parent 取的是第一个


def parse_closer(data: dict[str, Any]) -> FixCommit | None:
    """从 CLOSER_QUERY 的结果里取修复提交。手动关闭、PR 没合并、没有父提交时返回 None。"""
    issue = ((data.get("repository") or {}).get("issue")) or {}
    nodes = (issue.get("timelineItems") or {}).get("nodes") or []
    closer = (nodes[-1] if nodes else {}).get("closer") or {}
    kind = closer.get("__typename")
    if kind == "PullRequest":
        if not closer.get("merged"):
            return None
        commit, pr = closer.get("mergeCommit") or {}, closer.get("number")
    elif kind == "Commit":
        commit, pr = closer, None
    else:
        return None
    parents = [p["oid"] for p in (commit.get("parents") or {}).get("nodes") or []]
    if not commit.get("oid") or not parents:
        return None
    date = commit.get("committedDate")
    return FixCommit(
        pr=pr, sha=commit["oid"], parent=parents[0], merge=len(parents) > 1,
        committed_at=datetime.fromisoformat(date.replace("Z", "+00:00")) if date else None,
    )


async def find_fix(gh: GitHubRest, repo: str, number: int) -> FixCommit | None:
    owner, name = repo.split("/", 1)
    data = await gh.graphql(CLOSER_QUERY, {"owner": owner, "name": name, "n": number})
    return parse_closer(data)
