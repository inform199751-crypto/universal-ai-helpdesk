import os

# 測試一律用記憶體資料庫,而且必須在 import app.* 之前設好
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("FERNET_KEY", "x" * 43 + "=")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
# 強制清空,不是 setdefault:Settings 會讀專案根目錄的 .env,開發者本機有
# NVIDIA key 時,所有測試的第一個請求都會改打 NVIDIA —— 本機紅、CI 綠。
# 環境變數的優先權高於 .env,設成空字串就蓋得掉。
os.environ["NVIDIA_API_KEY"] = ""
# 分類層在測試裡預設關掉:它跟主模型同時呼叫 complete,既有測試的替身是「依序回應」
# 與「算呼叫次數」,多一個呼叫者就會亂掉;而且沒擋的話會打真的 HTTP。
# 要測它的測試自己打開(見 tests/test_webhook.py 的 classifier fixture)。
os.environ["HANDOFF_CLASSIFIER_ENABLED"] = "false"

import pytest  # noqa: E402

from app.database import Base, engine  # noqa: E402


@pytest.fixture
def tmp_engine():
    yield engine
    Base.metadata.drop_all(engine)
