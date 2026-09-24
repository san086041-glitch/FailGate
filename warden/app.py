"""应用装配：Warden 容器持有所有长生命周期组件，FastAPI 只是它的一个入口。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from warden import __version__
from warden.api import router as api_router
from warden.db import Database, Repo
from warden.index.docs import DocIndex
from warden.index.embed import Embedder
from warden.index.store import IssueIndex
from warden.ingress.webhooks import router as webhook_router
from warden.llm import LLMClient
from warden.orchestrator.machine import CaseMachine
from warden.orchestrator.pipeline import Pipeline
from warden.orchestrator.states import CaseState
from warden.orchestrator.worker import EventQueue, Worker
from warden.platforms.base import (
    CaseKind,
    CaseRef,
    Comment,
    DomainEvent,
    Platform,
    PlatformWriter,
    RepoRef,
)
from warden.platforms.github import GitHubPlatform
from warden.platforms.github_app import GitHubApp, InstallationClient, comment_from_api
from warden.platforms.github_rest import GitHubRest
from warden.policy.executor import EffectExecutor
from warden.policy.gate import PolicyGate
from warden.settings import Settings
from warden.skills.answer import AnswerSkill
from warden.skills.base import CommentSource
from warden.skills.dedup import DedupSkill
from warden.skills.intake import IntakeSkill
from warden.skills.triage import TriageSkill

log = logging.getLogger(__name__)


def build_llm(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> LLMClient | None:
    if not settings.llm_api_key:
        return None
    return LLMClient(
        settings.llm_base_url,
        settings.llm_api_key,
        timeout=settings.llm_timeout_seconds,
        transport=transport,
    )


def build_embedder(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> Embedder | None:
    if not (settings.embed_base_url and settings.embed_model):
        return None
    return Embedder(
        settings.embed_base_url, settings.embed_api_key, settings.embed_model, transport=transport
    )


def build_github_app(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> GitHubApp | None:
    if not (settings.github_app_id and settings.github_app_private_key_path):
        return None
    return GitHubApp.from_key_file(
        settings.github_app_id,
        settings.github_app_private_key_path,
        base_url=settings.github_api_url,
        transport=transport,
    )


class Warden:
    def __init__(
        self,
        settings: Settings,
        *,
        llm_transport: httpx.AsyncBaseTransport | None = None,
        embed_transport: httpx.AsyncBaseTransport | None = None,
        github_app: GitHubApp | None = None,
        rest_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.github_app = github_app or build_github_app(settings)
        self.db = Database(settings.warden_db_url)
        self.platforms: dict[str, Platform] = {
            "github": GitHubPlatform(settings.github_webhook_secret),
        }
        self.queue: EventQueue = asyncio.Queue()
        self.gate = PolicyGate()
        self.embedder = build_embedder(settings, embed_transport)
        self.index = IssueIndex(self.db, self.embedder)
        self.docs = DocIndex(self.db)
        # 没有 App 时的只读后备：用 GITHUB_TOKEN（或匿名）读公开仓库的评论
        self.rest = GitHubRest(
            settings.github_token, base_url=settings.github_api_url, transport=rest_transport
        )
        self.machine = CaseMachine(
            self.db,
            default_mode=settings.default_repo_mode,
            index=self.index,
            permissions=self._permission if self.github_app else None,
        )
        self.llm = build_llm(settings, llm_transport)
        self.pipeline = (
            Pipeline(
                self.db,
                self.machine,
                self.gate,
                self.llm,
                {
                    CaseState.INTAKE: (IntakeSkill(), settings.llm_model_small),
                    CaseState.TRIAGING: (TriageSkill(), settings.llm_model_small),
                    CaseState.DEDUPING: (
                        DedupSkill(
                            recall_k=settings.dedup_recall_k,
                            high=settings.dedup_high,
                            low=settings.dedup_low,
                        ),
                        settings.llm_model_small,
                    ),
                    CaseState.ANSWERING: (AnswerSkill(), settings.llm_model_large),
                },
                case_budget_usd=settings.case_budget_usd,
                index=self.index,
                labels=self._labels if self.github_app else None,
                docs=self.docs,
                comments=self._comments_for,
            )
            if self.llm is not None
            else None
        )
        self.executor = EffectExecutor(self.db, self._writer)
        self.worker = Worker(self.queue, self.machine, self.pipeline, self.executor)
        self._tasks: list[asyncio.Task[None]] = []

    # ---- 平台读写的装配：只有 GitHub 且拿到了安装 ID 才能调用 ----

    def _client(self, platform: str, installation_id: int | None) -> InstallationClient | None:
        if self.github_app is None or platform != "github" or not installation_id:
            return None
        return self.github_app.installation(installation_id)

    def _writer(self, repo: Repo) -> PlatformWriter | None:
        return self._client(repo.platform, repo.installation_id)

    async def _labels(self, repo: Repo) -> tuple[str, ...] | None:
        client = self._client(repo.platform, repo.installation_id)
        if client is None:
            return None
        labels = await client.list_labels(RepoRef(platform=repo.platform, full_name=repo.full_name))
        return tuple(label.name for label in labels)

    def _comments_for(self, repo: Repo) -> CommentSource | None:
        if repo.platform != "github":
            return None
        client = self._client(repo.platform, repo.installation_id)

        async def via_app(full_name: str, number: int) -> list[Comment]:
            assert client is not None
            ref = CaseRef(
                repo=RepoRef(platform="github", full_name=full_name),
                kind=CaseKind.ISSUE,
                number=number,
            )
            return await client.list_comments(ref)

        async def via_rest(full_name: str, number: int) -> list[Comment]:
            return [comment_from_api(c) for c in await self.rest.list_comments(full_name, number)]

        return via_app if client is not None else via_rest

    async def _permission(self, event: DomainEvent) -> str | None:
        client = self._client(event.platform, event.installation_id)
        if client is None:
            return None
        return await client.get_permission(event.repo, event.actor.login)

    async def start(self, *, run_worker: bool = True) -> None:
        await self.db.create_all()
        if not self.settings.github_webhook_secret:
            log.warning("GITHUB_WEBHOOK_SECRET 未设置：所有 GitHub webhook 都会被拒绝")
        if self.llm is None:
            log.warning("LLM_API_KEY 未设置：能力模块不会运行，新 issue 会停在 INTAKE")
        if self.github_app is None:
            log.warning("GitHub App 未配置：正常模式下的写操作会停在 pending，不会真正发出")
        if run_worker:
            self._tasks.append(asyncio.create_task(self.worker.run_forever()))
            self._tasks.append(
                asyncio.create_task(
                    self.executor.run_forever(self.settings.effect_retry_interval_seconds)
                )
            )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self.github_app is not None:
            await self.github_app.aclose()
        await self.rest.aclose()
        if self.llm is not None:
            await self.llm.aclose()
        if self.embedder is not None:
            await self.embedder.aclose()
        await self.db.dispose()


def create_app(
    settings: Settings | None = None,
    *,
    run_worker: bool = True,
    llm_transport: httpx.AsyncBaseTransport | None = None,
    embed_transport: httpx.AsyncBaseTransport | None = None,
    github_app: GitHubApp | None = None,
    rest_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    warden = Warden(
        settings or Settings(),
        llm_transport=llm_transport,
        embed_transport=embed_transport,
        github_app=github_app,
        rest_transport=rest_transport,
    )

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
