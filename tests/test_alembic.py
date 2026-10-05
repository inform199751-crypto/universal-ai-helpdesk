"""Alembic 遷移的測試。

其他測試都用 Base.metadata.create_all 建表 —— 驗的是 model,不是遷移檔。
遷移檔寫錯(例如 NOT NULL 欄位忘了 server_default)只會在對著「已經有
資料」的資料庫跑 upgrade 時才爆,也就是部署當下。

只跑 SQLite:對 CI 的 PostgreSQL 測試庫跑 upgrade 會跟其他測試的
create_all / drop_all 互相踩。PostgreSQL 的遷移由部署時 app 容器開機
的 `alembic upgrade head` 驗(計畫 Task 7 Step 4),而那時資料庫裡有真的資料。
"""

import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
INITIAL = "66766e088730"


@pytest.fixture
def alembic_sqlite(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'migrate.db'}"
    # alembic/env.py 讀的是 get_settings().database_url,而它有 lru_cache
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    # 刻意不給 ini 檔路徑:有檔名的話 env.py 會跑 fileConfig(),預設
    # disable_existing_loggers=True,之後的測試抓 log 會莫名其妙抓不到。
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    yield cfg, url
    get_settings.cache_clear()


def _columns(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns("companies")}
    finally:
        engine.dispose()


def test_upgrade_keeps_existing_companies_and_defaults_the_rules_to_empty(alembic_sqlite):
    """companies 已經有資料列時,NOT NULL 欄位沒有 server_default 的話
    ALTER TABLE 會直接失敗 —— 部署時就是這個情況。"""
    cfg, url = alembic_sqlite
    command.upgrade(cfg, INITIAL)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO companies (id, slug, name, industry, line_channel_secret_enc,"
            " line_channel_token_enc, system_prompt, tone, forbidden_phrases,"
            " fallback_message, human_mode_timeout_minutes, vector_collection, is_active)"
            " VALUES ('c1', 'bistro', '微醺', 'restaurant', x'00', x'00', 'p', 't', '[]',"
            " 'f', 30, 'kb_bistro', 1)"))
    engine.dispose()

    command.upgrade(cfg, "head")

    assert {"escalation_rules", "staff_notify_to"} <= _columns(url)
    engine = create_engine(url)
    with engine.connect() as conn:
        rules, staff = conn.execute(text(
            "SELECT escalation_rules, staff_notify_to FROM companies")).one()
    engine.dispose()
    assert json.loads(rules) == []
    assert staff is None


def test_downgrade_removes_both_columns(alembic_sqlite):
    cfg, url = alembic_sqlite
    command.upgrade(cfg, "head")
    command.downgrade(cfg, INITIAL)
    assert not {"escalation_rules", "staff_notify_to"} & _columns(url)
