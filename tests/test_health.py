"""/health 的測試。

這個端點有兩個讀者:compose 的 healthcheck,以及 demo 前 curl 一下的人。
第二個才是重點 —— 只證明行程活著的話,資料庫斷了它照樣回 ok,
而那正是最需要它講實話的時候。
"""

from fastapi.testclient import TestClient

import app.main as main_module
from app.database import Base, engine
from app.main import app

client = TestClient(app)


def test_health_is_ok_when_the_database_answers():
    Base.metadata.create_all(engine)
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"


def test_health_reports_fail_when_the_database_is_down(monkeypatch):
    """資料庫斷了要說出來。回 ok 的話,compose 的 healthcheck 跟人都會
    以為沒事,而真正的症狀(客人沒收到回覆)出現在完全別的地方。"""

    def boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(main_module, "_database_ok", boom)
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["database"] == "fail"


def test_health_never_leaks_the_connection_string(monkeypatch):
    """這個端點是公開的,而連線字串裡有資料庫密碼。
    例外訊息尤其危險 —— SQLAlchemy 的連線錯誤常常把整串 URL 印出來。"""

    def boom():
        raise RuntimeError(
            "could not connect to postgresql+psycopg://helpdesk:hunter2@db:5432/helpdesk")

    monkeypatch.setattr(main_module, "_database_ok", boom)
    text = client.get("/health").text
    assert "hunter2" not in text
    assert "psycopg" not in text
