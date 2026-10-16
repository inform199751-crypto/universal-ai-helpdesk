"""agent loop。用假的 complete 回應序列 —— 測的是迴圈怎麼走,不是模型。"""

import json
import time

from app.agent.llm import LLMError, LLMResult, ToolCall
from app.agent.loop import run_agent

TOOLS = [{"type": "function", "function": {"name": "check_availability"}},
         {"type": "function", "function": {"name": "create_booking"}}]
BOOKED = {"date": "2026-10-09", "weekday": "五", "time": "20:00", "party_size": 4,
          "name": "王小明", "phone": "0912345678", "extra": {}}


def _text(t, tokens=10):
    return LLMResult(t, token_count=tokens, latency_ms=1)


def _tools(*calls, text=""):
    return LLMResult(text, token_count=10, latency_ms=1,
                     tool_call=calls[0], tool_calls=tuple(calls))


def _call(name, args=None, call_id="c1"):
    return ToolCall(name, args or {}, id=call_id)


class Script:
    """依序回傳預先寫好的回應,並記下每次被呼叫時拿到的東西。"""

    def __init__(self, *results, delay=0.0):
        self.results = list(results)
        self.seen = []
        self.delay = delay

    def __call__(self, messages, *, tools=None, deadline=None, **kw):
        self.seen.append({"messages": [dict(m) for m in messages],
                          "tools": tools, "deadline": deadline, "kw": dict(kw)})
        if self.delay:
            time.sleep(self.delay)
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _run(script, execute=None, *, max_steps=4, budget=45.0, tools=TOOLS):
    return run_agent([{"role": "system", "content": "S"},
                      {"role": "user", "content": "我要訂位"}],
                     tools=tools, execute=execute or (lambda n, a: {"ok": True}),
                     max_steps=max_steps, budget_seconds=budget, complete_fn=script)


def test_a_plain_answer_ends_the_loop_in_one_step():
    s = Script(_text("您好"))
    r = _run(s)
    assert (r.text, r.steps, r.handoff, r.booking, r.tools_used) == ("您好", 1, None, None, ())
    assert s.seen[0]["tools"] == TOOLS


def test_tool_results_go_back_to_the_model_until_it_answers():
    executed = []

    def execute(name, args):
        executed.append((name, args))
        return {"slots": []} if name == "check_availability" else {"ok": True, "booking": BOOKED}

    s = Script(_tools(_call("check_availability", {"date": "2026-10-09"}, "c1")),
               _tools(_call("create_booking", {"time": "20:00"}, "c2")),
               _text("已為您訂好"))
    r = _run(s, execute)
    assert (r.text, r.steps, r.booking) == ("已為您訂好", 3, BOOKED)
    assert r.tools_used == ("check_availability", "create_booking")
    assert executed == [("check_availability", {"date": "2026-10-09"}),
                        ("create_booking", {"time": "20:00"})]
    third = s.seen[2]["messages"]
    assert third[2] == {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "check_availability", "arguments": '{"date": "2026-10-09"}'}}]}
    assert third[3] == {"role": "tool", "tool_call_id": "c1", "content": '{"slots": []}'}
    assert json.loads(third[5]["content"]) == {"ok": True, "booking": BOOKED}


def test_a_handoff_wins_and_other_tools_in_the_same_reply_are_not_run():
    executed = []
    s = Script(_tools(_call("create_booking", {}, "c1"),
                      _call("transfer_to_human", {"category": "safety"}, "c2")))
    r = _run(s, lambda n, a: executed.append(n) or {"ok": True})
    assert r.handoff == ToolCall("transfer_to_human", {"category": "safety"})
    assert executed == [] and r.text is None and r.steps == 1


def test_the_last_step_is_asked_exactly_like_the_others():
    """最後一步跟前面一樣只給工具規格、不帶 tool_choice。規格一定要帶:模型最後一步照樣會呼叫
    create_booking,看不到規格就漏帶必填的 weekday,日期防線擋下 → 客人收到 fallback
    (2026-10-15 真模型驗收)。tool_choice none 不送:NVIDIA 收到時 3 次裡 2 次漏 weekday
    (只給規格 3/3 齊全);OpenRouter 收到 none 的那次照樣回 create_booking,沒觀察到被遵守過,
    真的遵守時反而讓資料齊全的訂位成立不了。"""
    s = Script(_tools(_call("check_availability")),
               _tools(_call("check_availability", call_id="c2")),
               _text("請問要哪個時段?"))
    r = _run(s, lambda n, a: {"slots": []}, max_steps=3)
    assert [c["tools"] for c in s.seen] == [TOOLS, TOOLS, TOOLS]
    assert [c["kw"] for c in s.seen] == [{}, {}, {}]   # 沒有 tool_choice(也沒有別的參數)
    assert (r.text, r.steps) == ("請問要哪個時段?", 3)


