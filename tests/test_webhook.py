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
            "message": {"type": "text", "id": msg_id, "text": text,
                        "markAsReadToken": "read-" + msg_id},
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


@pytest.fixture(autouse=True)
def loading_calls(monkeypatch):
    """show_loading 一律擋掉並記錄。

    autouse 是刻意的:不擋的話,任何沒特別 patch 它的測試都會對
    api.line.me 發出真的 HTTP 請求 —— 測試套件不該碰網路,
    而且那種失敗會以「測試很慢、偶爾紅」的形式出現,很難查。
    """
    calls = []
    monkeypatch.setattr("app.routers.webhook.LineClient.show_loading",
                        lambda self, uid, seconds=20: calls.append(uid) or True)
    return calls


@pytest.fixture(autouse=True)
def read_calls(monkeypatch):
    """mark_as_read 一律擋掉並記錄 —— 理由跟 loading_calls 一樣:測試不碰網路。

    _body / _image_body 都帶 markAsReadToken,跟官方帳號開了「聊天」之後
    LINE 真正送來的事件一樣。"""
    calls = []
    monkeypatch.setattr("app.routers.webhook.LineClient.mark_as_read",
                        lambda self, token: calls.append(token) or True)
    return calls


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
                    "message": {"type": "image", "id": msg_id,
                                "markAsReadToken": "read-" + msg_id},
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


def test_loading_animation_is_shown_before_the_llm_call(sent, loading_calls):
    """LLM 要跑 3-15 秒。那幾秒的沉默會讓客人以為訊息沒送出去而重傳 ——
    重傳會燒免費額度,也會讓他收到兩則幾乎一樣的回覆。"""
    with TestClient(app) as client:
        _post(client, _body())
    assert loading_calls == ["U1"]


def test_no_loading_animation_for_non_text(sent, loading_calls):
    """非文字訊息走的是固定話術,不呼叫 LLM,是秒回 —— 這時跳出打字動畫
    反而怪,而且是白白多打一次 API。"""
    with TestClient(app) as client:
        _post(client, _image_body(msg_id="IMG3"))
    assert loading_calls == []


def test_loading_failure_does_not_stop_the_answer(monkeypatch):
    """動畫是錦上添花。它壞掉不能讓客人收不到答案。"""
    out = []
    monkeypatch.setattr("app.routers.webhook.LineClient.show_loading",
                        lambda self, uid, seconds=20: (_ for _ in ()).throw(
                            RuntimeError("loading 端點掛了")))
    monkeypatch.setattr("app.routers.webhook.LineClient.send",
                        lambda self, rt, uid, text: out.append(text) or True)
    monkeypatch.setattr("app.routers.webhook.complete",
                        lambda messages, **kw: LLMResult("您好", token_count=1,
                                                         latency_ms=1))
    with TestClient(app) as client:
        assert _post(client, _body()).status_code == 200
    assert out == ["您好"]


def test_simplified_characters_in_the_answer_are_fixed_before_sending(monkeypatch):
    """模型混進簡體字時,客人不該看到,資料庫也不該存到。

    prompt 已經規定繁體中文,但免費模型的語料大量是簡體,真機上就漂移過。
    弱模型要靠程式兜底,不能只靠 prompt。
    """
    out = []
    monkeypatch.setattr("app.routers.webhook.LineClient.send",
                        lambda self, rt, uid, text: out.append(text) or True)
    monkeypatch.setattr("app.routers.webhook.complete",
                        lambda messages, **kw: LLMResult(
                            "店門口有兩个停车位,滿了的話對面有收費停车場。",
                            token_count=1, latency_ms=1))
    with TestClient(app) as client:
        assert _post(client, _body()).status_code == 200

    assert out, "沒送出任何東西"
    assert "车" not in out[0] and "个" not in out[0]
    assert "停車位" in out[0]

    # 資料庫存的要跟客人看到的一樣
    with session_scope() as db:
        stored = [r.content for r in db.query(ChatHistory).all() if r.role.value == "assistant"]
    assert stored == out

# --- 轉真人 -------------------------------------------------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

