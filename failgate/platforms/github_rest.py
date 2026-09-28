"""GitHub 只读客户端：回填历史 issue（failgate index build）、文档和源码包、
回放时查修复提交（GraphQL，必须带 token）。

- 公开仓库不带 token 也能读，但限额是每小时 60 次；带 token 是 5000 次
- /issues 接口会把 PR 也返回回来（带 pull_request 字段），需要过滤
- 分页走响应头里的 Link: <...>; rel="next"
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx

_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


class TarballTooLarge(RuntimeError):
    pass


class GraphQLError(RuntimeError):
    pass


class GitHubRest:
    def __init__(
        self,
        token: str = "",
        *,
        base_url: str = "https://api.github.com",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=30, transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _paginate(
        self, url: str, params: dict[str, Any] | None, *, items_key: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        next_url: str | None = url
        while next_url:
            r = await self._http.get(next_url, params=params)
            r.raise_for_status()
            data = r.json()
            for item in data[items_key] if items_key else data:
                yield item
            m = _NEXT.search(r.headers.get("link", ""))
            # next 链接自带完整查询参数，必须传 None（见 iter_issues 的注释）
            next_url, params = (m.group(1), None) if m else (None, None)

    async def search_issue_numbers(self, query: str, *, limit: int = 1000) -> list[int]:
        """GitHub 搜索 API。注意：单个查询最多返回 1000 条结果，且限额是每分钟 30 次。"""
        numbers: list[int] = []
        params = {"q": query, "per_page": 100}
        async for item in self._paginate("/search/issues", params, items_key="items"):
            numbers.append(item["number"])
            if len(numbers) >= limit:
                break
        return numbers

    async def fetch_tarball(
        self, full_name: str, ref: str = "HEAD", *, max_bytes: int | None = None
    ) -> tuple[str, bytes]:
        """下载某个提交的源码包，返回 (完整 commit SHA, tar.gz 字节)。

        先把 ref 解析成 SHA 再下载，保证文档内容和引用链接里的 SHA 是同一个版本。
        /tarball 会 302 跳转到 codeload.github.com，需要跟随重定向。
        max_bytes：边下载边计数，超过就中止（源码包是外部内容，不能无限读进内存）。
        """
        sha = (await self.commit(full_name, ref))["sha"]
        url = f"/repos/{full_name}/tarball/{sha}"
        async with self._http.stream("GET", url, follow_redirects=True) as r:
            r.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            async for chunk in r.aiter_bytes():
                size += len(chunk)
                if max_bytes is not None and size > max_bytes:
                    raise TarballTooLarge(f"{full_name}@{sha[:10]} 的源码包超过 {max_bytes} 字节")
                chunks.append(chunk)
        return sha, b"".join(chunks)

    async def commit(self, full_name: str, ref: str) -> dict[str, Any]:
        """一个提交的信息：sha、commit.committer.date、parents[].sha ……"""
        r = await self._http.get(f"/repos/{full_name}/commits/{ref}")
        r.raise_for_status()
        data: dict[str, Any] = r.json()
        return data

    async def commit_before(self, full_name: str, until: datetime) -> dict[str, Any] | None:
        """默认分支上不晚于 until 的最后一个提交（回放的"时间旅行"：issue 创建时的代码）。

        不带时区的时间按 UTC 处理（回放库里存的就是 UTC），并显式写成 Z 结尾：不带时区
        发给 GitHub 会被按别的时区解释，实测取到了 issue 创建 15 分钟之后的提交。
        """
        if until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        stamp = until.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        params: dict[str, str | int] = {"until": stamp, "per_page": 1}
        r = await self._http.get(f"/repos/{full_name}/commits", params=params)
        r.raise_for_status()
        items = r.json()
        return items[0] if items else None

    async def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """GitHub GraphQL（必须带 token）。返回 data；有 errors 时抛 GraphQLError。"""
        r = await self._http.post("/graphql", json={"query": query, "variables": variables})
        r.raise_for_status()
        body = r.json()
        if body.get("errors"):
            raise GraphQLError("; ".join(e.get("message", "") for e in body["errors"])[:500])
        data: dict[str, Any] = body.get("data") or {}
        return data

    async def list_labels(self, full_name: str) -> list[dict[str, Any]]:
        return [x async for x in self._paginate(f"/repos/{full_name}/labels", {"per_page": 100})]

    async def list_comments(self, full_name: str, number: int) -> list[dict[str, Any]]:
        params = {"per_page": 100}
        url = f"/repos/{full_name}/issues/{number}/comments"
        return [c async for c in self._paginate(url, params)]

    async def iter_issues(
        self, full_name: str, *, limit: int = 1000, since: datetime | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        params: dict[str, Any] | None = {"state": "all", "per_page": 100, "sort": "created"}
        if since is not None:
            assert params is not None
            params["since"] = since.isoformat()
        url: str | None = f"/repos/{full_name}/issues"
        yielded = 0
        while url and yielded < limit:
            # next 链接自带完整的查询参数；此时必须传 None 而不是 {}，
            # 否则 httpx 会用空参数覆盖掉 URL 里的 ?page=2，导致一直读第一页
            r = await self._http.get(url, params=params)
            r.raise_for_status()
            for item in r.json():
                if "pull_request" in item:
                    continue
                yield item
                yielded += 1
                if yielded >= limit:
                    return
            m = _NEXT.search(r.headers.get("link", ""))
            url, params = (m.group(1), None) if m else (None, None)
