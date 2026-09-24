"""GitHub REST 只读客户端：目前只用于回填历史 issue（warden index build）。

- 公开仓库不带 token 也能读，但限额是每小时 60 次；带 token 是 5000 次
- /issues 接口会把 PR 也返回回来（带 pull_request 字段），需要过滤
- 分页走响应头里的 Link: <...>; rel="next"
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import httpx

_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


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

    async def fetch_tarball(self, full_name: str, ref: str = "HEAD") -> tuple[str, bytes]:
        """下载某个提交的源码包，返回 (完整 commit SHA, tar.gz 字节)。

        先把 ref 解析成 SHA 再下载，保证文档内容和引用链接里的 SHA 是同一个版本。
        /tarball 会 302 跳转到 codeload.github.com，需要跟随重定向。
        """
        r = await self._http.get(f"/repos/{full_name}/commits/{ref}")
        r.raise_for_status()
        sha = r.json()["sha"]
        r = await self._http.get(f"/repos/{full_name}/tarball/{sha}", follow_redirects=True)
        r.raise_for_status()
        return sha, r.content

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
