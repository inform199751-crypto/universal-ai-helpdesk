import io
import sys

import pytest

from app.cli import main, run_seed, run_validate
from app.crypto import decrypt
from app.database import Base, engine, session_scope
from app.models import ChatHistory, ChatRole, Company, User

# 用真實形狀的假憑證。以前這裡寫的是 "s" / "t" —— 那種值在真實世界不可能
# 存在,所以「1 個字元的 secret 被照單全收」這個 bug 就躲在全綠的測試後面,
# 一路躲到真的接上 LINE 才爆。
SECRET = "0123456789abcdef0123456789abcdef"   # channel secret:32 位十六進位
TOKEN = "T" + "k" * 171                        # 長期 access token 約 172 字元


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
                   channel_secret=SECRET, channel_token=TOKEN)
    with session_scope() as db:
        c = db.get(Company, cid)
        assert c.slug == "bistro"
        assert c.system_prompt  # 不是空字串
        assert decrypt(c.line_channel_secret_enc) == SECRET


def test_seed_is_idempotent_on_same_slug():
    """重跑 seed 應該更新同一列,不是新增一列 —— 否則改個 FAQ 就多一家公司。"""
    a = run_seed("restaurant", slug="bistro", channel_secret=SECRET, channel_token=TOKEN)
    b = run_seed("restaurant", slug="bistro", channel_secret=SECRET, channel_token=TOKEN)
    assert a == b
    with session_scope() as db:
        assert db.query(Company).count() == 1


def test_seed_creates_separate_company_for_different_slug():
    """補強:上一條測試兩次呼叫的 slug 相同,就算 upsert 寫成「隨便抓現有的第一列」
    也會矇混過關(全表本來就只有一列)。這裡用兩個不同的 slug 各 seed 一次,
    確認 upsert 真的是以 slug 為鍵——用同一個 slug 才更新,不同 slug 要各自成列。
    """
    a = run_seed("restaurant", slug="bistro-a", channel_secret=SECRET, channel_token=TOKEN)
    b = run_seed("restaurant", slug="bistro-b", channel_secret=SECRET, channel_token=TOKEN)
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
        run_seed("restaurant", slug="broken", channel_secret=SECRET, channel_token=TOKEN)

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
    cid = run_seed("restaurant", slug="bistro", channel_secret=SECRET,
                   channel_token=TOKEN, destination="Ubot0001")
    with session_scope() as db:
        assert db.get(Company, cid).line_destination == "Ubot0001"


def test_seed_strips_destination():
    """Global Constraint 3。destination 是拿去逐字元比對的,貼上時黏到尾端
    換行,結果不是報錯而是「每一則訊息都被判成 destination 不符」——
    官方帳號整個啞掉,而錯誤訊息指不到真正原因。"""
    cid = run_seed("restaurant", slug="bistro", channel_secret=SECRET,
                   channel_token=TOKEN, destination="  Ubot0001\n")
    with session_scope() as db:
        assert db.get(Company, cid).line_destination == "Ubot0001"


def test_seed_without_destination_warns_that_the_check_is_off(capsys):
    """不帶 --destination 是合法的 —— 第一次串接時還拿不到 bot 的 userId。

    但 router 的交叉比對寫的是 `if expected_destination and ...`,欄位是
    NULL 就整條靜靜跳過。安靜地少一道防線,比明講出來危險得多,所以這裡
    必須印出警告。
    """
    rc = main(["seed", "--industry", "restaurant", "--slug", "bistro",
               "--channel-secret", SECRET, "--channel-token", TOKEN])
    assert rc == 0
    out = capsys.readouterr().out
    assert "destination" in out and "--destination" in out


def _add_history(company_id: str, line_user_id: str, n: int = 2) -> None:
    with session_scope() as db:
        user = User(company_id=company_id, line_user_id=line_user_id)
        db.add(user)
        db.flush()
        for i in range(n):
            db.add(ChatHistory(company_id=company_id, user_id=user.id,
                               role=ChatRole.USER, content=f"訊息{i}",
                               line_message_id=f"{line_user_id}-{i}"))