def test_tool_calls_on_the_last_step_still_run_but_the_model_is_not_asked_again():
    """2026-10-08 實測:NVIDIA 在最後一步不帶工具、tool_choice none、關思考三種做法下
    都照樣回 create_booking。資料齊全的訂位不能因此丟掉 —— 照樣執行,但結果不再塞回
    對話問模型(沒有下一步了),回覆由 webhook 補(有成立預約 → 確認句)。"""
    executed = []

    def execute(name, args):
        executed.append(name)
        return {"slots": []} if name == "check_availability" else {"ok": True, "booking": BOOKED}

    s = Script(_tools(_call("check_availability")),
               _tools(_call("create_booking", call_id="c2")))
    r = _run(s, execute, max_steps=2)
    assert executed == ["check_availability", "create_booking"]
    assert r.booking == BOOKED
    assert r.text is None
    assert r.steps == 2
    assert r.tools_used == ("check_availability", "create_booking")
    assert len(s.seen) == 2  # 沒有第三次呼叫模型
    assert s.seen[1]["tools"] == TOOLS and "tool_choice" not in s.seen[1]["kw"]


def test_a_handoff_on_the_last_step_still_wins():
    executed = []
    s = Script(_tools(_call("check_availability")),
               _tools(_call("create_booking", {}, "c2"),
                      _call("transfer_to_human", {"category": "safety"}, "c3")))
    r = _run(s, lambda n, a: executed.append(n) or {"ok": True}, max_steps=2)
    assert r.handoff == ToolCall("transfer_to_human", {"category": "safety"})
    assert executed == ["check_availability"]  # 最後一步的 create_booking 沒執行
    assert r.text is None and r.booking is None and r.steps == 2


def test_text_written_before_last_step_tool_results_is_dropped():
    """最後一步的文字是在工具結果出來之前寫的,不是對預約結果的回答 —— 可能只是「好的,
    幫您處理」。留著的話,webhook 會把它當模型的答案送出,客人看不到確認句。丟掉文字,
    webhook 才會補程式組的確認句。"""
    s = Script(_tools(_call("check_availability")),
               _tools(_call("create_booking", call_id="c2"), text="好的,幫您處理"))
    r = _run(s, lambda n, a: {"ok": True, "booking": BOOKED} if n == "create_booking"
             else {"slots": []}, max_steps=2)
    assert r.text is None
    assert r.booking == BOOKED
    assert r.tools_used == ("check_availability", "create_booking")


def test_a_refused_booking_on_the_last_step_does_not_keep_a_text_that_claims_success():
    """模型在看到結果之前就寫了「已為您訂位」,而工具其實拒絕了(名額被搶):這句話
    不能送給客人 —— 沒有 booking、沒有文字,webhook 送的是 fallback。"""
    s = Script(_tools(_call("check_availability")),
               _tools(_call("create_booking", call_id="c2"), text="已為您訂位"))
    r = _run(s, lambda n, a: {"ok": False, "error": "剛好滿了"} if n == "create_booking"
             else {"slots": []}, max_steps=2)
    assert r.text is None
    assert r.booking is None
    assert r.tools_used == ("check_availability", "create_booking")


def test_a_failure_after_booking_still_reports_the_booking():
    s = Script(_tools(_call("create_booking")), LLMError("上游掛了"))
    r = _run(s, lambda n, a: {"ok": True, "booking": BOOKED})
    assert (r.text, r.booking, r.steps) == (None, BOOKED, 2)


BOOKED_SAT = {**BOOKED, "date": "2026-10-10", "weekday": "六"}


def _book_by_date(name, args):
    """create_booking 依參數裡的日期回對應的預約,其他工具回空的查詢結果。"""
    if name != "create_booking":
        return {"slots": []}
    return {"ok": True, "booking": BOOKED_SAT if args.get("date") == "2026-10-10" else BOOKED}


def test_two_bookings_on_the_last_step_are_both_reported_in_order():
    """最後一步回兩個 create_booking、兩筆都成立:只回最後一筆的話,客人只被告知 10/10,
    店員卻收到兩則推播 —— 客人不知道自己還有一筆 10/09。"""
    s = Script(_tools(_call("check_availability")),
               _tools(_call("create_booking", {"date": "2026-10-09"}, "c2"),
                      _call("create_booking", {"date": "2026-10-10"}, "c3")))
    r = _run(s, _book_by_date, max_steps=2)
    assert r.bookings == (BOOKED, BOOKED_SAT)
    assert r.booking == BOOKED_SAT   # 既有欄位照舊是最後一筆
    assert r.text is None


def test_two_bookings_followed_by_a_model_failure_are_both_reported():
    s = Script(_tools(_call("create_booking", {"date": "2026-10-09"}, "c1")),
               _tools(_call("create_booking", {"date": "2026-10-10"}, "c2")),
               LLMError("上游掛了"))
    r = _run(s, _book_by_date)
    assert (r.text, r.bookings, r.booking) == (None, (BOOKED, BOOKED_SAT), BOOKED_SAT)


