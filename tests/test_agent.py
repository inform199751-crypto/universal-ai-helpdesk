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


# --- 供應商滿載時換一個模型 ------------------------------------------------

from app.config import get_settings  # noqa: E402

_S = get_settings()
PREFERRED = _S.openrouter_model
FALLBACK = _S.openrouter_fallback_model


def _overloaded():
    """OpenRouter 轉述上游滿載的真實形狀:HTTP 200,body 裡包著 error。"""
    return httpx.Response(200, json={"error": {
        "message": "Upstream error from Nvidia: Service temporarily overloaded",
        "code": 503, "metadata": {"error_type": "provider_overloaded"}}})


def _by_model(table):
    """依請求裡的 model 欄位分派回應。"""
    def handler(request):
        import json
        model = json.loads(request.content)["model"]
        return table[model]()
    return handler


def test_falls_back_to_the_auto_router_when_the_preferred_model_is_overloaded():
    """真實踩到的:免費的 Nvidia 模型單獨打一次就滿載。沒有這層退回,
    客人收到的就是 fallback 訊息 —— 技術上正確,現場觀感很差。"""
    r = complete([{"role": "user", "content": "嗨"}],
                 client=_http(_by_model({PREFERRED: _overloaded,
                                         FALLBACK: lambda: _ok("退回之後答出來了")})))
    assert r.text == "退回之後答出來了"
    assert r.model == FALLBACK


def test_does_not_retry_when_the_preferred_model_works():
    """偏好模型會通時不可以多打一次 —— 免費額度是按請求數算的。"""
    calls = []

    def handler(request):
        import json
        calls.append(json.loads(request.content)["model"])
        return _ok()

    complete([{"role": "user", "content": "嗨"}], client=_http(handler))
    assert calls == [PREFERRED]


def test_latency_includes_the_time_spent_on_the_failed_model():
    """latency_ms 要記客人實際等了多久,不是最後那個模型花了多久。
    只記最後一個的話,退回越常發生,紀錄就越偏低 —— 正好在最需要
    看清楚的時候失真。"""
    def slow_overloaded():
        _time.sleep(0.3)
        return _overloaded()

    r = complete([{"role": "user", "content": "嗨"}],
                 client=_http(_by_model({PREFERRED: slow_overloaded,
                                         FALLBACK: lambda: _ok("退回之後答出來了")})))
    assert r.latency_ms >= 300


def test_raises_only_after_every_model_has_failed():
    with pytest.raises(LLMError) as exc:
        complete([{"role": "user", "content": "嗨"}],
                 client=_http(_by_model({PREFERRED: _overloaded,
                                         FALLBACK: _overloaded})))
    # 錯誤訊息要講出兩個模型都試過了,否則查的人會以為只打了一次
    assert PREFERRED in str(exc.value) and FALLBACK in str(exc.value)


def test_does_not_try_the_same_model_twice(monkeypatch):
    """退回模型設成跟偏好模型一樣時,不該白白多打一次同一個上游。"""
    class FakeSettings:
        openrouter_model = "same/model"
        openrouter_fallback_model = "same/model"
        openrouter_base_url = _S.openrouter_base_url
        openrouter_api_key = "k"
        llm_max_tokens = _S.llm_max_tokens
        llm_timeout_seconds = _S.llm_timeout_seconds
        llm_total_budget_seconds = _S.llm_total_budget_seconds

    monkeypatch.setattr("app.agent.llm.get_settings", lambda: FakeSettings())
    calls = []

    def handler(request):
        calls.append(1)
        return _overloaded()

    with pytest.raises(LLMError):
        complete([{"role": "user", "content": "嗨"}], client=_http(handler))
    assert len(calls) == 1


# --- 整體時間上限 ------------------------------------------------------------

import json as _json  # noqa: E402
import time as _time  # noqa: E402

TRICKLE_SECONDS = 3.0


