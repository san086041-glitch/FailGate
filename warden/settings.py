from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

RepoMode = Literal["shadow", "live", "paused"]


class Settings(BaseSettings):
    """服务端配置，从环境变量或 .env 读取（变量名不区分大小写）。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    warden_db_url: str = "sqlite+aiosqlite:///./warden.db"
    github_webhook_secret: str = ""
    github_app_id: str | None = None
    github_app_private_key_path: str | None = None
    # 新接入的仓库默认进入影子模式：所有对外写操作只记录，不执行
    default_repo_mode: RepoMode = "shadow"

    # LLM（任意 OpenAI 兼容接口）；未配置 API Key 时能力模块不运行，Case 停在 INTAKE
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_model_small: str = "deepseek-flash"
    llm_model_large: str = "deepseek-flash"
    llm_timeout_seconds: float = 60.0

    # 单个 Case 的模型花费上限（美元），超过后进入 FAILED
    case_budget_usd: float = 0.5

    # 查重：召回候选数；LLM 分数 ≥ high 建议关闭为重复，≥ low 列为相关 issue
    dedup_recall_k: int = 8
    dedup_high: float = 0.95  # 依据见 ADR 0003
    dedup_low: float = 0.5
    # 可选的向量通道（任意 OpenAI 兼容 /embeddings 接口），不配置则只用词法和堆栈通道
    embed_base_url: str = ""
    embed_api_key: str = ""
    embed_model: str = ""

    # GitHub REST 只读 token（回填历史 issue 用），公开仓库可以留空
    github_token: str = ""
