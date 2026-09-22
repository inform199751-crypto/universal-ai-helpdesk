import json

import pytest
from fastapi.testclient import TestClient

from app.agent.llm import LLMError, LLMResult
from app.crypto import encrypt
from app.database import Base, engine, session_scope
from app.line.signature import compute_signature
from app.main import app
from app.models import ChatHistory, Company, User

SECRET = "chan-secret"


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.create_all(engine)
    with session_scope() as db:
        db.add(Company(
            slug="acme", name="測試公司", industry="ecommerce",
            system_prompt="你是測試公司的客服", vector_collection="kb_acme",
            fallback_message="我這邊剛剛連線有點問題,可以再問一次嗎?",
            line_channel_secret_enc=encrypt(SECRET),
            line_channel_token_enc=encrypt("chan-token"),
            line_destination="Ubot0001",
        ))
    yield
    Base.metadata.drop_all(engine)


def _body(text="你好", msg_id="M1", user="U1", destination="Ubot0001"):
    return json.dumps({
        "destination": destination,
        "events": [{
            "type": "message",
            "message": {"type": "text", "id": msg_id, "text": text},
            "webhookEventId": "E" + msg_id,
            "deliveryContext": {"isRedelivery": False},
            "timestamp": 1692000000000,
            "source": {"type": "user", "userId": user},
            "replyToken": "rt-" + msg_id,
            "mode": "active",
        }],
    }, ensure_ascii=False).encode("utf-8")


def _post(client, body, secret=SECRET, slug="acme"):
    return client.post(f"/webhook/{slug}", content=body,
                       headers={"X-Line-Signature": compute_signature(secret, body)})


@pytest.fixture
def sent(monkeypatch):
    """攔住對外的兩個呼叫,記下送出去的字。"""
    out = []
    monkeypatch.setattr("app.routers.webhook.LineClient.send",
                        lambda self, rt, uid, text: out.append(text) or True)
    monkeypatch.setattr("app.routers.webhook.complete",
                        lambda messages, **kw: LLMResult("您好,有什麼可以幫忙",
                                                         token_count=10, latency_ms=5))
    return out


def test_valid_request_replies_and_records_history(sent):
    with TestClient(app) as client:
        assert _post(client, _body()).status_code == 200
    assert sent == ["您好,有什麼可以幫忙"]
    with session_scope() as db:
        rows = db.query(ChatHistory).order_by(ChatHistory.id).all()
        assert [r.role.value for r in rows] == ["user", "assistant"]
        assert rows[1].latency_ms == 5 and rows[1].token_count == 10


def test_bad_signature_is_401_and_calls_nothing(sent):
    with TestClient(app) as client:
        r = _post(client, _body(), secret="wrong-secret")
    assert r.status_code == 401
    assert sent == []


def test_unknown_slug_is_404(sent):
    with TestClient(app) as client:
        assert _post(client, _body(), slug="nope").status_code == 404


def test_destination_mismatch_is_401(sent):
    with TestClient(app) as client:
        r = _post(client, _body(destination="Usomeoneelse"))
    assert r.status_code == 401
    assert sent == []


def test_empty_events_is_200_and_calls_nothing(sent):
    """LINE Console 按 Verify 時送的就是這個。"""
    body = json.dumps({"destination": "Ubot0001", "events": []}).encode()
    with TestClient(app) as client:
        assert _post(client, body).status_code == 200
    assert sent == []


def test_duplicate_message_id_only_answers_once(sent):
    with TestClient(app) as client:
        _post(client, _body(msg_id="SAME"))
        _post(client, _body(msg_id="SAME"))
    assert len(sent) == 1


def test_non_text_message_gets_a_polite_reply(sent):
    body = json.dumps({
        "destination": "Ubot0001",
        "events": [{"type": "message",
                    "message": {"type": "image", "id": "M9"},
                    "webhookEventId": "E9",
                    "deliveryContext": {"isRedelivery": False},
                    "timestamp": 1692000000000,
                    "source": {"type": "user", "userId": "U1"},
                    "replyToken": "rt-9", "mode": "active"}],
    }).encode()
    with TestClient(app) as client:
        assert _post(client, body).status_code == 200
    assert "文字" in sent[0]


def test_llm_failure_sends_fallback_not_silence(monkeypatch):
    out = []
    monkeypatch.setattr("app.routers.webhook.LineClient.send",
                        lambda self, rt, uid, text: out.append(text) or True)
    monkeypatch.setattr("app.routers.webhook.complete",
                        lambda messages, **kw: (_ for _ in ()).throw(LLMError("boom")))
    with TestClient(app) as client:
        assert _post(client, _body()).status_code == 200
    assert out == ["我這邊剛剛連線有點問題,可以再問一次嗎?"]


