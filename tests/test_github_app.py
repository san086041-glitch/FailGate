"""GitHub App 客户端：JWT、安装令牌缓存、限流退避、权限查询、标签读写。"""

import time

import httpx
import jwt
import pytest
from fake_github import PUBLIC_KEY, FakeGitHub

from warden.platforms.base import CaseKind, CaseRef, RepoRef
from warden.platforms.github_app import GitHubApiError, app_jwt

REPO = RepoRef(platform="github", full_name="acme/widgets")
ISSUE = CaseRef(repo=REPO, kind=CaseKind.ISSUE, number=7)


def test_app_jwt_claims():
    from fake_github import PRIVATE_KEY

    now = 1_700_000_000
    token = app_jwt("12345", PRIVATE_KEY, now=now)
    claims = jwt.decode(
        token, PUBLIC_KEY, algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False}
    )
    assert claims["iss"] == "12345"
    # 往前拨 60 秒容忍时钟偏差；总有效期不超过 GitHub 允许的 10 分钟
    assert claims["iat"] == now - 60 and claims["exp"] - claims["iat"] <= 600


async def test_installation_token_is_cached_until_near_expiry():
    fake = FakeGitHub()
    gh = fake.app()
    client = gh.installation(42)
    await client.list_labels(REPO)
    await client.get_permission(REPO, "alice")
    assert fake.tokens_issued == 1

    # 令牌只剩 2 分钟（小于提前刷新的 5 分钟）→ 下一次调用前换新的
    gh._tokens[42].expires_at = time.time() + 120
    await client.get_permission(REPO, "alice")
    assert fake.tokens_issued == 2
    await gh.aclose()


@pytest.mark.parametrize("offset", [-330.0, 600.0])
async def test_clock_skew_is_learned_from_date_header(offset: float):
    """本机快 5 分半（JWT 签发时间在未来）或慢 10 分钟（JWT 已过期）：
    第一次被拒，按服务器时间重签后成功。"""
    fake = FakeGitHub(server_offset=offset)
    gh = fake.app()
    info = await gh.get_app()
    assert info["slug"] == "repowarden-test"
    assert fake.jwt_rejections == 1 and abs(gh._skew - offset) < 3
    # 学到偏差之后，后续的 JWT 一次就过
    await gh.installation(42).list_labels(REPO)
    assert fake.jwt_rejections == 1
    await gh.aclose()


async def test_real_bad_credentials_are_not_retried_forever():
    fake = FakeGitHub()
    gh = fake.app()
    fake.fail("GET", r"^/app$", httpx.Response(401, json={"message": "Bad credentials"}))
    with pytest.raises(GitHubApiError) as e:
        await gh.get_app()
    assert e.value.status == 401
    assert len([r for r in fake.requests if r[1] == "/app"]) == 1
    await gh.aclose()


async def test_primary_rate_limit_waits_until_reset_then_retries():
    fake = FakeGitHub()
    waits: list[float] = []

    async def sleep(s: float) -> None:
        waits.append(s)

    gh = fake.app()
    gh._sleep = sleep
    reset = str(int(time.time()) + 10)
    fake.fail(
        "POST", r"/comments$",
        httpx.Response(
            403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset},
            json={"message": "API rate limit exceeded"},
        ),
    )
    cid = await gh.installation(42).create_comment(ISSUE, "hi")
    assert cid and len(waits) == 1 and 0 < waits[0] <= 12
    await gh.aclose()


async def test_secondary_rate_limit_uses_retry_after():
    fake = FakeGitHub()
    waits: list[float] = []

    async def sleep(s: float) -> None:
        waits.append(s)

    gh = fake.app()
    gh._sleep = sleep
    fake.fail("POST", r"/comments$", httpx.Response(429, headers={"retry-after": "3"}))
    await gh.installation(42).create_comment(ISSUE, "hi")
    assert waits == [3.0]
    await gh.aclose()


async def test_rate_limit_too_long_raises_retryable():
    fake = FakeGitHub()
    gh = fake.app()
    fake.fail("POST", r"/comments$", httpx.Response(429, headers={"retry-after": "3600"}))
    with pytest.raises(GitHubApiError) as e:
        await gh.installation(42).create_comment(ISSUE, "hi")
    assert e.value.retryable
    await gh.aclose()


async def test_forbidden_without_rate_limit_headers_is_not_retryable():
    fake = FakeGitHub()
    gh = fake.app()
    fake.fail(
        "POST", r"/comments$",
        httpx.Response(403, json={"message": "Resource not accessible by integration"}),
    )
    with pytest.raises(GitHubApiError) as e:
        await gh.installation(42).create_comment(ISSUE, "hi")
    assert e.value.status == 403 and not e.value.retryable
    assert "not accessible" in str(e.value)
    await gh.aclose()


async def test_permission_lookup():
    fake = FakeGitHub(permissions={"carol": "maintain", "dave": "triage", "erin": "custom-role"})
    gh = fake.app()
    c = gh.installation(42)
    assert await c.get_permission(REPO, "carol") == "maintain"
    # triage 角色的粗粒度 permission 是 read，不能执行命令
    assert await c.get_permission(REPO, "dave") == "triage"
    # 认不出的自定义角色：退回粗粒度字段
    assert await c.get_permission(REPO, "erin") == "read"
    # 不是协作者：404 → none
    assert await c.get_permission(REPO, "mallory") == "none"
    await gh.aclose()


async def test_labels_paginate_and_cache():
    fake = FakeGitHub(labels=[f"l{i}" for i in range(150)])
    gh = fake.app()
    c = gh.installation(42)
    labels = await c.list_labels(REPO)
    assert len(labels) == 150
    calls = len([r for r in fake.requests if r[1].endswith("/labels")])
    assert calls == 2
    await c.list_labels(REPO)
    assert len([r for r in fake.requests if r[1].endswith("/labels")]) == calls
    await gh.aclose()


async def test_set_labels_adds_and_removes_with_url_encoding():
    fake = FakeGitHub()
    fake.issue_labels[("acme/widgets", 7)] = ["good first issue"]
    gh = fake.app()
    c = gh.installation(42)
    await c.set_labels(ISSUE, add=["bug"], remove=["good first issue", "not-there"])
    assert fake.issue_labels[("acme/widgets", 7)] == ["bug"]
    await gh.aclose()


async def test_find_comment_only_matches_bot_comments_with_marker():
    fake = FakeGitHub()
    fake.comments[("acme/widgets", 7)] = [
        {"id": 1, "body": "<!-- repowarden:summary --> fake", "user": {"type": "User"}},
        {"id": 2, "body": "report\n<!-- repowarden:summary -->", "user": {"type": "Bot"}},
    ]
    gh = fake.app()
    assert await gh.installation(42).find_comment(ISSUE, "<!-- repowarden:summary -->") == "2"
    await gh.aclose()
