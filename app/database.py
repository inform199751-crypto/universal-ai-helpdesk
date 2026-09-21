"""資料庫連線。

兩種取得 session 的方式,用途不同,不可混用:
  get_db()       —— FastAPI 依賴注入。response 送出時就會被關掉。
  session_scope()—— 背景任務專用,自己開自己關。

為什麼要分兩種:BackgroundTasks 在 response 之後才執行,那時 get_db() 的
session 已經關了。拿去用會噴 DetachedInstanceError 之類完全指不到真正原因
的錯誤。這是 FastAPI 最容易中的坑之一。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Generator, Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings

_settings = get_settings()

_connect_args = {}
_kwargs = {}
if _settings.database_url.startswith("sqlite"):
    _connect_args["check_same_thread"] = False
    if _settings.database_url in ("sqlite://", "sqlite:///:memory:"):
        # 記憶體資料庫預設每條連線各自獨立,測試會看不到彼此寫的東西
        _kwargs["poolclass"] = StaticPool

engine = create_engine(
    _settings.database_url, connect_args=_connect_args, future=True, **_kwargs
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, _record):
    # SQLite 預設不檢查外鍵。不開這個,ForeignKey 宣告形同虛設 ——
    # 本機測試全過,上了 PostgreSQL 才發現一堆孤兒資料。
    if isinstance(dbapi_connection, sqlite3.Connection):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依賴注入用。不要在 BackgroundTasks 裡使用。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """背景任務用。自己開、自己 commit、自己關。"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