def test_unexpected_crash_still_sends_fallback(monkeypatch):
    """計畫外的例外(不是 LLMError)也不能讓客人收到沉默。

    最外層那個 except Exception 原本只寫 log,訊息卻寫著「嘗試送出 fallback」
    —— 註解承諾了程式沒有做的事。Global Constraint 8 說沉默是唯一不被接受的
    失敗模式,所以這條路徑也要真的送出一句話。
    """
    out = []
    monkeypatch.setattr("app.routers.webhook.LineClient.send",
                        lambda self, rt, uid, text: out.append(text) or True)

    def boom(*args, **kwargs):
        raise TypeError("計畫外的爆炸,不是 LLMError")

    monkeypatch.setattr("app.routers.webhook.build_messages", boom)
    with TestClient(app) as client:
        assert _post(client, _body()).status_code == 200
    assert out == ["我這邊剛剛連線有點問題,可以再問一次嗎?"]


def test_second_message_includes_first_in_history(monkeypatch):
    seen = []

    def fake_complete(messages, **kw):
        seen.append(messages)
        return LLMResult("好的", token_count=1, latency_ms=1)

    monkeypatch.setattr("app.routers.webhook.LineClient.send",
                        lambda self, rt, uid, text: True)
    monkeypatch.setattr("app.routers.webhook.complete", fake_complete)
    with TestClient(app) as client:
        _post(client, _body(text="第一句", msg_id="A"))
        _post(client, _body(text="第二句", msg_id="B"))
    contents = [m["content"] for m in seen[1]]
    assert "第一句" in contents and "好的" in contents


def test_user_row_is_created_once_per_company(sent):
    with TestClient(app) as client:
        _post(client, _body(msg_id="A"))
        _post(client, _body(msg_id="B"))
    with session_scope() as db:
        assert db.query(User).count() == 1


def test_health_endpoint():
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"


def _image_body(msg_id="M9", user="U1"):
    return json.dumps({
        "destination": "Ubot0001",
        "events": [{"type": "message",
                    "message": {"type": "image", "id": msg_id},
                    "webhookEventId": "E" + msg_id,
                    "deliveryContext": {"isRedelivery": False},
                    "timestamp": 1692000000000,
                    "source": {"type": "user", "userId": user},
                    "replyToken": "rt-" + msg_id, "mode": "active"}],
    }).encode()


def test_duplicate_image_only_replies_once(sent):
    """去重是靠 line_message_id 的 unique 索引擋的(決策 6),但非文字訊息
    原本在那一步之前就 return,整條去重被繞過。

    Console 的 Webhook redelivery 是開著的 —— LINE 沒收到 2xx 會重送,
    所以客人會連收兩次「我只看得懂文字訊息」。文字訊息不會有這個問題,
    只有圖片、貼圖、語音、位置這些會,所以測試套件一直沒抓到。
    """
    with TestClient(app) as client:
        _post(client, _image_body(msg_id="SAMEIMG"))
        _post(client, _image_body(msg_id="SAMEIMG"))
    assert len(sent) == 1


def test_non_text_message_is_recorded_in_history(sent):
    """非文字訊息原本完全不留紀錄。對一個要拿來談營運數據的系統來說,
    「客人實際上都傳了什麼」不該是看不到的。"""
    with TestClient(app) as client:
        _post(client, _image_body(msg_id="IMG1"))
    with session_scope() as db:
        rows = db.query(ChatHistory).order_by(ChatHistory.id).all()
        assert [r.role.value for r in rows] == ["user", "assistant"]
        assert rows[0].content == "[圖片]"
        assert rows[0].line_message_id == "IMG1"
        assert "文字" in rows[1].content


def test_a_text_message_after_an_image_still_sees_it_in_history(sent):
    """紀錄下來的非文字訊息要進得了短期記憶 —— 客人傳了一張圖、我們說看不懂,
    下一句接著問「那這個呢」時,模型要知道剛才發生過什麼。"""
    with TestClient(app) as client:
        _post(client, _image_body(msg_id="IMG2"))
        _post(client, _body(text="那這個多少錢", msg_id="T2"))
    with session_scope() as db:
        contents = [r.content for r in
                    db.query(ChatHistory).order_by(ChatHistory.id).all()]
    assert "[圖片]" in contents
