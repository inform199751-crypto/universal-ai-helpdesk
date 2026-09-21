import os

# 測試一律用記憶體資料庫,而且必須在 import app.* 之前設好
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("FERNET_KEY", "x" * 43 + "=")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")

import pytest  # noqa: E402

from app.database import Base, engine  # noqa: E402


@pytest.fixture
def tmp_engine():
    yield engine
    Base.metadata.drop_all(engine)