from sqlalchemy import select  # noqa: E402

from app.agent.handoff import EXPIRED_PREFIX, GENERIC_SCRIPT  # noqa: E402
from app.agent.llm import ToolCall  # noqa: E402
from app.models import ConversationMode  # noqa: E402

SAFETY_SCRIPT = "這件事我立刻請店長與您聯繫。"
RULES = [
    {"level": "L3", "category": "safety", "trigger": "過敏、送醫",
     "action": "transfer", "script": SAFETY_SCRIPT},
    {"level": "L1", "category": "service", "trigger": "上菜太慢",
     "action": "apologize", "script": "很抱歉讓您有這樣的感受。"},
]


@pytest.fixture
def rules():
    with session_scope() as db:
        c = db.scalar(select(Company).where(Company.slug == "acme"))
        c.escalation_rules = RULES
        c.staff_notify_to = "Ustaff"


@pytest.fixture
def line_out(monkeypatch):
    """回給客人的(reply)與推給店員的(push)記在同一條時間軸上,
    才驗得了先後順序。"""
    out = []
    monkeypatch.setattr("app.routers.webhook.LineClient.send",
                        lambda self, rt, uid, text: out.append(("reply", text)) or True)
    monkeypatch.setattr("app.routers.webhook.LineClient.push",
                        lambda self, to, text: out.append(("push", to, text)) or True)
    return out


def _llm(monkeypatch, result=None):
    calls = []

    def fake(messages, **kw):
        calls.append(kw)
        return result or LLMResult("您好", token_count=1, latency_ms=1)

    monkeypatch.setattr("app.routers.webhook.complete", fake)
    return calls


def _mode():
    with session_scope() as db:
        u = db.scalar(select(User))
        return u.mode, u.mode_expires_at


def _expire():
    with session_scope() as db:
        db.scalar(select(User)).mode_expires_at = (
            datetime.now(timezone.utc) - timedelta(minutes=1))


def _replies(out):
    return [e[1] for e in out if e[0] == "reply"]


def _pushes(out):
    return [e for e in out if e[0] == "push"]


def test_a_keyword_hit_transfers_without_asking_the_model(rules, line_out, monkeypatch):
    calls = _llm(monkeypatch)
    with TestClient(app) as client:
        _post(client, _body(text="我朋友吃完過敏送醫了"))
    assert calls == []
    assert _replies(line_out) == [SAFETY_SCRIPT]
    (_, to, text), = _pushes(line_out)
    assert to == "Ustaff"
    assert "【安全】" in text and "判斷依據:關鍵字「過敏」" in text
    mode, expires = _mode()
    assert mode == ConversationMode.HUMAN and expires is not None


def test_the_customer_reply_goes_out_before_the_staff_push(rules, line_out, monkeypatch):
    """reply token 約一分鐘有效,推播沒有時限。"""
    _llm(monkeypatch)
    with TestClient(app) as client:
        _post(client, _body(text="過敏"))
    assert [e[0] for e in line_out] == ["reply", "push"]


def test_apologize_rules_do_not_transfer(rules, line_out, monkeypatch):
    calls = _llm(monkeypatch)
    with TestClient(app) as client:
        _post(client, _body(text="你們上菜太慢了"))
    assert len(calls) == 1
    assert _pushes(line_out) == []
    assert _mode()[0] == ConversationMode.AI


def test_messages_during_human_mode_get_no_reply(rules, line_out, monkeypatch):
    calls = _llm(monkeypatch)
    with TestClient(app) as client:
        _post(client, _body(text="過敏", msg_id="A"))
        _post(client, _body(text="還在嗎", msg_id="B"))
    assert calls == []
    assert _replies(line_out) == [SAFETY_SCRIPT]
    with session_scope() as db:
        # 存下來:真人在後台看得到,AI 之後接手時也有上下文
        assert "還在嗎" in [r.content for r in db.query(ChatHistory)]


