"""搬遷腳本的測試。

scripts/ 不是套件,所以用檔案路徑載入(跟 test_dev_script.py 同一套做法)。

真正要守的是時區那一條:它壞掉的時候不會報錯,只是整批時間差八小時,
而且要等到有人去看對話紀錄的時間戳才會發現。
"""

import importlib.util
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select

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

# chat_histories 是四張表裡唯一自增整數主鍵的一張(其他三張是 UUID,見
# app/models/chat.py)。這一類 bug 在 SQLite 上不可能重現 —— SQLite 沒有
# pg_get_serial_sequence/sequence 物件,序號推進那段程式碼在 SQLite 上
# 根本不會執行(copy_all 自己會用 dialect.name 跳過)。
POSTGRES_ONLY = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "sqlite://").startswith("postgresql"),
    reason="PostgreSQL 專屬:SQLite 沒有 sequence 物件,這個 bug 在那裡不可能重現",
)


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


def _seed_chat_history_source(url: str) -> None:
    """在 _seed_source 之外多塞一個 user 與三筆帶明確 id 的 chat_histories。

    只有 chat_histories 是自增整數主鍵(其他三張表都是 UUID),所以只有
    這裡需要額外的資料才踩得到序號沒推進的 bug —— 沿用 _seed_source 塞
    company,不重複已經驗過的那份資料形狀。
    """
    _seed_source(url)
    engine = create_engine(url, future=True)
    with engine.begin() as conn:
        conn.execute(Base.metadata.tables["users"].insert(), {
            "id": "22222222-2222-2222-2222-222222222222",
            "company_id": "11111111-1111-1111-1111-111111111111",
            "line_user_id": "Uabc123",
            "display_name": "王小明", "picture_url": None,
            "mode": "AI", "mode_changed_at": None, "mode_expires_at": None,
            "last_message_at": None,
            "created_at": datetime(2026, 9, 21, 3, 0, 0),
            "updated_at": datetime(2026, 9, 21, 3, 0, 0),
        })
        # 明確指定 id,模擬「已經搬過去的資料本來就是自增整數」——
        # 這正是會撞到序號沒推進的那幾列。
        for i in (1, 2, 3):
            conn.execute(Base.metadata.tables["chat_histories"].insert(), {
                "id": i,
                "company_id": "11111111-1111-1111-1111-111111111111",
                "user_id": "22222222-2222-2222-2222-222222222222",
                "role": "user", "content": f"訊息 {i}",
                "line_message_id": f"msg-{i}",
                "latency_ms": None, "token_count": None,
                "created_at": datetime(2026, 9, 21, 3, 0, 0),
            })
    engine.dispose()


@POSTGRES_ONLY
def test_sequence_is_advanced_so_the_next_message_does_not_collide(tmp_path):
    """chat_histories 的主鍵是自增整數,不是 UUID(其他三張表都是,見
    app/models/chat.py)。搬遷用明確的 id 值插入,PostgreSQL 的序號不會
    自動跟著推進 —— 搬完後序號還停在很小的號碼,下一筆不指定 id 的
    INSERT 撞主鍵。對這個系統而言,「下一筆不指定 id 的 INSERT」就是
    下一則進來的 LINE 訊息(app/routers/webhook.py 建 ChatHistory 時
    從不指定 id)。

    只驗「搬過去的東西還在」抓不到這個 bug —— 資料真的都在,壞的是
    序號。要驗就要真的再寫一筆,而不指定 id,跟正式程式碼的寫法一樣。
    """
    src = f"sqlite:///{tmp_path / 'source.db'}"
    _seed_chat_history_source(src)

    target_url = os.environ["DATABASE_URL"]
    target = create_engine(target_url, future=True)
    Base.metadata.drop_all(target)     # 清掉其他測試可能留下的資料
    Base.metadata.create_all(target)   # 模擬 Alembic 已經建好表

    try:
        counts = migrate.copy_all(src, target_url)
        assert counts["chat_histories"] == 3

        chat = Base.metadata.tables["chat_histories"]
        with target.begin() as conn:
            max_id_before = conn.execute(select(func.max(chat.c.id))).scalar_one()
            assert max_id_before == 3   # 確認搬過去的資料就是造成問題的那種形狀

            result = conn.execute(chat.insert().values(
                company_id="11111111-1111-1111-1111-111111111111",
                user_id="22222222-2222-2222-2222-222222222222",
                role="user", content="下一則進來的訊息",
            ))
            new_id = result.inserted_primary_key[0]

        assert new_id > max_id_before
    finally:
        Base.metadata.drop_all(target)
        target.dispose()
