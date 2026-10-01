"""Fixer App：自带修复 Agent 在 GitHub 上的写入身份（ADR 0029）。

为什么单独一个 App：核验 App（阅卷）只有 Issues / Pull requests 写权限，**不能改代码**；
修复要推分支，需要 Contents 写权限，交给另一个默认不装的 App。这样"阅卷的不能改代码"在
权限上成立，Fixer 开的 PR 来自另一个机器人账号，核验 App 也不会把它当成自己的回声事件。

只做一件事：把一组文件（完整内容）以一个提交推到 failgate/fix-N，没有 PR 就开一个。
  · 树以 base 提交的树为底：每一轮推的都是"base + 这一轮的全部改动"，上一轮改过、这一轮
    改回去的文件由调用方以 base 的内容传进来；
  · 提交的父提交是分支当前的头（第一次是 base），PR 上能看到每一轮的提交；
  · 幂等：新树和分支头的树一样就不再提交；同一分支已有打开的 PR 就不再开。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .github_app import GitHubApiError, GitHubApp

BRANCH_PREFIX = "failgate/fix-"


def fix_branch(issue: int) -> str:
    return f"{BRANCH_PREFIX}{issue}"


class PushResult(BaseModel):
    branch: str
    commit_sha: str | None  # None：和分支头一样，没有新提交
    pr_number: int
    pr_url: str
    created_pr: bool


class FixerClient:
    def __init__(self, app: GitHubApp) -> None:
        self.app = app
        self._installations: dict[str, int] = {}

    async def installation_id(self, repo: str) -> int:
        if repo not in self._installations:
            data = await self.app._app_request("GET", f"/repos/{repo}/installation")
            self._installations[repo] = int(data["id"])
        return self._installations[repo]

    async def _call(self, repo: str, method: str, url: str, json: Any = None) -> Any:
        client = self.app.installation(await self.installation_id(repo))
        return await client._call(method, url, json=json)

    async def bot_login(self) -> str:
        """Fixer 机器人的登录名（slug[bot]）：核验 App 只放行它开的 PR 事件。"""
        data = await self.app.get_app()
        return f"{data['slug']}[bot]"

    async def push_fix(
        self, repo: str, *, base_sha: str, base_branch: str, branch: str,
        files: dict[str, str], message: str, title: str, body: str,
    ) -> PushResult:
        head = await self._branch_head(repo, branch)
        parent = head or base_sha
        base_tree = (await self._call(repo, "GET", f"/repos/{repo}/git/commits/{base_sha}"))[
            "tree"]["sha"]
        tree = await self._call(repo, "POST", f"/repos/{repo}/git/trees", json={
            "base_tree": base_tree,
            "tree": [{"path": p, "mode": "100644", "type": "blob", "content": c}
                     for p, c in sorted(files.items())],
        })
        commit_sha: str | None = None
        parent_tree = (await self._call(repo, "GET", f"/repos/{repo}/git/commits/{parent}"))[
            "tree"]["sha"] if head else None
        if tree["sha"] != parent_tree:
            commit = await self._call(repo, "POST", f"/repos/{repo}/git/commits", json={
                "message": message, "tree": tree["sha"], "parents": [parent],
            })
            commit_sha = commit["sha"]
            if head is None:
                await self._call(repo, "POST", f"/repos/{repo}/git/refs",
                                 json={"ref": f"refs/heads/{branch}", "sha": commit_sha})
            else:
                await self._call(repo, "PATCH", f"/repos/{repo}/git/refs/heads/{branch}",
                                 json={"sha": commit_sha, "force": False})
        owner = repo.split("/", 1)[0]
        open_prs = await self._call(
            repo, "GET", f"/repos/{repo}/pulls?head={owner}:{branch}&state=open")
        if open_prs:
            pr = open_prs[0]
            return PushResult(branch=branch, commit_sha=commit_sha, pr_number=pr["number"],
                              pr_url=pr["html_url"], created_pr=False)
        pr = await self._call(repo, "POST", f"/repos/{repo}/pulls", json={
            "title": title, "head": branch, "base": base_branch, "body": body,
        })
        return PushResult(branch=branch, commit_sha=commit_sha, pr_number=pr["number"],
                          pr_url=pr["html_url"], created_pr=True)

    async def _branch_head(self, repo: str, branch: str) -> str | None:
        try:
            data = await self._call(repo, "GET", f"/repos/{repo}/git/ref/heads/{branch}")
        except GitHubApiError as e:
            if e.status == 404:
                return None
            raise
        return str(data["object"]["sha"])

    async def aclose(self) -> None:
        await self.app.aclose()
