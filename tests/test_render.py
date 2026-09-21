import pytest
from jinja2 import UndefinedError

from app.knowledge.render import render_system_prompt

DATA = {
    "company": {"name": "微醺之夜", "industry": "restaurant", "hours": "17:00-24:00",
                "contact": "02-1111-2222", "tone": "親切但不過度熱情",
                "forbidden_phrases": ["保證", "一定沒問題"]},
    "faq": [{"q": "有停車位嗎", "a": "店門口有兩格,滿了可停對面收費停車場。"}],
    "policies": [{"title": "訂位保留", "content": "逾時十五分鐘不保留。"}],
    "escalation": [{"level": "L3", "trigger": "過敏 送醫", "action": "transfer",
                    "script": "我立刻請主管與您聯繫", "category": "safety"}],
    "glossary": [{"term": "低消", "meaning": "每位客人最少的消費金額。"}],
}


def test_prompt_contains_company_identity():
    p = render_system_prompt(DATA)
    assert "微醺之夜" in p and "17:00-24:00" in p


def test_prompt_lists_forbidden_phrases():
    p = render_system_prompt(DATA)
    assert "保證" in p and "一定沒問題" in p


def test_prompt_contains_faq_and_escalation():
    p = render_system_prompt(DATA)
    assert "有停車位嗎" in p
    assert "我立刻請主管與您聯繫" in p


def test_prompt_does_not_leak_jinja_syntax():
    p = render_system_prompt(DATA)
    assert "{{" not in p and "{%" not in p


def test_render_raises_when_data_is_missing_a_referenced_field():
    """StrictUndefined 是刻意的:樣板引用資料裡沒有的欄位要當場報錯,
    不要默默算成空字串——那會變成一個少了半段人設的客服。這條測試守著
    這個決策本身:上面四條測試給的 DATA 都是完整的,就算 render.py
    改回 Jinja2 預設的 Undefined(靜默算成空字串),那四條也不會發現,
    要靠故意缺一個欄位才能驗到。
    """
    incomplete = {**DATA, "company": {k: v for k, v in DATA["company"].items()
                                       if k != "contact"}}
    with pytest.raises(UndefinedError):
        render_system_prompt(incomplete)