def test_images_during_human_mode_get_no_reply(rules, line_out, monkeypatch):
    """「我只看得懂文字」會跟店員在後台的回覆混在一起。"""
    _llm(monkeypatch)
    with TestClient(app) as client:
        _post(client, _body(text="過敏", msg_id="A"))
        _post(client, _image_body(msg_id="IMG"))
    assert _replies(line_out) == [SAFETY_SCRIPT]


def test_an_expired_human_mode_returns_to_ai_with_a_heads_up(rules, line_out, monkeypatch):
    _llm(monkeypatch)
    with TestClient(app) as client:
        _post(client, _body(text="過敏", msg_id="A"))
        _expire()
        _post(client, _body(text="有停車位嗎", msg_id="B"))
    assert _replies(line_out)[-1] == EXPIRED_PREFIX + "您好"
    assert _mode() == (ConversationMode.AI, None)


def test_an_expired_human_mode_hit_again_has_no_heads_up(rules, line_out, monkeypatch):
    """「專員不在線上」接「我立刻請店長聯繫」自相矛盾。"""
    _llm(monkeypatch)
    with TestClient(app) as client:
        _post(client, _body(text="過敏", msg_id="A"))
        _expire()
        _post(client, _body(text="還是很不舒服要送醫", msg_id="B"))
    assert _replies(line_out) == [SAFETY_SCRIPT, SAFETY_SCRIPT]
    assert _mode()[0] == ConversationMode.HUMAN


def test_an_expired_human_mode_with_a_model_failure_gets_fallback_without_the_prefix(
        rules, line_out, monkeypatch):
    """F3g (1):模型失敗時送的是 fallback,不是模型的答案 —— 既有規則是
    「只有模型真的回了答案才加前綴」,fallback 前面接「專員目前不在線上」
    語意不通,這裡補上這個交叉情況的測試。"""
    monkeypatch.setattr("app.routers.webhook.complete",
                        lambda messages, **kw: (_ for _ in ()).throw(LLMError("boom")))
    with TestClient(app) as client:
        _post(client, _body(text="過敏", msg_id="A"))
        _expire()
        _post(client, _body(text="有停車位嗎", msg_id="B"))
    assert _replies(line_out)[-1] == "我這邊剛剛連線有點問題,可以再問一次嗎?"


def test_an_expired_human_mode_with_a_tool_call_transfers_again_without_the_prefix(
        rules, line_out, monkeypatch):
    """F3g (2):過期後沒命中關鍵字,但模型自己呼叫工具轉真人 —— 一樣不該
    加「專員不在線上」的前綴(script 已經表達會有人聯繫),而且要重新進
    HUMAN 模式。"""
    _llm(monkeypatch, LLMResult("", token_count=1, latency_ms=1, tool_call=ToolCall(
        "transfer_to_human", {"category": "safety", "reason": "還是不舒服"})))
    with TestClient(app) as client:
        _post(client, _body(text="過敏", msg_id="A"))
        _expire()
        _post(client, _body(text="我女兒吃完全身起紅疹", msg_id="B"))
    assert _replies(line_out) == [SAFETY_SCRIPT, SAFETY_SCRIPT]
    mode, expires = _mode()
    assert mode == ConversationMode.HUMAN and expires is not None


def test_the_model_calling_the_tool_transfers(rules, line_out, monkeypatch):
    """這是整個設計的核心:沒命中關鍵字,由模型自己決定要轉。"""
    _llm(monkeypatch, LLMResult("", token_count=1, latency_ms=1, tool_call=ToolCall(
        "transfer_to_human", {"category": "safety", "reason": "起紅疹疑似過敏反應"})))
    with TestClient(app) as client:
        _post(client, _body(text="我女兒吃完全身起紅疹"))
    assert _replies(line_out) == [SAFETY_SCRIPT]
    (_, _, text), = _pushes(line_out)
    assert "判斷依據:AI —— 起紅疹疑似過敏反應" in text
    assert _mode()[0] == ConversationMode.HUMAN


def test_a_tool_call_wins_over_text(rules, line_out, monkeypatch):
    _llm(monkeypatch, LLMResult("我幫您轉給專人喔", token_count=1, latency_ms=1,
                                tool_call=ToolCall("transfer_to_human", {"category": "safety"})))
    with TestClient(app) as client:
        _post(client, _body(text="我女兒吃完全身起紅疹"))
    assert _replies(line_out) == [SAFETY_SCRIPT]


