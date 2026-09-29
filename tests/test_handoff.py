"""轉真人判斷的測試。

規則層是安全攸關的保底,它壞掉的方式通常不是報錯,而是「該轉沒轉」
或「什麼都轉」—— 兩種都不會有錯誤訊息,所以每一種都要有測試守著。
"""

from datetime import datetime, timedelta, timezone

from app.agent.handoff import (
    EXPIRED_PREFIX, GENERIC_SCRIPT, TOOL_NAME, Decision, as_utc, build_tool,
    customer_label, decision_from_recited_script, decision_from_tool,
    enter_human_mode, is_still_human, match_keyword, return_to_ai, staff_message,
)
from app.models import ConversationMode, User

RULES = [
    {"level": "L3", "category": "safety", "trigger": "過敏、食物中毒、送醫",
     "action": "transfer", "script": "這件事我立刻請店長與您聯繫。"},
    {"level": "L3", "category": "legal", "trigger": "提告、律師",
     "action": "transfer", "script": "這部分我請主管直接與您說明。"},
    {"level": "L1", "category": "service", "trigger": "上菜太慢、態度不佳",
     "action": "apologize", "script": "很抱歉讓您有這樣的感受。"},
]

NOW = datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)


# --- 規則層 -------------------------------------------------------------------

def test_a_trigger_word_anywhere_in_the_message_is_a_hit():
    d = match_keyword(RULES, "我朋友吃完過敏送醫了")
    assert d == Decision("safety", "這件事我立刻請店長與您聯繫。", "關鍵字「過敏」")


def test_apologize_rules_never_transfer():
    """L1 不需要轉真人,道歉本來就是模型做得好的事。"""
    assert match_keyword(RULES, "你們上菜太慢了") is None


def test_no_trigger_word_is_no_hit():
    assert match_keyword(RULES, "有停車位嗎") is None


def test_the_first_matching_rule_wins():
    """yaml 裡 L3 safety 排在最前面 —— 同時命中時安全優先。"""
    assert match_keyword(RULES, "我要提告,我朋友過敏送醫").category == "safety"


def test_an_empty_word_from_a_stray_separator_matches_nothing():
    """trigger 寫成「過敏、」時切出來會有空字串,而 "" in 任何字串都是 True ——
    不擋的話,每一則訊息都會被轉真人。"""
    rules = [{"category": "safety", "trigger": "過敏、", "action": "transfer",
              "script": "s"}]
    assert match_keyword(rules, "有停車位嗎") is None


# --- 工具定義 -----------------------------------------------------------------

def test_the_tool_only_offers_transfer_categories():
    tool = build_tool(RULES)
    assert tool["function"]["name"] == TOOL_NAME
    enum = tool["function"]["parameters"]["properties"]["category"]["enum"]
    assert enum == ["safety", "legal"]


def test_the_tool_description_carries_the_trigger_words():
    """模型要靠描述知道每一類大概是什麼情況,才判斷得出換句話說的版本。"""
    assert "過敏" in build_tool(RULES)["function"]["description"]


def test_no_transfer_rules_means_no_tool():
    assert build_tool([RULES[2]]) is None
    assert build_tool([]) is None


def test_a_known_category_from_the_model_uses_that_script():
    d = decision_from_tool(RULES, {"category": "legal", "reason": "客人說要找律師"})
    assert d == Decision("legal", "這部分我請主管直接與您說明。", "AI —— 客人說要找律師")


def test_an_unknown_category_still_transfers_with_the_generic_script():
    """模型已經表達「需要真人」,類別寫錯不該讓客人被漏掉。"""
    d = decision_from_tool(RULES, {"category": "weather"})
    assert d.category is None
    assert d.script == GENERIC_SCRIPT


def test_empty_arguments_still_transfer():
    """Task 1 在參數 JSON 壞掉時給的就是 {}。"""
    assert decision_from_tool(RULES, {}).script == GENERIC_SCRIPT


# --- 安全網:模型把 script 念出來,而不是呼叫工具(F1b) --------------------

