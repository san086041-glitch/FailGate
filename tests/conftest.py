from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from fake_llm import FakeLLM

from failgate.app import FailGate, create_app
from failgate.settings import Settings

SECRET = "test-secret"
REPO = "acme/widgets"


@dataclass
class Harness:
    failgate: FailGate
    client: httpx.AsyncClient
    llm: FakeLLM

    async def send(
        self, event: str, payload: dict[str, Any], delivery: str, *, secret: str = SECRET
    ) -> httpx.Response:
        body = json.dumps(payload).encode()
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return await self.client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Event": event,
                "X-GitHub-Delivery": delivery,
                "X-Hub-Signature-256": sig,
                "Content-Type": "application/json",
            },
        )


def make_settings(tmp_path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "failgate_db_url": f"sqlite+aiosqlite:///{(tmp_path / 'failgate.db').as_posix()}",
        "github_webhook_secret": SECRET,
        "default_repo_mode": "shadow",
        "llm_api_key": "test-key",
        "llm_base_url": "http://llm.test",
        "llm_model_small": "deepseek-flash",
        "case_budget_usd": 0.5,
    }
    values.update(overrides)
    # _env_file=None：测试不读取开发者本地的 .env
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


# 没配置 App 时的只读 REST 后备（读公开仓库的评论）：测试里绝不能访问真实网络
PUBLIC_COMMENTS: dict[int, list[dict[str, Any]]] = {}


def _public_rest(request: httpx.Request) -> httpx.Response:
    parts = request.url.path.split("/")
    if len(parts) >= 7 and parts[-1] == "comments":
        return httpx.Response(200, json=PUBLIC_COMMENTS.get(int(parts[-2]), []))
    return httpx.Response(404, json={"message": "not stubbed"})


async def _harness(
    settings: Settings, github_app: Any = None, repro_runner: Any = None,
    verify_runner: Any = None,
) -> AsyncIterator[Harness]:
    llm = FakeLLM()
    PUBLIC_COMMENTS.clear()
    app = create_app(
        settings,
        run_worker=False,
        llm_transport=llm.transport,
        github_app=github_app,
        rest_transport=httpx.MockTransport(_public_rest),
        repro_runner=repro_runner,
        verify_runner=verify_runner,
    )
    failgate: FailGate = app.state.failgate
    await failgate.start(run_worker=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield Harness(failgate, client, llm)
    await failgate.stop()


@pytest.fixture
async def harness(tmp_path) -> AsyncIterator[Harness]:
    async for h in _harness(make_settings(tmp_path)):
        yield h


@pytest.fixture
async def harness_no_llm(tmp_path) -> AsyncIterator[Harness]:
    async for h in _harness(make_settings(tmp_path, llm_api_key="")):
        yield h


def user(login: str, *, bot: bool = False) -> dict[str, Any]:
    return {"login": login, "type": "Bot" if bot else "User"}


def issue_event(
    action: str,
    number: int = 1,
    *,
    author: str = "alice",
    sender: str | None = None,
    association: str = "NONE",
    bot: bool = False,
) -> dict[str, Any]:
    return {
        "action": action,
        "issue": {
            "number": number,
            "title": "KeyError when reading parquet",
            "body": "Traceback ...",
            "user": user(author, bot=bot),
            "author_association": association,
        },
        "repository": {"full_name": REPO},
        "installation": {"id": 42},
        "sender": user(sender or author, bot=bot),
    }


def comment_event(
    body: str,
    number: int = 1,
    *,
    login: str = "bob",
    association: str = "NONE",
    on_pull: bool = False,
) -> dict[str, Any]:
    issue: dict[str, Any] = {"number": number, "user": user("alice")}
    if on_pull:
        issue["pull_request"] = {"url": "https://api.github.com/..."}
    return {
        "action": "created",
        "issue": issue,
        "comment": {"body": body, "user": user(login), "author_association": association},
        "repository": {"full_name": REPO},
        "sender": user(login),
    }
