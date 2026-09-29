"""应用装配：FailGate 容器持有所有长生命周期组件，FastAPI 只是它的一个入口。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from failgate import __version__
from failgate.api import router as api_router
from failgate.db import Database, Repo
from failgate.index.docs import DocIndex
from failgate.index.embed import Embedder
from failgate.index.store import IssueIndex
from failgate.ingress.webhooks import router as webhook_router
from failgate.llm import LLMClient
from failgate.orchestrator.machine import CaseMachine
from failgate.orchestrator.pipeline import Pipeline
from failgate.orchestrator.states import CaseState
from failgate.orchestrator.worker import EventQueue, Worker
from failgate.platforms.base import (
    CaseKind,
    CaseRef,
    Comment,
    DomainEvent,
    Label,
    Platform,
    PlatformWriter,
    RepoRef,
)
from failgate.platforms.github import GitHubPlatform
from failgate.platforms.github_app import GitHubApp, InstallationClient, comment_from_api
from failgate.platforms.github_rest import GitHubRest
from failgate.policy.executor import EffectExecutor
from failgate.policy.gate import PolicyGate
from failgate.settings import Settings
from failgate.skills.answer import AnswerSkill
from failgate.skills.base import CommentSource, Skill
from failgate.skills.dedup import DedupSkill
from failgate.skills.intake import IntakeSkill
from failgate.skills.repro import ReproRunner, ReproSkill, SandboxReproRunner
from failgate.skills.triage import TriageSkill
from failgate.skills.verify import (
    ResealSkill,
    SandboxVerifyRunner,
    VerifyRunner,
    VerifySkill,
)

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


class FailGate:
    def __init__(
        self,
        settings: Settings,
        *,
        llm_transport: httpx.AsyncBaseTransport | None = None,
        embed_transport: httpx.AsyncBaseTransport | None = None,
        github_app: GitHubApp | None = None,
        rest_transport: httpx.AsyncBaseTransport | None = None,
        repro_runner: ReproRunner | None = None,
        verify_runner: VerifyRunner | None = None,
    ) -> None:
        self.settings = settings
        self.github_app = github_app or build_github_app(settings)
        self.db = Database(settings.failgate_db_url)
        self.platforms: dict[str, Platform] = {
            "github": GitHubPlatform(settings.github_webhook_secret),
        }
        self.queue: EventQueue = asyncio.Queue()
        self.gate = PolicyGate()
        self.embedder = build_embedder(settings, embed_transport)
        self.index = IssueIndex(self.db, self.embedder)
        self.docs = DocIndex(self.db, self.embedder)
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
        self.repro_runner = self._build_repro_runner(settings, repro_runner)
        # PR 核验和复现用同一个开关：都要 Docker，也都依赖复现产出的考卷
        self.verify_runner: VerifyRunner | None = verify_runner or (
            SandboxVerifyRunner(settings, self.db) if settings.repro_enabled else None
        )
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
                    **self._repro_stage(settings),
                    **self._verify_stage(),
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
        self.worker = Worker(self.queue, self.machine, self.pipeline, self.executor, db=self.db)
        self._tasks: list[asyncio.Task[None]] = []

    def _build_repro_runner(
        self, settings: Settings, injected: ReproRunner | None
    ) -> ReproRunner | None:
        """复现阶段需要 LLM；总开关 REPRO_ENABLED 打开（或测试注入了 runner）才启用。"""
        if injected is not None:
            return injected
        if settings.repro_enabled and self.llm is not None:
            return SandboxReproRunner(settings, self.llm)
        return None

    def _repro_stage(self, settings: Settings) -> dict[CaseState, tuple[Skill, str]]:
        if self.repro_runner is None:
            return {}
        skill = ReproSkill(self.repro_runner, max_budget_usd=settings.repro_budget_usd,
                           hidden_exam=settings.hidden_exam_enabled)
        return {CaseState.REPRODUCING: (skill, settings.llm_model_large)}

    def _verify_stage(self) -> dict[CaseState, tuple[Skill, str]]:
        if self.verify_runner is None:
            return {}
        return {
            CaseState.VERIFYING: (VerifySkill(self.verify_runner), "-"),
            CaseState.RESEALING: (ResealSkill(self.verify_runner), "-"),
        }

    # ---- 平台读写的装配：只有 GitHub 且拿到了安装 ID 才能调用 ----

    def _client(self, platform: str, installation_id: int | None) -> InstallationClient | None:
        if self.github_app is None or platform != "github" or not installation_id:
            return None
        return self.github_app.installation(installation_id)

    def _writer(self, repo: Repo) -> PlatformWriter | None:
        return self._client(repo.platform, repo.installation_id)

    async def _labels(self, repo: Repo) -> list[Label] | None:
        client = self._client(repo.platform, repo.installation_id)
        if client is None:
            return None
        return await client.list_labels(RepoRef(platform=repo.platform, full_name=repo.full_name))

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
        if isinstance(self.repro_runner, SandboxReproRunner):
            await self.repro_runner.aclose()
        if isinstance(self.verify_runner, SandboxVerifyRunner):
            await self.verify_runner.aclose()
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
    repro_runner: ReproRunner | None = None,
    verify_runner: VerifyRunner | None = None,
) -> FastAPI:
    failgate = FailGate(
        settings or Settings(),
        llm_transport=llm_transport,
        embed_transport=embed_transport,
        github_app=github_app,
        rest_transport=rest_transport,
        repro_runner=repro_runner,
        verify_runner=verify_runner,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await failgate.start(run_worker=run_worker)
        yield
        await failgate.stop()

    app = FastAPI(title="FailGate", version=__version__, lifespan=lifespan)
    app.state.failgate = failgate
    app.include_router(webhook_router)
    app.include_router(api_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app
