from app.knowledge.validate import validate

HIGH_RISK = [
    {"level": "L3", "trigger": "提告 律師 消保官", "action": "transfer",
     "script": "這部分我請主管與您聯繫", "category": "legal"},
    {"level": "L3", "trigger": "受傷 過敏 送醫 症狀", "action": "transfer",
     "script": "我立刻請專人與您聯繫", "category": "safety"},
    {"level": "L2", "trigger": "退款 溢扣 重複扣款", "action": "transfer",
     "script": "我幫您轉給專員確認", "category": "money"},
    {"level": "L2", "trigger": "個資 刪除資料 隱私", "action": "transfer",
     "script": "我請專人協助您處理", "category": "privacy"},
]


def _ok_data(**over):
    data = {
        "company": {
            "name": "測試公司", "industry": "ecommerce",
            "hours": "10:00-19:00", "contact": "02-1234-5678",
            "tone": "專業親切", "forbidden_phrases": ["保證", "絕對"],
        },
        "faq": [{"q": "出貨要多久", "a": "下單後兩到三個工作天寄出。"}],
        "policies": [{"title": "退換貨", "content": "到貨七日內可退換。"}],
        "escalation": list(HIGH_RISK),
        "glossary": [{"term": "到貨付款", "meaning": "收到商品時再付錢。"}],
    }
    data.update(over)
    return data


def _levels(findings, rule):
    return [f.level for f in findings if f.rule == rule]


def test_clean_data_has_no_findings():
    assert validate(_ok_data()) == []


def test_rule2_missing_required_field_is_error():
    d = _ok_data()
    del d["company"]["forbidden_phrases"]
    assert "ERROR" in _levels(validate(d), 2)


def test_rule3_faq_answer_violating_own_forbidden_phrase_is_error():
    """最重要的一條:自己的答案違反自己的禁語清單。"""
    d = _ok_data(faq=[{"q": "會不會壞", "a": "我們保證絕對不會壞。"}])
    assert "ERROR" in _levels(validate(d), 3)


def test_rule4_overlong_answer_is_error():
    d = _ok_data(faq=[{"q": "詳細說明", "a": "字" * 6000}])
    assert "ERROR" in _levels(validate(d), 4)


def test_rule5_missing_medical_escalation_is_block():
    d = _ok_data(escalation=[e for e in HIGH_RISK if e["category"] != "safety"])
    assert "BLOCK" in _levels(validate(d), 5)


def test_rule6_placeholder_is_block():
    d = _ok_data(policies=[{"title": "保固", "content": "請填入保固條款"}])
    assert "BLOCK" in _levels(validate(d), 6)


def test_rule7_glossary_term_colliding_with_forbidden_phrase_is_error():
    d = _ok_data(glossary=[{"term": "保證", "meaning": "廠商的承諾。"}])
    assert "ERROR" in _levels(validate(d), 7)


def test_rule8_unparseable_hours_is_error():
    d = _ok_data()
    d["company"]["hours"] = "看心情"
    assert "ERROR" in _levels(validate(d), 8)


def test_block_does_not_imply_error():
    """BLOCK 不擋 seed。混淆這兩個等級會讓人以為資料壞了。"""
    d = _ok_data(policies=[{"title": "保固", "content": "TODO"}])
    findings = validate(d)
    assert any(f.level == "BLOCK" for f in findings)
    assert not any(f.level == "ERROR" for f in findings)


def test_rule2_escalation_missing_script_is_error():
    """臨界情況:法律類別存在 (rule 5 過) 但 script 為空 (rule 2 該攔)。
    客人說「我要提告」,系統沉默。這是 constraint 8 的反面。"""
    d = _ok_data(escalation=[
        {"level": "L3", "trigger": "提告 律師 消保官", "action": "transfer",
         "script": "", "category": "legal"}
    ])
    assert "ERROR" in _levels(validate(d), 2)


def test_rule2_policies_missing_content_is_error():
    d = _ok_data(policies=[{"title": "退換貨"}])
    assert "ERROR" in _levels(validate(d), 2)


def test_rule2_escalation_missing_trigger_is_error():
    d = _ok_data(escalation=[
        {"level": "L3", "action": "transfer",
         "script": "這部分我請主管與您聯繫", "category": "legal"}
    ])
    assert "ERROR" in _levels(validate(d), 2)


def test_rule2_escalation_missing_action_is_error():
    d = _ok_data(escalation=[
        {"level": "L3", "trigger": "提告 律師 消保官",
         "script": "這部分我請主管與您聯繫", "category": "legal"}
    ])
    assert "ERROR" in _levels(validate(d), 2)


def test_rule2_escalation_missing_level_is_error():
    d = _ok_data(escalation=[
        {"trigger": "提告 律師 消保官", "action": "transfer",
         "script": "這部分我請主管與您聯繫", "category": "legal"}
    ])
    assert "ERROR" in _levels(validate(d), 2)


def test_rule2_glossary_missing_term_is_error():
    d = _ok_data(glossary=[{"meaning": "廠商的承諾。"}])
    assert "ERROR" in _levels(validate(d), 2)


def test_rule2_glossary_missing_meaning_is_error():
    d = _ok_data(glossary=[{"term": "保證"}])
    assert "ERROR" in _levels(validate(d), 2)


def test_rule4_respects_max_text_length_parameter():
    """Rule 4 讀取參數而非硬編碼 5000。"""
    d = _ok_data(faq=[{"q": "短問題", "a": "字" * 100}])
    # 100 字 < 5000,預設應該通過
    assert "ERROR" not in _levels(validate(d), 4)
    # 但 max_text_length=50 時應該失敗
    assert "ERROR" in _levels(validate(d, max_text_length=50), 4)


def test_rule8_accepts_24hour_format():
    """24 小時營業應該通過,不是「解析不了」。"""
    d = _ok_data()
    d["company"]["hours"] = "24小時"
    assert "ERROR" not in _levels(validate(d), 8)

    d["company"]["hours"] = "24 小時營業"
    assert "ERROR" not in _levels(validate(d), 8)

    d["company"]["hours"] = "全天營業"
    assert "ERROR" not in _levels(validate(d), 8)
