from unittest.mock import patch

import pytest
from sqlalchemy import Column, ForeignKey, Integer, String, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import Base, session_scope


class _Parent(Base):
    __tablename__ = "_t_parent"
    id = Column(Integer, primary_key=True)


class _Child(Base):
    __tablename__ = "_t_child"
    id = Column(Integer, primary_key=True)
    parent_id = Column(Integer, ForeignKey("_t_parent.id"), nullable=False)


def test_sqlite_enforces_foreign_keys(tmp_engine):
    """SQLite 預設不檢查外鍵。不開 PRAGMA 的話,這個測試會「通過插入」而不報錯。"""
    Base.metadata.create_all(tmp_engine, tables=[_Parent.__table__, _Child.__table__])
    with session_scope() as db:
        db.add(_Child(id=1, parent_id=999))  # 999 這個 parent 不存在
        with pytest.raises(IntegrityError):
            db.flush()
        # flush 失敗後 session 的交易會進入必須 rollback 的狀態(SQLAlchemy
        # 錯誤訊息本身就是這樣寫的:"first issue Session.rollback()")。
        # 不清掉的話,session_scope() 結束時的 db.commit() 會噴
        # PendingRollbackError,蓋掉這個測試真正要驗證的 IntegrityError。
        db.rollback()


def test_pragma_is_actually_on(tmp_engine):
    with tmp_engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_session_scope_commits_on_success(tmp_engine):
    """區塊正常結束要真的 commit——用另一個 session_scope() 才讀得到剛寫的資料。

    只在同一個 session 裡讀自己剛 add 的物件無法證明有沒有真的 commit
    (identity map 會騙人),所以驗證一定要換一個新的 session。
    """
    Base.metadata.create_all(tmp_engine, tables=[_Parent.__table__])
    with session_scope() as db:
        db.add(_Parent(id=101))

    with session_scope() as verify_db:
        assert verify_db.get(_Parent, 101) is not None


def test_session_scope_rolls_back_on_exception(tmp_engine):
    """區塊內丟例外時,session_scope() 必須自己呼叫 rollback()。

    這裡特地 spy 住 Session.rollback 而不是只檢查資料庫最終有沒有那筆資料:
    實測過(記憶體 SQLite + StaticPool)flush 之後就算完全不呼叫 rollback,
    只呼叫 close() 也會因為連線池「歸還時重置」而把還沒 commit 的資料一併
    丟掉,兩種情況資料庫看起來一模一樣。只看資料在不在,沒辦法證明
    session_scope() 真的呼叫了 rollback()——所以直接斷言呼叫本身,
    再搭配資料庫層的結果一起驗證。
    """

    class _Boom(Exception):
        pass

    Base.metadata.create_all(tmp_engine, tables=[_Parent.__table__])

    real_rollback = Session.rollback
    with patch.object(Session, "rollback", autospec=True, side_effect=real_rollback) as mock_rollback:
        with pytest.raises(_Boom):
            with session_scope() as db:
                db.add(_Parent(id=202))
                db.flush()  # 真的把 INSERT 送出去,而不是留在 pending 佇列裡
                raise _Boom("背景任務炸了")

    mock_rollback.assert_called_once()

    with session_scope() as verify_db:
        assert verify_db.get(_Parent, 202) is None


def test_session_scope_closes_session_on_exit():
    """背景任務用完的 session 一定要被關掉,不能留著讓下一個背景任務誤用
    到已經用過的 session(見 spec 第六節第 1 坑)。

    直接 spy Session.close 斷言「真的被呼叫過一次」,而不是檢查
    is_active/get_bind() 這類不管有沒有關都不會變的屬性。
    """
    real_close = Session.close
    with patch.object(Session, "close", autospec=True, side_effect=real_close) as mock_close:
        with session_scope():
            pass

    mock_close.assert_called_once()
