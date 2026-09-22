import httpx
import pytest

from app.agent.llm import LLMError, complete
from app.agent.prompt import build_messages


def test_messages_start_with_system_and_end_with_user():
    msgs = build_messages("你是客服", [], "有停車位嗎")
    assert msgs[0] == {"role": "system", "content": "你是客服"}
    assert msgs[-1] == {"role": "user", "content": "有停車位嗎"}


def test_history_is_preserved_in_order():
    history = [
        {"role": "user", "content": "第一句"},
        {"role": "assistant", "content": "第一答"},
    ]
    msgs = build_messages("S", history, "第二句")
    assert [m["content"] for m in msgs] == ["S", "第一句", "第一答", "第二句"]


def _http(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _ok(text="您好", tokens=42):
    return httpx.Response(200, json={
        "choices": [{"message": {"content": text}}],
        "usage": {"total_tokens": tokens},
    })


def test_complete_returns_text_and_tokens():
    r = complete([{"role": "user", "content": "嗨"}],
                 client=_http(lambda req: _ok("您好,有什麼可以幫忙的")))
    assert r.text == "您好,有什麼可以幫忙的"
    assert r.token_count == 42
    assert r.latency_ms >= 0


def test_http_200_wrapping_an_error_object_is_treated_as_failure():
    """OpenRouter 會用 HTTP 200 包著 error 物件回來(provider 暫時滿載時)。

    只看 status_code 會誤判成功,然後拿空字串當答案送給客人。
    判斷標準必須是「有沒有真的拿到 message 內容」。
    """
    def handler(request):
        return httpx.Response(200, json={"error": {"message": "Provider overloaded"}})

    with pytest.raises(LLMError):
        complete([{"role": "user", "content": "嗨"}], client=_http(handler))


def test_empty_answer_is_treated_as_failure():
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "   "}}]})

    with pytest.raises(LLMError):
        complete([{"role": "user", "content": "嗨"}], client=_http(handler))


def test_non_json_body_raises_llm_error():
    """上游掛掉時代理層常常回 HTML 錯誤頁,狀態碼卻還是 200。
    r.json() 會丟 ValueError —— 那不是 HTTPError,沒特別接就會變成
    500 往外冒,客人收到的是沉默。"""
    def handler(request):
        return httpx.Response(200, text="<html>502 Bad Gateway</html>")

    with pytest.raises(LLMError):
        complete([{"role": "user", "content": "嗨"}], client=_http(handler))


def test_timeout_raises_llm_error():
    def handler(request):
        raise httpx.ReadTimeout("too slow")

    with pytest.raises(LLMError):
        complete([{"role": "user", "content": "嗨"}], client=_http(handler))


def test_http_500_raises_llm_error():
    with pytest.raises(LLMError):
        complete([{"role": "user", "content": "嗨"}],
                 client=_http(lambda req: httpx.Response(500, text="boom")))


def test_request_disables_reasoning_and_sets_max_tokens():
    """推理模型會把內部思考當答案輸出,而 max_tokens 太小會被截斷。"""
    seen = {}

    def handler(request):
        import json
        seen.update(json.loads(request.content))
        return _ok()

    complete([{"role": "user", "content": "嗨"}], client=_http(handler))
    assert seen["max_tokens"] >= 1000
    assert seen["reasoning"]["exclude"] is True
