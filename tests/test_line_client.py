import httpx
import pytest

from app.line.client import API_BASE, LineClient, truncate_for_line


def test_truncate_leaves_short_text_alone():
    assert truncate_for_line("短訊息", limit=100) == "短訊息"


def test_truncate_marks_the_cut():
    out = truncate_for_line("字" * 200, limit=50)
    assert len(out) <= 50
    assert "已截斷" in out


def _client(handler):
    transport = httpx.MockTransport(handler)
    c = LineClient("token")
    # 一定要跟正式的 client 一樣帶 base_url。少了它,相對路徑沒有 scheme/host,
    # httpx 會丟 ValueError —— 那不是 HTTPError,_post 的 except 攔不到,
    # 測試會以「例外」而不是「回 False」的形式壞掉,看起來像正式碼有問題。
    c._http = httpx.Client(transport=transport, base_url=API_BASE)
    return c


def test_reply_posts_to_reply_endpoint():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={})

    assert _client(handler).reply("rt", "您好") is True
    assert seen["url"].endswith("/v2/bot/message/reply")
    assert seen["auth"] == "Bearer token"


def test_reply_returns_false_on_error():
    def handler(request):
        return httpx.Response(400, json={"message": "Invalid reply token"})

    assert _client(handler).reply("expired", "您好") is False


def test_send_falls_back_to_push_when_reply_fails():
    """reply token 只有一分鐘且只能用一次。逾時就必須改用 push,
    否則那一次就是徹底沉默。"""
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.path.endswith("/reply"):
            return httpx.Response(400, json={"message": "Invalid reply token"})
        return httpx.Response(200, json={})

    assert _client(handler).send("rt", "U1", "您好") is True
    assert len(calls) == 2 and calls[1].endswith("/push")


def test_send_returns_false_when_both_fail():
    def handler(request):
        return httpx.Response(500, json={})

    assert _client(handler).send("rt", "U1", "您好") is False


def test_network_error_returns_false_not_raise():
    def handler(request):
        raise httpx.ConnectError("boom")

    assert _client(handler).reply("rt", "您好") is False


def test_send_skips_reply_entirely_when_no_reply_token():
    """有些事件(例如 postback 重送、或我們自己補送)手上根本沒有 reply token。
    這時要直接 push,不能拿空字串去打 reply 白白浪費一次往返。"""
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    assert _client(handler).send("", "U1", "您好") is True
    assert calls == ["/v2/bot/message/push"]


def test_show_loading_posts_to_the_loading_endpoint():
    seen = {}

    def handler(request):
        import json
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={})

    assert _client(handler).show_loading("U1") is True
    assert seen["path"] == "/v2/bot/chat/loading/start"
    assert seen["body"]["chatId"] == "U1"
    # 秒數必須是 5 的倍數、5~60,否則 LINE 直接回 400
    assert seen["body"]["loadingSeconds"] % 5 == 0
    assert 5 <= seen["body"]["loadingSeconds"] <= 60


def test_show_loading_treats_202_as_success():
    """這支 API 回的是 202 Accepted,不是 200。只認 200 的話每次都會被
    當成失敗寫進 log —— 動畫其實有出來,log 卻天天在喊錯。"""
    assert _client(lambda req: httpx.Response(202, json={})).show_loading("U1") is True


def test_show_loading_failure_is_not_fatal():
    """動畫是錦上添花。它失敗不能影響客人拿到答案,所以只回 False。"""
    assert _client(lambda req: httpx.Response(400, json={})).show_loading("U1") is False


def test_reply_still_requires_200():
    """202 只對 loading 那支端點成立。reply 回 202 不代表送出去了,
    不可以因為放寬了一支就連帶放寬全部。"""
    assert _client(lambda req: httpx.Response(202, json={})).reply("rt", "hi") is False