def test_seed_rejects_a_one_character_channel_secret():
    """真實踩過的坑。在 PowerShell 主控台按 Ctrl+V 不是貼上,是塞進一個
    字面上的控制字元 ^V,於是 secret 變成 1 個字元 —— 而 run_seed 照單全收、
    加密、寫進資料庫,完全不報錯。症狀要到 LINE Console 按 Verify 回 401
    才出現,那時人會去查 webhook 網址,查不到真正原因。"""
    with pytest.raises(SystemExit):
        run_seed("restaurant", slug="bistro", channel_secret="\x16",
                 channel_token=TOKEN)
    with session_scope() as db:
        assert db.query(Company).count() == 0


def test_seed_rejects_a_channel_secret_that_is_not_hex():
    with pytest.raises(SystemExit):
        run_seed("restaurant", slug="bistro", channel_secret="z" * 32,
                 channel_token=TOKEN)


def test_seed_rejects_a_too_short_channel_token():
    with pytest.raises(SystemExit):
        run_seed("restaurant", slug="bistro", channel_secret=SECRET,
                 channel_token="too-short")
    with session_scope() as db:
        assert db.query(Company).count() == 0


def test_seed_strips_credentials():
    """Global Constraint 3。從網頁複製很容易連尾端換行一起帶走,而含換行的
    HTTP 標頭會被整個丟掉 —— 上游只會回「Missing Authentication header」,
    完全指不到真正原因。"""
    cid = run_seed("restaurant", slug="bistro",
                   channel_secret=f"  {SECRET}\n", channel_token=f"{TOKEN}\r\n")
    with session_scope() as db:
        c = db.get(Company, cid)
        assert decrypt(c.line_channel_secret_enc) == SECRET
        assert decrypt(c.line_channel_token_enc) == TOKEN


def test_updating_an_existing_company_does_not_require_credentials():
    """改店名、換行業、補 destination 都只是改資料,不該逼人再把憑證從
    LINE Console 複製一次 —— 那一步正是整個導入流程裡最容易出錯的地方。"""
    cid = run_seed("restaurant", slug="bistro",
                   channel_secret=SECRET, channel_token=TOKEN)
    again = run_seed("clinic", slug="bistro")
    assert again == cid
    with session_scope() as db:
        c = db.get(Company, cid)
        assert c.industry == "clinic"
        assert decrypt(c.line_channel_secret_enc) == SECRET
        assert decrypt(c.line_channel_token_enc) == TOKEN


def test_switching_industry_rewrites_the_system_prompt():
    """這個專案的主張就是這條:換行業只換資料夾,程式碼一個字不動。"""
    cid = run_seed("restaurant", slug="bistro",
                   channel_secret=SECRET, channel_token=TOKEN)
    with session_scope() as db:
        before = db.get(Company, cid).system_prompt
    run_seed("clinic", slug="bistro")
    with session_scope() as db:
        after = db.get(Company, cid).system_prompt
    assert "微醺之夜" in before and "微醺之夜" not in after
    assert "晴日皮膚科" in after


def test_creating_a_new_company_still_requires_both_credentials():
    """省略只對「已存在的公司」成立。新公司沒有憑證,就只是一列永遠收不到
    訊息的死資料 —— 而且要在寫入之前就擋下,不是寫完才發現。"""
    with pytest.raises(SystemExit):
        run_seed("restaurant", slug="brand-new")
    with session_scope() as db:
        assert db.query(Company).count() == 0


def test_the_missing_credential_error_names_which_one_is_missing():
    with pytest.raises(SystemExit) as exc:
        run_seed("restaurant", slug="brand-new", channel_secret=SECRET)
    assert "--channel-token" in str(exc.value)


def test_no_warning_when_the_company_already_has_a_destination(capsys):
    """補完 destination 警告的另一半:已經設好時再 seed(例如只是換行業),
    不可以還印「交叉比對是關著的」—— 那是謊話,會讓人以為還要再跑一次。"""
    main(["seed", "--industry", "restaurant", "--slug", "bistro",
          "--channel-secret", SECRET, "--channel-token", TOKEN,
          "--destination", "Ubot0001"])
    capsys.readouterr()
    main(["seed", "--industry", "clinic", "--slug", "bistro"])
    assert "交叉比對是關著的" not in capsys.readouterr().out