def test_an_unknown_category_from_the_model_still_transfers(rules, line_out, monkeypatch):
    _llm(monkeypatch, LLMResult("", token_count=1, latency_ms=1,
                                tool_call=ToolCall("transfer_to_human", {"category": "weather"})))
    with TestClient(app) as client:
        _post(client, _body(text="我女兒吃完全身起紅疹"))
    assert _replies(line_out) == [GENERIC_SCRIPT]
    assert _mode()[0] == ConversationMode.HUMAN


def test_a_failed_push_keeps_human_mode(rules, monkeypatch):
    """客人已經被告知「會有人聯繫」,AI 這時又開始回答反而更混亂(spec 決策 6)。"""
    out = []
    monkeypatch.setattr("app.routers.webhook.LineClient.send",
                        lambda self, rt, uid, text: out.append(text) or True)
    monkeypatch.setattr("app.routers.webhook.LineClient.push",
                        lambda self, to, text: False)
    _llm(monkeypatch)
    with TestClient(app) as client:
        _post(client, _body(text="過敏"))
    assert out == [SAFETY_SCRIPT]
    assert _mode()[0] == ConversationMode.HUMAN


def test_a_push_that_raises_still_leaves_the_customer_with_exactly_one_message(
        rules, monkeypatch):
    """F3a:push 不是回 False,是直接丟例外(連線爆炸、SDK 內部錯誤都可能
    這樣)。跟回 False 的情況要有一樣的結果 —— 客人已經收到 script,
    不能因為店員通知爆炸就再送一句 fallback,那會變成兩句互相矛盾的話。"""
    out = []
    monkeypatch.setattr("app.routers.webhook.LineClient.send",
                        lambda self, rt, uid, text: out.append(text) or True)

    def boom_push(self, to, text):
        raise RuntimeError("push 端點連線爆炸")

    monkeypatch.setattr("app.routers.webhook.LineClient.push", boom_push)
    _llm(monkeypatch)
    with TestClient(app) as client:
        assert _post(client, _body(text="過敏")).status_code == 200
    assert out == [SAFETY_SCRIPT]
    assert _mode()[0] == ConversationMode.HUMAN


def test_a_successful_handoff_logs_one_info_line(rules, line_out, monkeypatch, caplog):
    """F3e:給示範現場正面的 log 證據(demo 步驟 2 要看 log 顯示轉真人發生了)。"""
    import logging
    caplog.set_level(logging.INFO, logger="app.routers.webhook")
    _llm(monkeypatch)
    with TestClient(app) as client:
        _post(client, _body(text="過敏"))
    assert any("轉真人 category=safety" in r.message and "依據=" in r.message
              for r in caplog.records)


def test_a_handoff_reply_that_fails_logs_an_error(rules, monkeypatch, caplog):
    """F3e:reply 跟 push 都失敗的話,客人在 HUMAN 模式裡卻什麼都沒收到 ——
    這比推播失敗更嚴重(推播失敗客人至少收得到 script),必須有 error log。"""
    import logging
    monkeypatch.setattr("app.routers.webhook.LineClient.send",
                        lambda self, rt, uid, text: False)
    monkeypatch.setattr("app.routers.webhook.LineClient.push",
                        lambda self, to, text: True)
    _llm(monkeypatch)
    caplog.set_level(logging.ERROR, logger="app.routers.webhook")
    with TestClient(app) as client:
        _post(client, _body(text="過敏"))
    assert any(r.levelname == "ERROR" and "沒收到任何訊息" in r.message
              for r in caplog.records)


def test_no_staff_target_means_no_push_but_still_transfers(rules, line_out, monkeypatch):
    with session_scope() as db:
        db.scalar(select(Company).where(Company.slug == "acme")).staff_notify_to = None
    _llm(monkeypatch)
    with TestClient(app) as client:
        _post(client, _body(text="過敏"))
    assert _pushes(line_out) == []
    assert _mode()[0] == ConversationMode.HUMAN


