"""app.cli set-webhook 的測試。

這道指令會真的去改別人 LINE 帳號的設定,所以「不該打的時候一個 API
都不能打」跟「打了要成功」一樣重要。
"""

import httpx
import pytest

from app import cli
from app.crypto import encrypt
from app.database import Base, engine, session_scope
from app.models import Company


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.create_all(engine)
    with session_scope() as db:
        db.add(Company(
            slug="bistro", name="微醺之夜", industry="restaurant",
            system_prompt="客服", vector_collection="kb_bistro",
            line_channel_secret_enc=encrypt("chan-secret"),
            line_channel_token_enc=encrypt("chan-token"),
        ))
    yield
    Base.metadata.drop_all(engine)


def test_sends_the_url_with_that_companys_token(monkeypatch):
    seen = {}

    def fake_put(url, json, headers, timeout):
        seen["url"] = url
        seen["endpoint"] = json["endpoint"]
        seen["auth"] = headers["Authorization"]
        return httpx.Response(200, request=httpx.Request("PUT", url))

    monkeypatch.setattr(httpx, "put", fake_put)
    cli.run_set_webhook("bistro", "https://helpdesk.tail9a2f.ts.net")

    assert seen["endpoint"] == "https://helpdesk.tail9a2f.ts.net/webhook/bistro"
    assert seen["auth"] == "Bearer chan-token"


def test_unknown_slug_never_touches_line(monkeypatch):
    """找不到公司就一個 API 都不能打 —— 拿不到 token 的情況下還去打,
    等於用別人的憑證或空 token 去改設定。"""
    called = []
    monkeypatch.setattr(httpx, "put", lambda *a, **kw: called.append(a))

    with pytest.raises(SystemExit) as exc:
        cli.run_set_webhook("nope", "https://x.ts.net")

    assert called == []
    assert "nope" in str(exc.value)


def test_line_rejecting_the_url_is_a_hard_failure(monkeypatch):
    """LINE 回非 200 要讓指令失敗。印一句話就當沒事的話,人會以為
    設定好了,而症狀要到 demo 現場傳訊息沒回應才出現。"""
    monkeypatch.setattr(httpx, "put", lambda url, json, headers, timeout:
                        httpx.Response(400, text="Invalid webhook endpoint URL",
                                       request=httpx.Request("PUT", url)))

    with pytest.raises(SystemExit) as exc:
        cli.run_set_webhook("bistro", "https://x.ts.net")

    assert "400" in str(exc.value)


def test_trailing_slash_does_not_produce_a_double_slash(monkeypatch):
    """貼網址時很容易多帶一個斜線。//webhook/bistro 在 LINE 那邊會
    Verify 失敗,而錯誤訊息只說網址無效。"""
    seen = {}
    monkeypatch.setattr(httpx, "put", lambda url, json, headers, timeout:
                        seen.update(endpoint=json["endpoint"]) or
                        httpx.Response(200, request=httpx.Request("PUT", url)))

    cli.run_set_webhook("bistro", "https://helpdesk.tail9a2f.ts.net/")
    assert seen["endpoint"] == "https://helpdesk.tail9a2f.ts.net/webhook/bistro"
