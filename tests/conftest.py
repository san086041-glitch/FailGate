from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from warden.app import Warden, create_app
from warden.settings import Settings

SECRET = "test-secret"
REPO = "acme/widgets"


@dataclass
class Harness:
    warden: Warden
    client: httpx.AsyncClient

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


@pytest.fixture
async def harness(tmp_path) -> AsyncIterator[Harness]:
    settings = Settings(
        warden_db_url=f"sqlite+aiosqlite:///{(tmp_path / 'warden.db').as_posix()}",
        github_webhook_secret=SECRET,
        default_repo_mode="shadow",
    )
    app = create_app(settings, run_worker=False)
    warden: Warden = app.state.warden
    await warden.start(run_worker=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield Harness(warden, client)
    await warden.stop()


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