def test_reciting_a_transfer_script_verbatim_is_treated_as_a_handoff():
    """模型該呼叫工具卻只是把 script 念出來:客人以為已經轉接,實際上
    HUMAN 沒切、店員沒收到通知 —— 這裡補一道安全網,把它當成真的轉了。"""
    d = decision_from_recited_script(RULES, "這件事我立刻請店長與您聯繫。")
    assert d == Decision("safety", "這件事我立刻請店長與您聯繫。",
                         "AI —— 念出轉接話術")


def test_reciting_with_surrounding_whitespace_still_counts():
    """比對用 verbatim(去頭尾空白)—— 模型偶爾會多帶換行。"""
    d = decision_from_recited_script(RULES, "\n這件事我立刻請店長與您聯繫。\n")
    assert d is not None and d.category == "safety"


def test_a_normal_answer_is_not_mistaken_for_a_recited_script():
    assert decision_from_recited_script(RULES, "門口兩格車位,滿了對面有收費停車場。") is None


def test_reciting_an_apologize_script_never_counts():
    """L1 apologize 本來就該由模型自己說,不是轉真人的訊號。"""
    assert decision_from_recited_script(RULES, "很抱歉讓您有這樣的感受。") is None


# --- 模式與時間 ---------------------------------------------------------------

def test_naive_datetimes_from_sqlite_are_marked_utc():
    assert as_utc(datetime(2026, 9, 24, 8, 0)) == NOW
    assert as_utc(None) is None


def test_ai_mode_is_not_human():
    assert not is_still_human(User(mode=ConversationMode.AI), NOW)


def test_human_mode_before_the_deadline_is_still_human():
    u = User(mode=ConversationMode.HUMAN, mode_expires_at=NOW + timedelta(minutes=1))
    assert is_still_human(u, NOW)


def test_human_mode_after_the_deadline_is_over():
    u = User(mode=ConversationMode.HUMAN, mode_expires_at=NOW - timedelta(minutes=1))
    assert not is_still_human(u, NOW)


def test_a_naive_deadline_read_back_from_sqlite_does_not_crash():
    """SQLite 讀回來 tzinfo=None,直接跟有時區的 now 比會 TypeError ——
    而那個例外會被 webhook 最外層接走,客人收到的是 fallback。"""
    u = User(mode=ConversationMode.HUMAN, mode_expires_at=datetime(2026, 9, 24, 7, 59))
    assert not is_still_human(u, NOW)


def test_entering_human_mode_sets_the_deadline():
    u = User(mode=ConversationMode.AI)
    enter_human_mode(u, timeout_minutes=30, now=NOW)
    assert u.mode == ConversationMode.HUMAN
    assert u.mode_changed_at == NOW
    assert u.mode_expires_at == NOW + timedelta(minutes=30)


def test_returning_to_ai_clears_the_deadline():
    u = User(mode=ConversationMode.HUMAN, mode_expires_at=NOW)
    return_to_ai(u, NOW)
    assert u.mode == ConversationMode.AI and u.mode_expires_at is None


# --- 給店員的通知 -------------------------------------------------------------

def test_customer_label_prefers_the_display_name():
    assert customer_label(User(line_user_id="U1234567890", display_name="王小明")) == "王小明"
    assert customer_label(User(line_user_id="U1234567890")) == "客人 …567890"


def test_staff_message_says_what_happened_and_why():
    d = Decision("safety", "s", "關鍵字「過敏」")
    msg = staff_message(d, "王小明", "我朋友吃完過敏送醫了")
    assert msg.splitlines() == ["【安全】王小明", "我朋友吃完過敏送醫了",
                                "判斷依據:關鍵字「過敏」"]


def test_staff_message_truncates_long_messages():
    msg = staff_message(Decision("safety", "s", "b"), "王小明", "過" * 300)
    assert "過" * 100 in msg and "過" * 101 not in msg


def test_staff_message_for_an_unknown_category():
    msg = staff_message(Decision(None, GENERIC_SCRIPT, "AI —— x"), "王小明", "嗨")
    assert msg.startswith("【轉真人】")


def test_the_expired_prefix_is_a_complete_sentence():
    """它會直接接在模型答案前面,少了句號兩句話會黏在一起。"""
    assert EXPIRED_PREFIX.endswith("。")
