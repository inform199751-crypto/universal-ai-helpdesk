"""全域設定。所有值都從環境變數或 .env 讀入。"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
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
    # 2026-10-05 實測 10 題(當時思考是開的):super 轉真人 5/5、中位數 1.5 秒;ultra 5/5、3.4 秒。
    # 2026-10-27 起 super 在 NVIDIA 回 404(/v1/models 還列著它),預設改成 ultra。
    # 免費層是每分鐘約 40 次(10/16 下午實測二十幾次就 429),沒有每日上限。
    # 模型名不帶 :free:那是 OpenRouter 的命名,NVIDIA 不認得。
    nvidia_api_key: str = ""
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_model: str = "nvidia/nemotron-3-ultra-550b-a55b"
    # 預設關掉思考。2026-10-08 實測:預約說明很長,開著思考時開放式問題(「我想訂位」)
    # 5 次裡 3 次把 max_tokens 1200 全拿去想、content 是空的(11~37 秒);關掉後
    # 5/5 在 0.9~3 秒內答出來。只送給 NVIDIA,OpenRouter 不帶這個參數。
    nvidia_enable_thinking: bool = False
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

    # 速率限制(設計見 docs/superpowers/specs/2026-10-05-rate-limit-design.md)。
    # 0 = 關閉那一條。全站每分鐘 30 留 10 的餘裕在 NVIDIA 免費層的每分鐘約 40 次以下 ——
    # 超過的話 NVIDIA 回 429,全部退到 OpenRouter,一天 50 次幾分鐘就燒光。
    # 這個估算假設一則訊息呼叫一次模型。有了 agent loop,預約的訊息最多要 4 次,尖峰時
    # 會超過 NVIDIA 的每分鐘約 40 次 —— 2026-10-06 的預約 spec(決策 9)接受這點:
    # 多數訊息仍是 1 次,429 時退到 OpenRouter。
    rate_limit_user_per_minute: int = Field(5, ge=0)
    rate_limit_user_per_day: int = Field(20, ge=0)
    rate_limit_global_per_minute: int = Field(30, ge=0)

    # agent loop(設計見 docs/superpowers/specs/2026-10-06-booking-agent-design.md 第六節)。
    # 最後一步跟前面一樣只給工具規格、不送 tool_choice;
    # 模型仍回工具呼叫時照樣執行、不再問模型(見 loop.py 檔頭)。
    # 45 秒是整個 loop 合計:LINE reply token 約一分鐘有效,留 15 秒給送出與備援。
    # llm_total_budget_seconds 保留給單次呼叫。
    agent_max_steps: int = Field(4, ge=1)
    agent_total_budget_seconds: float = Field(45.0, gt=0)

    # 轉真人分類層(設計見 docs/superpowers/specs/2026-10-16-handoff-classifier-design.md)。
    # 關鍵字沒命中時,跟主模型同時發一個只看升級規則的短呼叫;線上出問題時設成 false
    # 重啟就關掉,不用改程式。15 秒從送出算起:主模型先答完時最多再等到這裡,
    # 最壞情況仍在 agent 的 45 秒內。
    handoff_classifier_enabled: bool = True
    handoff_classifier_budget_seconds: float = Field(15.0, gt=0)

    @field_validator("*", mode="before")
    @classmethod
    def _strip(cls, v):
        # 金鑰貼上時很容易黏到尾端換行。HTTP 標頭含換行會被整個丟掉,
        # 而上游回的錯誤訊息完全指不到真正原因 —— 所以在入口就清掉。
        return v.strip() if isinstance(v, str) else v


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
