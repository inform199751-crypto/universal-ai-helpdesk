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