def _trickling(text="很慢的答案"):
    """真機上踩到的形狀:OpenRouter 一秒內就回 200 標頭,然後在上游還在
    生成時持續送空白維持連線,內容 56 秒後才到。

    httpx 的 timeout 是「兩次讀到資料之間」的上限,每個空白都會把它
    歸零 —— 所以 45 秒的 timeout 從頭到尾沒有觸發過。

    TRICKLE_SECONDS 後會送出正常的 JSON,不會永遠卡著:沒有上限的
    實作會在那時「成功」而讓斷言失敗,而不是讓整個測試卡死。
    """
    def body():
        end = _time.monotonic() + TRICKLE_SECONDS
        while _time.monotonic() < end:
            yield b" "
            _time.sleep(0.02)
        yield _json.dumps({"choices": [{"message": {"content": text}}]}).encode()
    return lambda: httpx.Response(200, content=body())


@pytest.fixture
def small_budget(monkeypatch):
    class FakeSettings:
        openrouter_model = PREFERRED
        openrouter_fallback_model = FALLBACK
        openrouter_base_url = _S.openrouter_base_url
        openrouter_api_key = "k"
        llm_max_tokens = _S.llm_max_tokens
        llm_timeout_seconds = _S.llm_timeout_seconds
        llm_total_budget_seconds = 0.5

    monkeypatch.setattr("app.agent.llm.get_settings", lambda: FakeSettings())


def test_a_trickling_upstream_is_cut_off_at_the_total_budget(small_budget):
    """上游一直送空白時要在時間上限內放棄。放著等的話,客人停在「已讀」
    直到 LINE reply token 過期 —— 連 fallback 訊息都收不到。"""
    started = _time.monotonic()
    with pytest.raises(LLMError):
        complete([{"role": "user", "content": "嗨"}],
                 client=_http(_by_model({PREFERRED: _trickling(),
                                         FALLBACK: _trickling()})))
    assert _time.monotonic() - started < TRICKLE_SECONDS


def test_the_fallback_model_does_not_get_a_fresh_budget(small_budget):
    """上限是兩個模型合計的。各自一份的話,最壞情況是兩倍時間,
    正好越過 reply token 的一分鐘效期。"""
    calls = []

    def handler(request):
        calls.append(_json.loads(request.content)["model"])
        return _trickling()()

    with pytest.raises(LLMError):
        complete([{"role": "user", "content": "嗨"}], client=_http(handler))
    assert calls == [PREFERRED]


# --- 工具呼叫 ----------------------------------------------------------------

from app.agent.llm import ToolCall  # noqa: E402

TOOL = {"type": "function", "function": {
    "name": "transfer_to_human", "description": "轉給真人",
    "parameters": {"type": "object",
                   "properties": {"category": {"type": "string", "enum": ["safety"]}},
                   "required": ["category"]}}}


def _tool_response(arguments='{"category": "safety", "reason": "起紅疹"}', content=None):
    """OpenAI 相容格式的工具呼叫。arguments 是「JSON 字串」不是物件 ——
    這是規格,也是模型最常寫壞的地方。"""
    return httpx.Response(200, json={
        "choices": [{"message": {
            "content": content,
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "transfer_to_human",
                                         "arguments": arguments}}]}}],
        "usage": {"total_tokens": 30}})


def test_a_tool_call_with_empty_content_is_a_success():
    """呼叫工具時 content 本來就常常是 null。沿用「沒有文字就是失敗」的判斷,
    每一次轉真人都會被當成模型壞掉、改送 fallback。"""
    r = complete([{"role": "user", "content": "我女兒吃完全身起紅疹"}],
                 tools=[TOOL], client=_http(lambda req: _tool_response()))
    assert r.tool_call == ToolCall("transfer_to_human",
                                   {"category": "safety", "reason": "起紅疹"})
    assert r.text == ""


def test_tools_are_sent_upstream_only_when_given():
    seen = []

    def handler(request):
        seen.append(_json.loads(request.content))
        return _ok()

    complete([{"role": "user", "content": "嗨"}], client=_http(handler))
    complete([{"role": "user", "content": "嗨"}], tools=[TOOL], client=_http(handler))
    assert "tools" not in seen[0]
    assert seen[1]["tools"] == [TOOL]


def test_text_and_a_tool_call_are_both_returned():
    """同時回了文字與工具呼叫時兩個都要交出去,由呼叫端決定 ——
    在這裡丟掉任何一個,webhook 就沒辦法實作「以工具為準」。"""
    r = complete([{"role": "user", "content": "嗨"}], tools=[TOOL],
                 client=_http(lambda req: _tool_response(content="我幫您轉給專人")))
    assert r.text == "我幫您轉給專人"
    assert r.tool_call is not None


