"""全域設定。所有值都從環境變數或 .env 讀入。"""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./helpdesk.db"

    # 憑證加密主金鑰(Fernet,base64 44 字元)
    fernet_key: str

    # LLM
    openrouter_api_key: str
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "nvidia/nemotron-3-super-120b-a12b:free"
    # 偏好模型滿載時改用這個。openrouter/free 是自動路由 —— OpenRouter
    # 會自己挑一個當下活著的免費模型。免費供應商滿載是常態,不是例外。
    openrouter_fallback_model: str = "openrouter/free"
    llm_timeout_seconds: float = 45.0
    llm_max_tokens: int = 1200

    # 對話
    history_limit: int = 10
    line_max_text_length: int = 5000

    @field_validator("*", mode="before")
    @classmethod
    def _strip(cls, v):
        # 金鑰貼上時很容易黏到尾端換行。HTTP 標頭含換行會被整個丟掉,
        # 而上游回的錯誤訊息完全指不到真正原因 —— 所以在入口就清掉。
        return v.strip() if isinstance(v, str) else v


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
