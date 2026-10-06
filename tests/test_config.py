import pytest
from pydantic import ValidationError

from app.config import Settings


def test_settings_strip_whitespace_from_secrets():
    """貼上金鑰時黏到的換行與空白必須被清掉。

    HTTP 標頭含換行會被整個丟掉,而上游回的錯誤訊息完全指不到真正原因。
    """
    s = Settings(
        fernet_key="  key-with-spaces  \n",
        openrouter_api_key="sk-or-v1-abc\n",
    )
    assert s.fernet_key == "key-with-spaces"
    assert s.openrouter_api_key == "sk-or-v1-abc"


def test_settings_have_defaults(monkeypatch):
    # 外部環境如果設了 DATABASE_URL(例如 CI 的 test-postgres job、或這裡
    # 對著 PostgreSQL 手動跑測試時),Settings 就不會落回內建預設值 ——
    # 斷言驗到的會是「當下環境變數剛好是什麼」,不是這個測試名稱宣稱要驗的
    # 東西。先清掉,確保驗的是 Settings 本身宣告的預設值。
    monkeypatch.delenv("DATABASE_URL", raising=False)
    s = Settings(fernet_key="k", openrouter_api_key="k")
    assert s.database_url.startswith("sqlite")
    assert s.history_limit == 10
    assert s.line_max_text_length == 5000


def test_default_models_both_support_tool_calling(monkeypatch):
    """z-ai/glm-5.2:free 不支援 tools(OpenRouter 的 supported_parameters 沒有
    tools)。把它設成主要模型的話,每一次請求都會失敗再退回,轉真人的
    agent 層等於只剩一半在工作,而且 log 只看得到「模型失敗」。"""
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_FALLBACK_MODEL", raising=False)
    s = Settings(_env_file=None, fernet_key="k", openrouter_api_key="k")
    assert s.openrouter_model == "openrouter/free"
    assert s.openrouter_fallback_model == "nvidia/nemotron-3-super-120b-a12b:free"


@pytest.mark.parametrize("field", ["rate_limit_user_per_minute", "rate_limit_user_per_day",
                                   "rate_limit_global_per_minute"])
def test_a_negative_rate_limit_is_rejected(field):
    """0 是「關閉」,負數沒有意義 —— 寫錯的設定要在開機時就爆,不是默默變成關閉。"""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, fernet_key="k", openrouter_api_key="k", **{field: -1})