def test_broken_tool_arguments_still_produce_a_tool_call():
    """模型已經表達「要轉」了。參數 JSON 寫壞不該讓整次呼叫失敗 ——
    那會讓客人的緊急狀況掉進 fallback。"""
    r = complete([{"role": "user", "content": "嗨"}], tools=[TOOL],
                 client=_http(lambda req: _tool_response(arguments="{category: safety")))
    assert r.tool_call == ToolCall("transfer_to_human", {})


def test_no_text_and_no_tool_call_is_still_a_failure():
    empty = lambda req: httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})
    with pytest.raises(LLMError):
        complete([{"role": "user", "content": "嗨"}], tools=[TOOL], client=_http(empty))


def test_tool_arguments_already_an_object_are_used_as_is():
    """arguments 已經是字典時,就直接用——模型的意圖很清楚,不該丟掉。"""
    r = complete([{"role": "user", "content": "嗨"}], tools=[TOOL],
                 client=_http(lambda req: _tool_response(arguments={"category": "safety"})))
    assert r.tool_call == ToolCall("transfer_to_human", {"category": "safety"})


def test_a_successful_answer_logs_which_model_actually_answered(caplog):
    """F3d:退回機制生效時,回應裡的 model 欄位(上游實際回答的模型)
    可能跟我們請求的 slug 不一樣(例如 openrouter/free 自動路由分派到
    的實際供應商)。品質忽好忽壞時,這是第一個要查的線索。"""
    import logging
    caplog.set_level(logging.INFO, logger="app.agent.llm")

    def handler(request):
        return httpx.Response(200, json={
            "model": "some-actual-upstream/model-x",
            "choices": [{"message": {"content": "您好"}}],
            "usage": {"total_tokens": 5}})

    r = complete([{"role": "user", "content": "嗨"}], client=_http(handler))
    assert r.model == PREFERRED  # 既有語意不變:model 欄位仍是請求的 slug
    assert any("some-actual-upstream/model-x" in rec.message and "工具呼叫:無" in rec.message
               for rec in caplog.records)


def test_a_tool_call_answer_logs_the_tool_name(caplog):
    import logging
    caplog.set_level(logging.INFO, logger="app.agent.llm")
    complete([{"role": "user", "content": "嗨"}], tools=[TOOL],
            client=_http(lambda req: _tool_response()))
    assert any("工具呼叫:transfer_to_human" in rec.message for rec in caplog.records)


def test_tool_arguments_of_the_wrong_type_become_empty():
    """arguments 是其他型別(陣列、數字等)時就當成失敗解析,回 {}。"""
    r = complete([{"role": "user", "content": "嗨"}], tools=[TOOL],
                 client=_http(lambda req: _tool_response(arguments=[1, 2])))
    assert r.tool_call == ToolCall("transfer_to_human", {})


# --- 多供應商:NVIDIA 主、OpenRouter 備援 -------------------------------------

NV_URL = "https://nvidia.example/v1"
OR_URL = "https://openrouter.example/v1"


@pytest.fixture
def two_providers(monkeypatch):
    """設了 NVIDIA key 的設定。網址、key、模型名兩邊各自一份 ——
    這樣才看得出每個請求是照哪一份送的。"""
    class FakeSettings:
        nvidia_api_key = "nvapi-test"
        nvidia_base_url = NV_URL
        nvidia_model = "nv/super"
        openrouter_api_key = "sk-or-test"
        openrouter_base_url = OR_URL
        openrouter_model = "or/free"
        openrouter_fallback_model = "or/super-free"
        llm_max_tokens = _S.llm_max_tokens
        llm_timeout_seconds = _S.llm_timeout_seconds
        llm_total_budget_seconds = _S.llm_total_budget_seconds

    monkeypatch.setattr("app.agent.llm.get_settings", lambda: FakeSettings())
    return FakeSettings


