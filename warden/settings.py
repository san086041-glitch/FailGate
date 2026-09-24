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