def test_a_duplicate_of_the_same_slot_is_listed_once_where_it_first_happened():
    """模型重複呼叫同一時段,工具回 duplicate(帶著已存在的那一筆):同一筆對客人只講一次。"""
    def execute(name, args):
        out = _book_by_date(name, args)
        return {**out, "duplicate": True} if args.get("dup") else out

    s = Script(_tools(_call("create_booking", {"date": "2026-10-09"}, "c1")),
               _tools(_call("create_booking", {"date": "2026-10-10"}, "c2")),
               _tools(_call("create_booking", {"date": "2026-10-09", "dup": True}, "c3")),
               _text("好"))
    r = _run(s, execute)
    assert r.bookings == (BOOKED, BOOKED_SAT)
    assert r.booking == BOOKED   # 最後一個成立(含 duplicate)的結果


def test_no_booking_means_an_empty_bookings_tuple():
    assert _run(Script(_text("您好"))).bookings == ()


def test_an_unexpected_error_from_the_model_after_booking_still_reports_the_booking():
    """上游回了格式怪異的東西,解析時丟的不是 LLMError(例如 tool_calls 裡夾著字串):
    已經成立的預約不能因此掉成通用 fallback —— 結果要帶 booking、沒有文字,
    webhook 才會補確認句。"""
    s = Script(_tools(_call("create_booking")), RuntimeError("回應格式壞了"))
    r = _run(s, lambda n, a: {"ok": True, "booking": BOOKED})
    assert (r.text, r.booking, r.steps) == (None, BOOKED, 2)


def test_a_tool_that_raises_becomes_an_error_result_for_the_model():
    def boom(name, args):
        raise RuntimeError("db down")

    s = Script(_tools(_call("create_booking")), _text("抱歉,系統忙碌"))
    r = _run(s, boom)
    assert json.loads(s.seen[1]["messages"][-1]["content"]) == {
        "ok": False, "error": "系統暫時無法處理"}
    assert r.text == "抱歉,系統忙碌" and r.booking is None


def test_a_refused_booking_is_not_reported_as_booked():
    s = Script(_tools(_call("create_booking")), _text("那個時段滿了"))
    assert _run(s, lambda n, a: {"ok": False, "error": "剛好滿了"}).booking is None


def test_only_an_ok_result_with_a_booking_counts_as_booked():
    """ok 與 booking 缺一不可:被拒絕的結果就算帶著 booking 欄位也不算成立,
    ok 但沒有 booking(例如不是建立預約的工具)也不是預約。"""
    for out in ({"booking": BOOKED}, {"ok": False, "booking": BOOKED}, {"ok": True}):
        s = Script(_tools(_call("create_booking")), _text("好"))
        assert _run(s, lambda n, a, out=out: out).booking is None


def test_every_step_shares_one_deadline():
    s = Script(_tools(_call("check_availability")), _text("好"))
    before = time.monotonic()
    _run(s, lambda n, a: {"slots": []}, budget=45.0)
    first, second = s.seen[0]["deadline"], s.seen[1]["deadline"]
    assert first == second
    assert before + 44 < first <= time.monotonic() + 45


def test_the_loop_stops_when_the_budget_is_used_up():
    s = Script(_tools(_call("check_availability")), _text("不該走到這"), delay=0.3)
    r = _run(s, lambda n, a: {"slots": []}, budget=1.2)
    assert len(s.seen) == 1 and r.text is None and r.steps == 1


def test_tokens_are_summed_across_steps():
    s = Script(_tools(_call("check_availability")), _text("好", tokens=7))
    assert _run(s, lambda n, a: {}).token_count == 17


def test_latency_is_the_sum_of_each_steps_model_latency():
    """跟以前一樣記模型花了多久(test_webhook 斷言紀錄裡的 latency_ms 等於模型回報的值)。"""
    s = Script(LLMResult("", token_count=1, latency_ms=300,
                         tool_call=_call("check_availability"),
                         tool_calls=(_call("check_availability"),)),
               LLMResult("好", token_count=1, latency_ms=200))
    assert _run(s, lambda n, a: {}).latency_ms == 500


def test_a_result_that_only_fills_tool_call_still_counts_as_a_tool_call():
    """既有的測試替身只填 tool_call、沒填 tool_calls —— loop 要一樣認得。"""
    s = Script(LLMResult("", token_count=1, latency_ms=1,
                         tool_call=ToolCall("transfer_to_human", {"category": "safety"})))
    assert _run(s).handoff == ToolCall("transfer_to_human", {"category": "safety"})


def test_no_tools_means_tools_is_none_on_every_step():
    """沒有工具時每一步(含最後一步)都是 tools=None,也不帶 tool_choice。"""
    s = Script(_tools(_call("check_availability")), _text("好"))
    _run(s, tools=None, max_steps=2)
    assert [c["tools"] for c in s.seen] == [None, None]
    assert [c["kw"] for c in s.seen] == [{}, {}]
