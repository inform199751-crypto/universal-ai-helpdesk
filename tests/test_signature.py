import base64
import hashlib
import hmac
import json

from app.line.signature import compute_signature, verify_signature

SECRET = "testsecret"
# 固定樣本:一個真實形狀的 webhook body,逐字元固定下來。
# 不要用 json.dumps 重新產生 —— 那就失去這個測試的意義了。
RAW_BODY = (
    '{"destination":"Ufedcba9876543210","events":[{"type":"message",'
    '"message":{"type":"text","id":"468789577898262530","text":"\u4f60\u597d"},'
    '"webhookEventId":"01FZ74A0TDDPYRVKNK77XKC3ZR",'
    '"deliveryContext":{"isRedelivery":false},"timestamp":1692000000000,'
    '"source":{"type":"user","userId":"U1234567890abcdef"},'
    '"replyToken":"0f3779fba3b349968c5d07db31eabf65","mode":"active"}]}'
).encode("utf-8")
EXPECTED = base64.b64encode(
    hmac.new(SECRET.encode(), RAW_BODY, hashlib.sha256).digest()
).decode()


def test_compute_matches_known_sample():
    assert compute_signature(SECRET, RAW_BODY) == EXPECTED


def test_verify_accepts_correct_signature():
    assert verify_signature(SECRET, RAW_BODY, EXPECTED) is True


def test_verify_rejects_wrong_signature():
    assert verify_signature(SECRET, RAW_BODY, "bm90LWEtc2lnbmF0dXJl") is False


def test_verify_rejects_missing_header():
    assert verify_signature(SECRET, RAW_BODY, None) is False


def test_verify_rejects_malformed_header():
    """標頭不是合法 base64 時要回 False,不能丟例外。"""
    assert verify_signature(SECRET, RAW_BODY, "!!!not-base64!!!") is False


def test_reserialized_body_produces_different_signature():
    """這一條是整個檔案的重點。

    把 body parse 完再 dump 回去,位元組就變了,簽章一定對不上。
    router 因此絕對不能宣告 Pydantic body model —— 那等於已經 parse 過。
    """
    reserialized = json.dumps(json.loads(RAW_BODY)).encode("utf-8")
    assert reserialized != RAW_BODY
    assert compute_signature(SECRET, reserialized) != EXPECTED
