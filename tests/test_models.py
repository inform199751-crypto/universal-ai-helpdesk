import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.crypto import encrypt
from app.database import Base, engine, session_scope
from app.models import (
    ChatHistory,
    ChatRole,
    Company,
    ConversationMode,
    DocumentStatus,
    KnowledgeDocument,
    User,
)


@pytest.fixture(autouse=True)
def _tables():
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


def _company(db, slug="acme"):
    c = Company(
        slug=slug,
        name="測試公司",
        industry="ecommerce",
        line_channel_secret_enc=encrypt("secret"),
        line_channel_token_enc=encrypt("token"),
        vector_collection=f"kb_{slug}",
    )
    db.add(c)
    db.flush()
    return c


def test_same_line_user_id_allowed_across_companies():
    """LINE userId 只在同一個 channel 內唯一。

    設成單欄 unique 的話,第二家公司的客人會在插入時撞鍵,
    而錯誤訊息看起來像「這個人已經存在」—— 查很久都查不到真正原因。
    """
    with session_scope() as db:
        a = _company(db, "aaa")
        b = _company(db, "bbb")
        db.add(User(company_id=a.id, line_user_id="U123"))
        db.add(User(company_id=b.id, line_user_id="U123"))
        db.flush()  # 不可以報錯


def test_same_line_user_id_rejected_within_one_company():
    with session_scope() as db:
        c = _company(db)
        db.add(User(company_id=c.id, line_user_id="U123"))
        db.flush()
        db.add(User(company_id=c.id, line_user_id="U123"))
        with pytest.raises(IntegrityError):
            db.flush()
        # flush 失敗後 session 的交易會進入必須 rollback 的狀態(SQLAlchemy
        # 錯誤訊息本身就是這樣寫的:"first issue Session.rollback()")。
        # 不清掉的話,session_scope() 結束時的 db.commit() 會噴
        # PendingRollbackError,蓋掉這個測試真正要驗證的 IntegrityError。
        db.rollback()


def test_user_defaults_to_ai_mode():
    with session_scope() as db:
        c = _company(db)
        u = User(company_id=c.id, line_user_id="U1")
        db.add(u)
        db.flush()
        assert u.mode is ConversationMode.AI


def test_enum_columns_store_value_not_name():
    """role/status 存的是 .value(小寫字串),不是 .name。

    刻意不透過 ORM 讀回:不管資料庫裡存的是 "human_agent" 還是 "HUMAN_AGENT",
    SQLAlchemy 讀取時都會用同一套對照表還原成同一個 Python enum member,兩種
    情況測起來一模一樣——像上面 test_user_defaults_to_ai_mode 那樣斷言
    `u.mode is ConversationMode.AI`,對「有沒有存小寫」這件事沒有防護力。
    這裡直接用 text() 查資料庫裡的 raw 字串,才抓得到「有人把 Enum(...) 的
    values_callable 拿掉」這種回歸——一旦真的資料庫裡已經有資料,那時候要
    改就是資料遷移,不是一行程式碼的事了。

    選 HUMAN_AGENT:這是 .name 跟 .value 差最多的 member,拿掉
    values_callable 會直接變成大寫又帶底線的 "HUMAN_AGENT",跟預期的
    "human_agent" 一眼就能看出不一樣,不會跟其他情況搞混。
    """
    with session_scope() as db:
        c = _company(db)
        u = User(company_id=c.id, line_user_id="U1")
        db.add(u)
        db.flush()

        chat = ChatHistory(company_id=c.id, user_id=u.id,
                           role=ChatRole.HUMAN_AGENT, content="轉真人為您服務")
        doc = KnowledgeDocument(
            company_id=c.id, filename="f.pdf", source_type="file",
            checksum="deadbeef", vector_collection=c.vector_collection,
            status=DocumentStatus.READY,
        )
        db.add_all([chat, doc])
        db.flush()

        role_raw = db.execute(
            text("select role from chat_histories where id = :id"), {"id": chat.id}
        ).scalar_one()
        status_raw = db.execute(
            text("select status from knowledge_documents where id = :id"), {"id": doc.id}
        ).scalar_one()

        assert role_raw == "human_agent"
        assert status_raw == "ready"


def test_duplicate_line_message_id_rejected():
    """去重靠資料庫的 unique 索引,不要自己寫「先查再寫」(有 race condition)。"""
    with session_scope() as db:
        c = _company(db)
        u = User(company_id=c.id, line_user_id="U1")
        db.add(u)
        db.flush()
        db.add(ChatHistory(company_id=c.id, user_id=u.id, role=ChatRole.USER,
                           content="嗨", line_message_id="M1"))
        db.flush()
        db.add(ChatHistory(company_id=c.id, user_id=u.id, role=ChatRole.USER,
                           content="嗨", line_message_id="M1"))
        with pytest.raises(IntegrityError):
            db.flush()
        # 同上:flush 失敗後必須先 rollback,否則 session_scope() 的
        # db.commit() 會噴 PendingRollbackError,蓋掉這裡要驗證的 IntegrityError。
        db.rollback()


def test_multiple_null_line_message_ids_allowed():
    """assistant 的訊息沒有 LINE message id。

    PostgreSQL 與 SQLite 的 unique 約束都允許多個 NULL —— 這是刻意的,
    不要為了「乾淨」把這欄改成 NOT NULL,那會讓第二則回覆就撞鍵。
    """
    with session_scope() as db:
        c = _company(db)
        u = User(company_id=c.id, line_user_id="U1")
        db.add(u)
        db.flush()
        db.add(ChatHistory(company_id=c.id, user_id=u.id,
                           role=ChatRole.ASSISTANT, content="您好"))
        db.add(ChatHistory(company_id=c.id, user_id=u.id,
                           role=ChatRole.ASSISTANT, content="請問還有問題嗎"))
        db.flush()  # 不可以報錯


def test_credentials_are_not_stored_in_plaintext():
    with session_scope() as db:
        c = _company(db)
        assert b"secret" not in c.line_channel_secret_enc


def test_chat_history_is_isolated_between_companies():
    """租戶隔離。v1 只有一家公司,但這條要先立起來 ——
    等到真的開第二家才發現查詢漏了 company_id,那時外洩的是客人的對話。"""
    with session_scope() as db:
        a, b = _company(db, "aaa"), _company(db, "bbb")
        ua = User(company_id=a.id, line_user_id="U123")
        ub = User(company_id=b.id, line_user_id="U123")
        db.add_all([ua, ub])
        db.flush()
        db.add(ChatHistory(company_id=a.id, user_id=ua.id,
                           role=ChatRole.USER, content="A 公司的秘密"))
        db.flush()
        leaked = db.query(ChatHistory).filter(ChatHistory.company_id == b.id).all()
        assert leaked == []
