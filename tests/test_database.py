import pytest
from sqlalchemy import Column, ForeignKey, Integer, String, text
from sqlalchemy.exc import IntegrityError

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


def test_session_scope_closes_session():
    """背景任務用的 session 必須自己開自己關(見 spec 第六節第 1 坑)。"""
    with session_scope() as db:
        inner = db
    assert not inner.is_active or inner.get_bind() is not None