# --- F1:歷史紀錄裡的舊 script 不能教壞下一次的模型 --------------------------

def test_a_previously_recited_handoff_script_is_masked_in_the_messages_sent_to_the_model(
        rules, monkeypatch):
    """F1a:上一輪轉真人時存進 chat_histories 的 script,如果原封不動餵回
    模型當「歷史」,模型會學著下次也照樣念一次 script 而不呼叫工具 ——
    客人以為被轉了,實際上沒有。DB 裡的紀錄本身不能改(客人當時真的
    看到那句話),只在送給模型的 messages 裡替換成標記。"""
    seen = []

    def fake_complete(messages, **kw):
        seen.append(messages)
        return LLMResult("目前這邊還在協助您,請稍候。", token_count=1, latency_ms=1)

    monkeypatch.setattr("app.routers.webhook.LineClient.send",
                        lambda self, rt, uid, text: True)
    monkeypatch.setattr("app.routers.webhook.LineClient.push",
                        lambda self, to, text: True)
    monkeypatch.setattr("app.routers.webhook.complete", fake_complete)
    with TestClient(app) as client:
        _post(client, _body(text="過敏", msg_id="A"))
        _expire()
        _post(client, _body(text="還在嗎", msg_id="B"))

    contents = [m["content"] for m in seen[-1]]
    assert SAFETY_SCRIPT not in contents
    assert "(這一則已轉給真人處理)" in contents

    # DB 裡的紀錄本身要保持原樣 —— 客人當時真的看到的是 script,不是標記
    with session_scope() as db:
        stored = [r.content for r in db.query(ChatHistory).order_by(ChatHistory.id)]
    assert SAFETY_SCRIPT in stored


def test_the_model_reciting_the_script_as_plain_text_is_treated_as_a_handoff(
        rules, line_out, monkeypatch):
    """F1b 安全網:模型沒呼叫工具,卻照 prompt 把 script 整句念出來 ——
    客人聽起來像被轉了,實際上 HUMAN 沒切、店員沒收到通知。"""
    _llm(monkeypatch, LLMResult(SAFETY_SCRIPT, token_count=1, latency_ms=1))
    with TestClient(app) as client:
        _post(client, _body(text="我女兒吃完全身起紅疹"))
    assert _replies(line_out) == [SAFETY_SCRIPT]
    (_, _, text), = _pushes(line_out)
    assert "判斷依據:AI —— 念出轉接話術" in text
    assert _mode()[0] == ConversationMode.HUMAN


# --- F2:店員帳號不該被當成客人 ----------------------------------------------

def test_a_message_from_the_staff_account_gets_no_reply_and_no_handoff(
        rules, line_out, monkeypatch):
    """店員帳號加了 bot 好友、對 bot 打字(手滑或測試)不該被當客人處理 ——
    沒有 AI 回答的必要,更不能被規則層轉真人、再推播通知自己一次。"""
    calls = _llm(monkeypatch)
    with TestClient(app) as client:
        _post(client, _body(text="過敏", user="Ustaff"))
    assert calls == []
    assert line_out == []
    with session_scope() as db:
        staff_user = db.scalar(select(User).where(User.line_user_id == "Ustaff"))
        assert staff_user is not None and staff_user.mode == ConversationMode.AI


def test_the_tool_is_offered_only_when_there_are_transfer_rules(line_out, monkeypatch):
    calls = _llm(monkeypatch)
    with TestClient(app) as client:
        _post(client, _body(text="嗨", msg_id="A"))
    assert calls[0]["tools"] is None
    with session_scope() as db:
        db.scalar(select(Company).where(Company.slug == "acme")).escalation_rules = RULES
    with TestClient(app) as client:
        _post(client, _body(text="嗨", msg_id="B"))
    assert calls[1]["tools"][0]["function"]["name"] == "transfer_to_human"


# --- F3h:seed → escalation_rules → 關鍵字層,端到端接得起來 -------------------

