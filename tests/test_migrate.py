"""搬遷腳本的測試。

scripts/ 不是套件,所以用檔案路徑載入(跟 test_dev_script.py 同一套做法)。

真正要守的是時區那一條:它壞掉的時候不會報錯,只是整批時間差八小時,
而且要等到有人去看對話紀錄的時間戳才會發現。
"""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select

from app.crypto import encrypt
from app.database import Base
from app.models import Company

ROOT = Path(__file__).resolve().parents[1]


def _load_migrate():
    spec = importlib.util.spec_from_file_location(
        "migrate", ROOT / "scripts" / "migrate_sqlite_to_postgres.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


migrate = _load_migrate()


def test_naive_datetimes_are_marked_utc():
    """SQLite 讀出來的 datetime 是 naive 的。直接塞進 PostgreSQL 的
    timestamptz,PG 會拿伺服器時區去解讀 —— 容器裡是 UTC、筆電是 UTC+8,
    整批差八小時,而且不會有任何錯誤訊息。"""
    naive = datetime(2026, 9, 22, 1, 30, 0)
    assert migrate.as_utc(naive) == datetime(2026, 9, 22, 1, 30, tzinfo=timezone.utc)


def test_aware_datetimes_are_left_alone():
    aware = datetime(2026, 9, 22, 1, 30, tzinfo=timezone.utc)
    assert migrate.as_utc(aware) is aware


def test_non_datetime_values_pass_through():
    assert migrate.as_utc("Ubot0001") == "Ubot0001"
    assert migrate.as_utc(None) is None
    assert migrate.as_utc(b"\x01\x02") == b"\x01\x02"


def _seed_source(url: str) -> None:
    engine = create_engine(url, future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(Base.metadata.tables["companies"].insert(), {
            "id": "11111111-1111-1111-1111-111111111111",
            "slug": "bistro", "name": "微醺之夜", "industry": "restaurant",
            "line_channel_id": None,
            "line_channel_secret_enc": encrypt("chan-secret"),
            "line_channel_token_enc": encrypt("chan-token"),
            "line_destination": "Ubot0001",
            "system_prompt": "你是微醺之夜的客服", "tone": "專業親切",
            "forbidden_phrases": [], "fallback_message": "再問一次",
            "human_mode_timeout_minutes": 30, "vector_collection": "kb_bistro",
            "is_active": True,
            "created_at": datetime(2026, 9, 21, 3, 0, 0),
            "updated_at": datetime(2026, 9, 21, 3, 0, 0),
        })
    engine.dispose()


def test_copies_rows_with_the_ciphertext_untouched(tmp_path):
    """密文照搬不解密。解密再加密等於讓明文金鑰多在記憶體裡出現一次,
    沒有任何好處 —— 而且 FERNET_KEY 不變的話密文本來就還解得開。

    這個測試刻意搬 SQLite → SQLite,只驗「列有沒有完整搬過去」。
    時區正確性由上面那三個 as_utc 的單元測試負責 —— SQLAlchemy 的
    SQLite 方言寫入時會把 tzinfo 丟掉,在這裡斷言時區只會驗到 SQLite
    的行為,驗不到我們的。不要因為這裡沒斷言時區就以為漏測了。
    """
    src = f"sqlite:///{tmp_path / 'source.db'}"
    dst = f"sqlite:///{tmp_path / 'target.db'}"
    _seed_source(src)

    target = create_engine(dst, future=True)
    Base.metadata.create_all(target)   # 模擬 Alembic 已經建好表

    counts = migrate.copy_all(src, dst)
    assert counts["companies"] == 1

    with target.connect() as conn:
        row = conn.execute(select(Company.__table__)).one()
    assert row.slug == "bistro"
    from app.crypto import decrypt
    assert decrypt(row.line_channel_secret_enc) == "chan-secret"


def test_refuses_when_the_target_has_no_tables(tmp_path):
    """建表是 Alembic 的職責。讓這支腳本也能建,就是同一件事有兩個
    真相來源 —— 兩邊總有一天不一致,而且會在「搬完之後某個欄位
    莫名不存在」的時候才發現。"""
    src = f"sqlite:///{tmp_path / 'source.db'}"
    dst = f"sqlite:///{tmp_path / 'empty.db'}"
    _seed_source(src)

    with pytest.raises(SystemExit) as exc:
        migrate.copy_all(src, dst)
    assert "alembic" in str(exc.value).lower()


def test_refuses_to_overwrite_a_non_empty_target_without_force(tmp_path):
    src = f"sqlite:///{tmp_path / 'source.db'}"
    dst = f"sqlite:///{tmp_path / 'target.db'}"
    _seed_source(src)
    _seed_source(dst)   # 目標也有資料

    with pytest.raises(SystemExit) as exc:
        migrate.copy_all(src, dst)
    assert "--force" in str(exc.value)


def test_dry_run_reports_counts_without_writing(tmp_path):
    src = f"sqlite:///{tmp_path / 'source.db'}"
    dst = f"sqlite:///{tmp_path / 'target.db'}"
    _seed_source(src)
    target = create_engine(dst, future=True)
    Base.metadata.create_all(target)

    counts = migrate.copy_all(src, dst, dry_run=True)
    assert counts["companies"] == 1

    with target.connect() as conn:
        assert conn.execute(select(Company.__table__)).all() == []
