from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

RepoMode = Literal["shadow", "live", "paused"]


class Settings(BaseSettings):
    """服务端配置，从环境变量或 .env 读取（变量名不区分大小写）。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # WARDEN_DB_URL 是改名前的变量名，照样认；两个都设了以 FAILGATE_DB_URL 为准
    failgate_db_url: str = Field(
        "sqlite+aiosqlite:///./failgate.db",
        validation_alias=AliasChoices("failgate_db_url", "warden_db_url"),
    )
    github_webhook_secret: str = ""
    # GitHub App：两项都配置了才会真正调用写接口；否则 pending 的动作只会积压
    github_app_id: str | None = None
    github_app_private_key_path: str | None = None
    github_api_url: str = "https://api.github.com"
    # 定时补偿：每隔多少秒重试一次 pending 的写操作
    effect_retry_interval_seconds: float = 60.0
    # 新接入的仓库默认进入影子模式：所有对外写操作只记录，不执行
    default_repo_mode: RepoMode = "shadow"

    # 队列（W6，ADR 0023）：events 车道跑快阶段，sandbox 车道跑复现 / 核验
    # local = 进程内 asyncio.Queue（重启丢任务，只能单进程）；redis = arq + Redis
    queue_backend: Literal["local", "redis"] = "local"
    redis_url: str = "redis://127.0.0.1:6379/0"
    # 快阶段全是等 LLM / GitHub 的网络调用，几个并发就够；沙箱任务吃 CPU 和内存，默认 1 个
    events_concurrency: int = 4
    sandbox_concurrency: int = 1
    # serve 进程里跑哪几条车道（逗号分隔；空 = 只收 webhook，worker 另起 `failgate worker`）
    worker_lanes: str = "events,sandbox"
    events_job_timeout_seconds: int = 600
    # 链路追踪（ADR 0025）：none 不导出；console 打到标准输出；otlp 发到 OTLP/HTTP 后端
    tracing_exporter: Literal["none", "console", "otlp"] = "none"
    otel_service_name: str = "failgate"
    # 显式的 OTLP traces 地址和请求头（"k=v,k2=v2"）；不填时按下面 Langfuse 的三项拼
    otlp_endpoint: str = ""
    otlp_headers: str = ""
    # Langfuse 控制台给的配置片段里叫 LANGFUSE_BASE_URL（新版 SDK 的名字），两个都认
    langfuse_host: str = Field(
        "https://cloud.langfuse.com",
        validation_alias=AliasChoices("langfuse_host", "langfuse_base_url"),
    )
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    # 是否把 prompt、回答、skill 输入输出、容器输出放进 span。默认不放（sandbox 仓库是私有的）；
    # TRACING_CAPTURE_CONTENT=true 对所有仓库打开，TRACING_CAPTURE_REPOS 只对列出的仓库打开
    # （逗号分隔的 owner/name，* = 全部），比如只对公开的演示仓库打开
    tracing_capture_content: bool = False
    tracing_capture_repos: str = ""
    # 也是 worker 崩溃后 arq 重投的等待时间（任务超时 + 10 秒）：别设太长
    sandbox_job_timeout_seconds: int = 1200

    # LLM（任意 OpenAI 兼容接口）；未配置 API Key 时能力模块不运行，Case 停在 INTAKE
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_model_small: str = "deepseek-flash"
    llm_model_large: str = "deepseek-flash"
    # 推理模型想得久：查重评委（思考 high）146 次里 4 次连续 3 次超过 60 秒而失败，
    # 每次超时重试都要从头再想一遍（ADR 0026）
    llm_timeout_seconds: float = 180.0

    # 单个 Case 的模型花费上限（美元），超过后进入 FAILED
    case_budget_usd: float = 0.5

    # 查重：召回候选数；LLM 分数 ≥ high 建议关闭为重复，≥ low 列为相关 issue
    dedup_recall_k: int = 20  # 从 8 改为 20：向量召回下前 20 名覆盖 79% 的真实重复，见 ADR 0007
    # 查重评委的思考模式（ADR 0026）：空 = 服务方默认；disabled / low / high / max
    dedup_thinking: Literal["", "disabled", "low", "high", "max"] = ""
    dedup_high: float = 0.95  # 依据见 ADR 0003
    dedup_low: float = 0.5
    # 可选的向量通道（任意 OpenAI 兼容 /embeddings 接口），不配置则只用词法和堆栈通道
    embed_base_url: str = ""
    embed_api_key: str = ""
    embed_model: str = ""

    # GitHub REST 只读 token（回填历史 issue 用），公开仓库可以留空
    github_token: str = ""

    # 复现沙箱（技术方案 8.4 节）。docker_bin 留空时自动查找（含 Windows 按用户安装的路径）
    docker_bin: str = ""
    sandbox_image: str = "python:3.12-slim"
    sandbox_memory: str = "4g"
    sandbox_cpus: float = 2.0
    sandbox_run_timeout_seconds: int = 120
    # install 阶段的网络。生产环境应换成只放行包源的 egress 代理网络（failgate-egress）
    sandbox_install_network: str = "bridge"
    sandbox_artifacts_dir: str = "./artifacts"
    # 环境缓存（package 模式装好的环境 commit 成镜像）的总大小上限，超过按 LRU 删除
    sandbox_env_cache_gb: float = 20.0
    # PyPI JSON API（查版本）和 pip 镜像源（装包，留空用 pip 默认）
    pypi_url: str = "https://pypi.org"
    pip_index_url: str = ""
    # 复现的总开关：打开后，配置了包名（failgate repo repro）的仓库里被分诊为 bug 的 issue
    # 会进入 REPRODUCING。需要 Docker，默认关闭
    repro_enabled: bool = False
    # 复现 Agent（技术方案 8.5 节）：工具调用步数、提交次数、单次复现的模型花费上限（美元）
    repro_max_steps: int = 40
    repro_max_attempts: int = 4
    repro_budget_usd: float = 0.5
    # 考卷强度（技术方案 9.5 节）：核验第一层通过后，对修复改动过、考卷执行到的行做变异测试。
    # 只附加在报告里，不改变结论；每个 PR 多建一个带 coverage 的环境、最多跑这么多个变异体
    verify_strength: bool = True
    strength_max_mutants: int = 30
    # 隐藏考卷（ADR 0021）：封存 L2 考卷时自动出几道变体题（一次 LLM 调用 + 两次沙箱运行），
    # 核验时提示"疑似只迎合了公开考卷"；只公布题数和哈希
    hidden_exam_enabled: bool = True