def test_seeding_the_real_restaurant_yaml_then_a_keyword_message_transfers(
        line_out, monkeypatch):
    """單元測試各自用手寫的 RULES,證明不了「真的 yaml 經過 seed 存進資料庫、
    webhook 讀出來、關鍵字層命中」這一整條接得起來。這裡不手寫任何規則:
    走 run_seed 寫入真的 restaurant yaml,再用它的 trigger 打 webhook。
    憑證用真實形狀(32 位十六進位 secret、約 172 字元 token),否則 seed 會擋。"""
    from app.cli import INDUSTRIES, run_seed
    from app.knowledge.loader import load_industry

    secret = "0123456789abcdef0123456789abcdef"
    run_seed("restaurant", slug="acme-int", channel_secret=secret,
             channel_token="T" + "k" * 171, staff_notify_to="Ustaff")
    safety = next(r for r in load_industry(INDUSTRIES / "restaurant")["escalation"]
                  if r["category"] == "safety")
    assert "過敏" in safety["trigger"]  # 前提:yaml 真的有這個 trigger

    calls = _llm(monkeypatch)
    with TestClient(app) as client:
        assert _post(client, _body(text="我朋友吃完過敏送醫了"),
                     secret=secret, slug="acme-int").status_code == 200

    assert calls == []  # 關鍵字層命中,沒問模型
    assert _replies(line_out) == [safety["script"]]
    (_, to, text), = _pushes(line_out)
    assert to == "Ustaff" and "【安全】" in text
    assert _mode()[0] == ConversationMode.HUMAN


# --- 標示已讀 ----------------------------------------------------------------
# 官方帳號開了「聊天」之後,訊息要真人在後台點開才會已讀,AI 回了客人
# 那邊還是「未讀」。規則:AI 自己處理完、真的回了客人才標;轉真人與
# HUMAN 模式留給真人 —— 客人看到「未讀」,代表真人還沒看到,比較誠實。

def test_an_ai_answer_marks_the_message_as_read(sent, read_calls):
    with TestClient(app) as client:
        _post(client, _body(msg_id="R1"))
    assert read_calls == ["read-R1"]


def test_a_non_text_reply_marks_the_message_as_read(sent, read_calls):
    with TestClient(app) as client:
        _post(client, _image_body(msg_id="IMG-R"))
    assert read_calls == ["read-IMG-R"]


def test_a_fallback_reply_marks_the_message_as_read(monkeypatch, read_calls):
    """模型失敗時客人還是收到了一句話 —— 這則訊息是處理過的。"""
    monkeypatch.setattr("app.routers.webhook.LineClient.send",
                        lambda self, rt, uid, text: True)
    monkeypatch.setattr("app.routers.webhook.complete",
                        lambda messages, **kw: (_ for _ in ()).throw(LLMError("boom")))
    with TestClient(app) as client:
        _post(client, _body(msg_id="R2"))
    assert read_calls == ["read-R2"]


def test_a_keyword_handoff_leaves_the_message_unread(rules, line_out, monkeypatch,
                                                     read_calls):
    _llm(monkeypatch)
    with TestClient(app) as client:
        _post(client, _body(text="我朋友吃完過敏送醫了"))
    assert _replies(line_out) == [SAFETY_SCRIPT]
    assert read_calls == []


def test_a_tool_call_handoff_leaves_the_message_unread(rules, line_out, monkeypatch,
                                                       read_calls):
    _llm(monkeypatch, LLMResult("", token_count=1, latency_ms=1, tool_call=ToolCall(
        "transfer_to_human", {"category": "safety", "reason": "起紅疹"})))
    with TestClient(app) as client:
        _post(client, _body(text="我女兒吃完全身起紅疹"))
    assert _replies(line_out) == [SAFETY_SCRIPT]
    assert read_calls == []


def test_messages_during_human_mode_stay_unread(rules, line_out, monkeypatch, read_calls):
    """真人在後台點開才會已讀 —— AI 不能替還沒看到的真人說「我看到了」。"""
    _llm(monkeypatch)
    with TestClient(app) as client:
        _post(client, _body(text="過敏", msg_id="A"))
        _post(client, _body(text="還在嗎", msg_id="B"))
        _post(client, _image_body(msg_id="IMG-H"))
    assert read_calls == []


