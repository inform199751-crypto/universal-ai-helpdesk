import pytest

from app.cli import run_validate
from app.knowledge.loader import load_industry
from app.cli import INDUSTRIES

ALL = ["restaurant", "ecommerce", "clinic"]


@pytest.mark.parametrize("industry", ALL)
def test_industry_has_all_five_files(industry):
    data = load_industry(INDUSTRIES / industry)
    assert set(data) == {"company", "faq", "policies", "escalation", "glossary"}


@pytest.mark.parametrize("industry", ALL)
def test_industry_validates_without_errors(industry):
    assert [f for f in run_validate(industry) if f.level == "ERROR"] == []


@pytest.mark.parametrize("industry", ALL)
def test_industry_covers_all_high_risk_categories(industry):
    data = load_industry(INDUSTRIES / industry)
    covered = {e["category"] for e in data["escalation"]}
    assert {"legal", "safety", "money", "privacy"} <= covered


@pytest.mark.parametrize("industry", ALL)
def test_industry_has_enough_faq_entries(industry):
    """每個行業至少十二筆 FAQ。

    這是計畫對每一份 faq.yaml 的明文要求,但 validate 不管數量 ——
    只寫三筆也會「驗證通過」,然後這個行業在 demo 時問第四個問題就露餡。
    """
    assert len(load_industry(INDUSTRIES / industry)["faq"]) >= 12


def test_clinic_refuses_to_diagnose():
    """診所版必須有「不做診斷」這條 —— 這是這個行業存在於本專題的理由。"""
    data = load_industry(INDUSTRIES / "clinic")
    scripts = " ".join(e["script"] for e in data["escalation"])
    forbidden = " ".join(data["company"]["forbidden_phrases"])
    assert "診斷" in forbidden or "診斷" in scripts


def test_three_industries_are_actually_different():
    """避免複製貼上沒改內容。"""
    names = {load_industry(INDUSTRIES / i)["company"]["name"] for i in ALL}
    assert len(names) == 3


# --- 診所:病情判斷的說法要抓得到 --------------------------------------------

@pytest.mark.parametrize("text", [
    "我這個是癌症嗎",          # 2026-10-01 真機原句:舊的 trigger 全是長句,一個都沒命中
    "這顆會不會是腫瘤",
    "我這個症狀是不是癌症",    # 原本就抓得到的,不能因為改 trigger 而掉
])
def test_clinic_catches_diagnosis_questions_with_the_keyword_layer(text):
    """關鍵字層是安全路徑的保底 —— 免費模型判斷不穩(真機 5 次只轉 3 次),
    「是不是癌症」這種問題不能只靠模型。trigger 寫成整句的話,客人少一個字
    就對不到。"""
    from app.agent.handoff import match_keyword

    rules = load_industry(INDUSTRIES / "clinic")["escalation"]
    decision = match_keyword(rules, text)
    assert decision is not None and decision.category == "safety"
