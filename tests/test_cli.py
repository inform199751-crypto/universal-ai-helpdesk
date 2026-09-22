import io
import sys

import pytest

from app.cli import main, run_seed, run_validate
from app.crypto import decrypt
from app.database import Base, engine, session_scope
from app.models import Company


@pytest.fixture(autouse=True)
def _tables():
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


def test_restaurant_industry_has_no_errors():
    findings = run_validate("restaurant")
    assert [f for f in findings if f.level == "ERROR"] == []


def test_seed_creates_company_with_encrypted_credentials():
    cid = run_seed("restaurant", slug="bistro",
                   channel_secret="s3cret", channel_token="t0ken")
    with session_scope() as db:
        c = db.get(Company, cid)
        assert c.slug == "bistro"
        assert c.system_prompt  # 不是空字串
        assert decrypt(c.line_channel_secret_enc) == "s3cret"


def test_seed_is_idempotent_on_same_slug():
    """重跑 seed 應該更新同一列,不是新增一列 —— 否則改個 FAQ 就多一家公司。"""
    a = run_seed("restaurant", slug="bistro", channel_secret="s", channel_token="t")
    b = run_seed("restaurant", slug="bistro", channel_secret="s", channel_token="t")
    assert a == b
    with session_scope() as db:
        assert db.query(Company).count() == 1


def test_seed_creates_separate_company_for_different_slug():
    """補強:上一條測試兩次呼叫的 slug 相同,就算 upsert 寫成「隨便抓現有的第一列」
    也會矇混過關(全表本來就只有一列)。這裡用兩個不同的 slug 各 seed 一次,
    確認 upsert 真的是以 slug 為鍵——用同一個 slug 才更新,不同 slug 要各自成列。
    """
    a = run_seed("restaurant", slug="bistro-a", channel_secret="s", channel_token="t")
    b = run_seed("restaurant", slug="bistro-b", channel_secret="s", channel_token="t")
    assert a != b
    with session_scope() as db:
        assert db.query(Company).count() == 2


def test_seed_refuses_and_writes_nothing_when_validate_reports_errors(monkeypatch):
    """run_seed 在資料有 ERROR 時拒絕寫入,是這整個任務最關鍵的安全閂——
    Task 4 的 ERROR/BLOCK 分級存在的唯一理由,就是讓壞資料不可能寫進
    Company 表。只驗有沒有丟 SystemExit 不夠:「丟了例外」跟「真的沒
    寫入」是兩件不同的事,一個鬆掉 ERROR 篩選條件、或把 validate 和
    寫入順序對調的重構,可能只做到前者卻仍然寫壞資料進去。兩者都驗。

    用 monkeypatch 直接換掉 _load,不去動 industries/restaurant/ 底下
    那份要保持乾淨可用的真實資料。
    """
    bad_data = {
        "company": {"name": "壞公司", "industry": "restaurant", "hours": "17:00-24:00",
                     "contact": "02-0000-0000", "tone": "測試用",
                     "forbidden_phrases": ["保證"]},
        # 答案命中自己的禁語清單 → validate 規則 3 判 ERROR
        "faq": [{"q": "測試問題", "a": "我們保證絕對可以處理。"}],
        "policies": [], "escalation": [], "glossary": [],
    }
    monkeypatch.setattr("app.cli._load", lambda industry: bad_data)

    with pytest.raises(SystemExit):
        run_seed("restaurant", slug="broken", channel_secret="s", channel_token="t")

    with session_scope() as db:
        assert db.query(Company).count() == 0


def test_main_validate_smoke_survives_non_utf8_stdout(monkeypatch):
    """main() 目前沒有任何測試呼叫過它——這正是 stdout 編碼那個 bug
    (cp950 主控台印不出「✓」就整個崩潰)可以躲在全綠測試套件後面的
    原因:所有其他測試都只呼叫 run_validate/run_seed,從不經過
    main()/_report() 真正 print 的那幾行。

    pytest 的 capsys/capfd 兩個 fixture 都會把 stdout 強制換成
    UTF-8(已經用探測測試驗證過:兩者接 print('✓') 都不會出事),
    不會重現這個 bug,所以這裡不用它們,直接換一個明確用 cp950
    (繁體中文 Windows 的系統編碼,也是這個 bug 實際發生的編碼)包起來
    的 stdout 替身,對到真正會出事的條件。
    """
    fake_stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp950", newline="")
    monkeypatch.setattr(sys, "stdout", fake_stdout)

    rc = main(["validate", "--industry", "restaurant"])

    fake_stdout.flush()
    fake_stdout.seek(0)
    printed = fake_stdout.buffer.getvalue().decode("utf-8")
    assert rc == 0
    assert printed.strip()


def test_seed_stores_destination_when_given():
    """destination 是 bot 自己的 userId。router 拿它跟 webhook body 交叉比對:
    路徑說是 A 公司、body 的 destination 卻是 B 公司的 bot,就拒絕(決策 2)。"""
    cid = run_seed("restaurant", slug="bistro", channel_secret="s",
                   channel_token="t", destination="Ubot0001")
    with session_scope() as db:
        assert db.get(Company, cid).line_destination == "Ubot0001"


def test_seed_strips_destination():
    """Global Constraint 3。destination 是拿去逐字元比對的,貼上時黏到尾端
    換行,結果不是報錯而是「每一則訊息都被判成 destination 不符」——
    官方帳號整個啞掉,而錯誤訊息指不到真正原因。"""
    cid = run_seed("restaurant", slug="bistro", channel_secret="s",
                   channel_token="t", destination="  Ubot0001\n")
    with session_scope() as db:
        assert db.get(Company, cid).line_destination == "Ubot0001"


def test_seed_without_destination_warns_that_the_check_is_off(capsys):
    """不帶 --destination 是合法的 —— 第一次串接時還拿不到 bot 的 userId。

    但 router 的交叉比對寫的是 `if expected_destination and ...`,欄位是
    NULL 就整條靜靜跳過。安靜地少一道防線,比明講出來危險得多,所以這裡
    必須印出警告。
    """
    rc = main(["seed", "--industry", "restaurant", "--slug", "bistro",
               "--channel-secret", "s", "--channel-token", "t"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "destination" in out and "--destination" in out
