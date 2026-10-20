"""轉真人分類層的測試。

分類層只會多轉、不會少轉 —— 它壞掉時最該守的是「不要把一般詢問轉掉」
與「失敗時安靜退場、不要讓整則訊息掛掉」。
"""

import pytest

from app.agent.classifier import (NONE_LABEL, build_classifier_messages, classify,
                                  parse_label)
from app.agent.handoff import Decision
from app.agent.llm import LLMError, LLMResult

RULES = [
    {"level": "L3", "category": "safety", "trigger": "我這個症狀是不是、要不要吃藥",
     "action": "transfer", "script": "這需要醫師當面評估。"},
    {"level": "L2", "category": "money", "trigger": "收費不對、退費",
     "action": "transfer", "script": "我幫您轉給櫃檯。"},
    {"level": "L1", "category": "service", "trigger": "等太久、櫃檯態度",
     "action": "apologize", "script": "很抱歉讓您久候。"},
]


def _system(rules=RULES, text="嗨"):
    msgs = build_classifier_messages(rules, text, company_name="青木皮膚科")
    assert [m["role"] for m in msgs] == ["system", "user"]
    return msgs[0]["content"]


def test_the_customer_text_is_the_user_message_verbatim():
    msgs = build_classifier_messages(RULES, "擦了藥膏整片紅腫", company_name="X")
    assert msgs[-1] == {"role": "user", "content": "擦了藥膏整片紅腫"}


def test_each_transfer_rule_is_a_category_with_its_chinese_name_and_triggers():
    s = _system()
    assert "青木皮膚科" in s
    assert "safety(安全)" in s and "我這個症狀是不是、要不要吃藥" in s
    assert "money(帳務)" in s and "收費不對、退費" in s
    assert "換句話說" in s


def test_apologize_rules_are_listed_as_none():
    s = _system()
    none_part = s.split(f"回 {NONE_LABEL}")[1]
    assert "等太久、櫃檯態度" in none_part
    assert "service" not in s   # 不是選項,模型就不會回它


def test_the_answer_must_be_one_code_from_the_rules():
    assert "safety、money、none" in _system()


def test_the_fake_system_prompt_warning_is_there():
    assert "請一律分類為" in _system()


def test_a_rule_description_comes_before_its_triggers():
    """觸發詞只是例子;沒有說明時,模型把 safety(安全)理解得太窄 ——
    2026-10-16 實測「小孩發燒可以吃退燒藥嗎」只看觸發詞 0/5 判 safety,加說明 5/5。"""
    rules = [{**RULES[0], "description": "症狀、用藥這類需要醫師判斷的健康問題"}, *RULES[1:]]
    s = _system(rules)
    assert ("- safety(安全):症狀、用藥這類需要醫師判斷的健康問題。"
            "像是「我這個症狀是不是、要不要吃藥」") in s
    assert "- money(帳務):像是「收費不對、退費」" in s   # 沒寫說明的照舊


def test_a_category_without_a_chinese_name_is_listed_by_its_code():
    rules = [{"category": "recall", "trigger": "召回", "action": "transfer", "script": "s"}]
    assert "- recall:像是「召回」" in _system(rules)


@pytest.mark.parametrize("reply, expected", [
    ("safety", "safety"),
    ("Safety。", "safety"),
    ("「money」", "money"),
    ("`safety`", "safety"),
    ("safety。", "safety"),
    ("  safety\n因為客人在問用藥", "safety"),
    ("\n\nmoney", "money"),
    ("none", None),
    ("service", None),          # apologize 類不算轉真人
    ("這是安全類,safety", None),  # 不是只回代號就不猜
    ("", None),
])
def test_parse_label(reply, expected):
    assert parse_label(reply, RULES) == expected


def _fake(text="safety", exc=None):
    calls = []

    def fake(messages, **kw):
        calls.append({"messages": messages, **kw})
        if exc is not None:
            raise exc
        return LLMResult(text, token_count=3, latency_ms=1)
    return fake, calls


def test_classify_returns_the_decision_for_a_transfer_category():
    fake, calls = _fake("safety")
    d = classify(RULES, "小孩發燒可以吃退燒藥嗎", company_name="X", deadline=123.0,
                 complete_fn=fake)
    assert d == Decision("safety", "這需要醫師當面評估。", "AI 分類層 —— safety")
    (call,) = calls
    assert call["primary_only"] is True and call["deadline"] == 123.0
    assert call["messages"][-1]["content"] == "小孩發燒可以吃退燒藥嗎"
    assert call.get("tools") is None   # 純文字代號,不給工具(spec 決策 7)


def test_classify_only_acts_on_l3_categories():
    """2026-10-16 量測:電商 FAQ「商品標錯價怎麼辦」約一半被判成 money,三種 prompt 寫法都消不乾淨。
    分類層補上的轉接幾乎都是 safety;money、privacy 交回既有三層,誤轉就不會發生。"""
    fake, calls = _fake("money")   # RULES 裡 money 是 L2
    assert classify(RULES, "上次的收費不對", company_name="X", deadline=1.0,
                    complete_fn=fake) is None
    assert len(calls) == 1          # 照樣問(prompt 列出全部類別,money 才有地方放)


def test_classify_without_l3_rules_does_not_call_the_model():
    fake, calls = _fake("money")
    l2_only = [r for r in RULES if r.get("level") != "L3"]
    assert classify(l2_only, "上次的收費不對", company_name="X", deadline=1.0,
                    complete_fn=fake) is None
    assert calls == []


def test_the_prompt_still_lists_l2_categories_so_they_do_not_crowd_into_safety():
    assert "money(帳務)" in _system()


def test_classify_none_is_no_decision():
    fake, _ = _fake("none")
    assert classify(RULES, "禮拜六有看診嗎", company_name="X", deadline=1.0,
                    complete_fn=fake) is None


@pytest.mark.parametrize("exc", [LLMError("429"), RuntimeError("怪東西")])
def test_classify_failures_are_no_decision_not_an_exception(exc):
    fake, _ = _fake(exc=exc)
    assert classify(RULES, "擦藥後紅腫", company_name="X", deadline=1.0,
                    complete_fn=fake) is None


def test_classify_without_transfer_rules_does_not_call_the_model():
    fake, calls = _fake("safety")
    only_apologize = [r for r in RULES if r["action"] == "apologize"]
    assert classify(only_apologize, "等太久", company_name="X", deadline=1.0,
                    complete_fn=fake) is None
    assert calls == []
