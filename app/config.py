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
    # 兩個都必須支援 tool calling(轉真人的 agent 層靠它)。openrouter/free 是
    # 自動路由:請求帶 tools 時只會分到支援工具的模型。2026-09-24 實測一整天,
    # 偏好模型每次都限流,真正在答的一直是它 —— 所以直接讓它當主要。
    openrouter_model: str = "openrouter/free"
    openrouter_fallback_model: str = "nvidia/nemotron-3-super-120b-a12b:free"
    # NVIDIA 自家 API(build.nvidia.com)。選填:有 key 時它當主要模型、OpenRouter
    # 退成備援;沒有(含 compose 傳進來的空字串)就跟以前一樣只走 OpenRouter。
    # 2026-10-05 實測 10 題,super 轉真人 5/5、誤轉 0、中位數 1.5 秒 —— 同一顆
    # 模型在 OpenRouter 的 :free 版 9/24 四次全限流。免費層是每分鐘約 40 次,
    # 沒有每日上限。模型名不帶 :free:那是 OpenRouter 的命名,NVIDIA 不認得。
    nvidia_api_key: str = ""
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_model: str = "nvidia/nemotron-3-super-120b-a12b"
    llm_timeout_seconds: float = 45.0
    # 兩個模型「合計」最多等多久。llm_timeout_seconds 管不到這件事:httpx 的
    # timeout 是兩次讀到資料之間的上限,而 OpenRouter 在生成時會持續送空白
    # 維持連線,每個空白都把它歸零 —— 真機上 45 秒的 timeout 沒觸發,答案
    # 56 秒後才到。25 秒留足餘裕給 LINE reply token 的一分鐘效期與 fallback。
    llm_total_budget_seconds: float = 25.0
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
