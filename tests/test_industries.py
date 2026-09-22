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