def test_a_message_from_the_staff_account_stays_unread(rules, line_out, monkeypatch,
                                                       read_calls):
    _llm(monkeypatch)
    with TestClient(app) as client:
        _post(client, _body(text="嗨", user="Ustaff"))
    assert read_calls == []


def test_the_message_is_marked_read_only_after_the_reply_goes_out(monkeypatch):
    """先標已讀再回答的話,模型最後決定轉真人時,訊息早就被標成已讀了。"""
    timeline = []
    monkeypatch.setattr("app.routers.webhook.LineClient.send",
                        lambda self, rt, uid, text: timeline.append("reply") or True)
    monkeypatch.setattr("app.routers.webhook.LineClient.mark_as_read",
                        lambda self, token: timeline.append("read") or True)
    monkeypatch.setattr("app.routers.webhook.complete",
                        lambda messages, **kw: LLMResult("您好", token_count=1,
                                                         latency_ms=1))
    with TestClient(app) as client:
        _post(client, _body())
    assert timeline == ["reply", "read"]


def test_a_mark_as_read_failure_does_not_touch_the_answer(monkeypatch):
    """已讀是錦上添花。它爆炸時不能被最外層的 except 接走 —— 那會在
    正確答案之後再送一句 fallback。"""
    out = []
    monkeypatch.setattr("app.routers.webhook.LineClient.send",
                        lambda self, rt, uid, text: out.append(text) or True)
    monkeypatch.setattr("app.routers.webhook.LineClient.mark_as_read",
                        lambda self, token: (_ for _ in ()).throw(
                            RuntimeError("markAsRead 端點掛了")))
    monkeypatch.setattr("app.routers.webhook.complete",
                        lambda messages, **kw: LLMResult("您好", token_count=1,
                                                         latency_ms=1))
    with TestClient(app) as client:
        assert _post(client, _body()).status_code == 200
    assert out == ["您好"]


# --- 口頭說要轉接、卻沒呼叫工具 ---------------------------------------------

PROMISED = ("我理解您的 daughter 正在遭受不適,請務必立即尋求專業醫療協助。"
            "由於這涉及健康問題,我將為您轉接給專業人員協助。")


def test_a_model_that_promises_a_transfer_without_the_tool_is_handed_off(
        rules, line_out, monkeypatch, read_calls):
    """2026-09-29 真機:模型說要轉接卻沒呼叫工具。客人要真的被轉,而且
    收到的是店家審過的通用句,不是那段夾著醫療建議的話。"""
    _llm(monkeypatch, LLMResult(PROMISED, token_count=1, latency_ms=1))
    with TestClient(app) as client:
        _post(client, _body(text="我女兒起疹子"))
    assert _replies(line_out) == [GENERIC_SCRIPT]
    (_, to, text), = _pushes(line_out)
    assert to == "Ustaff"
    assert "判斷依據:AI —— 口頭說要轉接但沒呼叫工具(「轉接」)" in text
    assert _mode()[0] == ConversationMode.HUMAN
    assert read_calls == []   # 轉真人一律留給真人點開
    with session_scope() as db:
        stored = [r.content for r in db.query(ChatHistory) if r.role.value == "assistant"]
    assert stored == [GENERIC_SCRIPT]   # 模型那段話不能進紀錄,客人沒看到它


def test_reciting_a_script_still_keeps_its_own_category(rules, line_out, monkeypatch):
    """照念 safety script 時,script 本身也含「請店長」—— 要走念 script 那道
    (保留 safety 類別與原句),不是被口頭承諾那道搶走變成通用句。"""
    _llm(monkeypatch, LLMResult(SAFETY_SCRIPT, token_count=1, latency_ms=1))
    with TestClient(app) as client:
        _post(client, _body(text="我女兒起疹子"))
    assert _replies(line_out) == [SAFETY_SCRIPT]
    (_, _, text), = _pushes(line_out)
    assert text.startswith("【安全】")