def _recording(calls, responses):
    """記下每個請求的 (網址, Authorization, model),回應依 model 分派。"""
    def handler(request):
        model = _json.loads(request.content)["model"]
        calls.append((str(request.url), request.headers.get("Authorization"), model))
        return responses.get(model, lambda: httpx.Response(404, text="unknown model"))()
    return handler


def test_with_an_nvidia_key_the_first_request_goes_to_nvidia(two_providers):
    calls = []
    complete([{"role": "user", "content": "嗨"}],
             client=_http(_recording(calls, {"nv/super": _ok, "or/free": _ok})))
    assert calls == [(f"{NV_URL}/chat/completions", "Bearer nvapi-test", "nv/super")]


def test_when_nvidia_fails_openrouter_answers_with_its_own_key(two_providers):
    """key 送錯供應商等於把 key 交給第三方 —— 每個請求的網址與 key
    必須是同一家的。"""
    calls = []
    r = complete([{"role": "user", "content": "嗨"}],
                 client=_http(_recording(calls, {
                     "nv/super": lambda: httpx.Response(503, text="overloaded"),
                     "or/free": lambda: _ok("備援答的")})))
    assert r.text == "備援答的"
    assert r.model == "or/free"
    assert calls == [
        (f"{NV_URL}/chat/completions", "Bearer nvapi-test", "nv/super"),
        (f"{OR_URL}/chat/completions", "Bearer sk-or-test", "or/free"),
    ]


def test_with_an_nvidia_key_the_openrouter_copy_of_the_model_is_not_tried(two_providers):
    """OpenRouter 上的 :free 版跟 NVIDIA 主要模型是同一顆 —— NVIDIA 塞車時
    它多半也塞車(9/24 四次全限流),而 OpenRouter 失敗的請求也扣每日額度。"""
    calls = []
    with pytest.raises(LLMError):
        complete([{"role": "user", "content": "嗨"}],
                 client=_http(_recording(calls, {
                     "nv/super": _overloaded, "or/free": _overloaded,
                     "or/super-free": _ok})))
    assert [model for _, _, model in calls] == ["nv/super", "or/free"]


def test_an_empty_nvidia_key_means_openrouter_only(two_providers):
    """compose 沒給值時傳進來的是空字串,不是沒有這個變數。把空字串當成
    「有設定」的話,每則訊息都先拿空的 key 去打 NVIDIA、必定 401 再退回。"""
    two_providers.nvidia_api_key = ""
    calls = []
    complete([{"role": "user", "content": "嗨"}],
             client=_http(_recording(calls, {"or/free": _ok})))
    assert calls == [(f"{OR_URL}/chat/completions", "Bearer sk-or-test", "or/free")]


def test_the_default_nvidia_endpoint_and_model_are_the_ones_nvidia_serves(monkeypatch):
    """模型名不能帶 OpenRouter 的 :free 後綴 —— NVIDIA 不認得那個名字,
    每則訊息都會失敗再退回,看起來「有在動」其實主要模型從沒答過。"""
    from app.config import Settings
    for var in ("NVIDIA_BASE_URL", "NVIDIA_MODEL"):
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None, fernet_key="k", openrouter_api_key="k",
                 nvidia_api_key="nvapi-x")
    monkeypatch.setattr("app.agent.llm.get_settings", lambda: s)
    seen = []

    def handler(request):
        seen.append((str(request.url), _json.loads(request.content)["model"]))
        return _ok()

    complete([{"role": "user", "content": "嗨"}], client=_http(handler))
    assert seen == [("https://integrate.api.nvidia.com/v1/chat/completions",
                     "nvidia/nemotron-3-super-120b-a12b")]


def test_the_log_says_which_provider_answered(two_providers, caplog):
    """demo 現場看 log 就要知道是 NVIDIA 還是備援在答,不必去查設定。"""
    import logging
    caplog.set_level(logging.INFO, logger="app.agent.llm")
    complete([{"role": "user", "content": "嗨"}],
             client=_http(_recording([], {"nv/super": _ok})))
    complete([{"role": "user", "content": "嗨"}],
             client=_http(_recording([], {"nv/super": _overloaded, "or/free": _ok})))
    done = [rec.message for rec in caplog.records if "回答完成" in rec.message]
    assert len(done) == 2
    assert "NVIDIA" in done[0] and "OpenRouter" in done[1]