def test_reset_history_clears_only_the_target_company():
    """換行業時舊對話會留著:人設是診所、最近十則卻在講餐廳停車位,
    模型會被帶偏。但清除必須只影響這一家 —— 多租戶系統裡誤刪別人的
    對話紀錄是不可逆的。"""
    a = run_seed("restaurant", slug="aa", channel_secret=SECRET, channel_token=TOKEN)
    b = run_seed("restaurant", slug="bb", channel_secret=SECRET, channel_token=TOKEN)
    _add_history(a, "Ua")
    _add_history(b, "Ub")

    run_seed("clinic", slug="aa", reset_history=True)

    with session_scope() as db:
        assert db.query(ChatHistory).filter(ChatHistory.company_id == a).count() == 0
        assert db.query(ChatHistory).filter(ChatHistory.company_id == b).count() == 2


def test_seed_keeps_history_by_default():
    """預設不刪。重跑 seed 只是改個 FAQ 的情況遠比換行業多,
    而刪掉的對話紀錄救不回來。"""
    cid = run_seed("restaurant", slug="aa", channel_secret=SECRET, channel_token=TOKEN)
    _add_history(cid, "Ua")
    run_seed("restaurant", slug="aa")
    with session_scope() as db:
        assert db.query(ChatHistory).filter(ChatHistory.company_id == cid).count() == 2


def test_list_reports_every_industry_folder():
    """行業是參數,不是寫死的邏輯 —— 所以這道指令也不能有一份硬編碼的清單,
    它必須真的去掃 industries/ 底下有什麼。加第四個行業時不用改這裡。"""
    from app.cli import run_list
    rows = run_list()
    by_key = {r["industry"]: r for r in rows}
    assert {"restaurant", "ecommerce", "clinic"} <= set(by_key)
    assert by_key["clinic"]["name"] == "晴日皮膚科診所"
    assert by_key["ecommerce"]["name"] == "好日子生活選物"


def test_list_counts_match_the_actual_files():
    from app.cli import run_list
    from app.cli import INDUSTRIES
    from app.knowledge.loader import load_industry
    rows = {r["industry"]: r for r in run_list()}
    data = load_industry(INDUSTRIES / "restaurant")
    assert rows["restaurant"]["faq"] == len(data["faq"])
    assert rows["restaurant"]["escalation"] == len(data["escalation"])


def test_list_surfaces_industries_that_have_errors():
    """資料壞掉的行業要在清單上就看得出來,不必等到 seed 才發現。"""
    from app.cli import run_list
    assert all(r.get("errors") == 0 for r in run_list()), \
        "現有三個行業應該都是零 ERROR"


def test_main_list_prints_the_folder_name_and_the_company_name(capsys):
    """這道指令存在的理由就是「哪個資料夾是哪一家」,兩者都要印出來。"""
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "restaurant" in out and "微醺之夜 Bistro" in out
    assert "clinic" in out and "晴日皮膚科診所" in out


def test_validate_and_list_run_without_any_credentials(tmp_path):
    """validate 與 list 只讀 industries/ 底下的 YAML,不碰資料庫、不呼叫 LLM。

    但 app/cli.py 在模組層 import 了 app.database,而 app/database.py 在
    import 時就 get_settings() —— 於是這兩道指令變成「要先產一把 Fernet
    金鑰、先辦一個 OpenRouter 帳號」才跑得起來。順序是反的:validate 是
    導入現場第一個會跑的東西,那時候還沒有任何憑證。

    用子行程 + 洗掉環境變數 + 換到沒有 .env 的工作目錄來重現 —— 本行程
    的 conftest 早就把那些變數設好了,在行程內測不出這件事。
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    env = {k: v for k, v in os.environ.items()
           if k not in ("FERNET_KEY", "OPENROUTER_API_KEY", "DATABASE_URL")}
    env["PYTHONIOENCODING"] = "utf-8"
    root = Path(__file__).resolve().parents[1]

    for args in (["validate", "--industry", "restaurant"], ["list"]):
        r = subprocess.run([sys.executable, "-m", "app.cli", *args],
                           cwd=tmp_path, env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        assert r.returncode == 0, f"{args} 失敗:\n{r.stderr[-800:]}"
