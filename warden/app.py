"""应用装配：Warden 容器持有所有长生命周期组件，FastAPI 只是它的一个入口。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from warden import __version__
from warden.api import router as api_router
from warden.db import Database
from warden.ingress.webhooks import router as webhook_router
from warden.orchestrator.machine import CaseMachine
from warden.orchestrator.worker import EventQueue, Worker
from warden.platforms.base import Platform
from warden.platforms.github import GitHubPlatform
from warden.policy.gate import PolicyGate
from warden.settings import Settings

log = logging.getLogger(__name__)


class Warden:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Database(settings.warden_db_url)
        self.platforms: dict[str, Platform] = {
            "github": GitHubPlatform(settings.github_webhook_secret),
        }
        self.queue: EventQueue = asyncio.Queue()
        self.gate = PolicyGate()
        self.machine = CaseMachine(self.db, self.gate, default_mode=settings.default_repo_mode)
        self.worker = Worker(self.queue, self.machine)
        self._worker_task: asyncio.Task[None] | None = None

    async def start(self, *, run_worker: bool = True) -> None:
        await self.db.create_all()
        if not self.settings.github_webhook_secret:
            log.warning("GITHUB_WEBHOOK_SECRET 未设置：所有 GitHub webhook 都会被拒绝")
        if run_worker:
            self._worker_task = asyncio.create_task(self.worker.run_forever())

    async def stop(self) -> None:
        if self._worker_task is not None:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
        await self.db.dispose()


def create_app(settings: Settings | None = None, *, run_worker: bool = True) -> FastAPI:
    warden = Warden(settings or Settings())

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await warden.start(run_worker=run_worker)
        yield
        await warden.stop()

    app = FastAPI(title="RepoWarden", version=__version__, lifespan=lifespan)
    app.state.warden = warden
    app.include_router(webhook_router)
    app.include_router(api_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app
