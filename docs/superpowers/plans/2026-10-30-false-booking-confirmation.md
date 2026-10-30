# 假確認安全網 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 模型回覆宣稱預約已成立、這一輪卻沒有成立任何預約時,不送那句,改送固定回覆請客人一次傳來資料。

**Architecture:** `app/booking.py` 加兩個純函式 —— `claims_booked(text)` 用正規表示式認「已經完成」的說法、
`unconfirmed_text(config, contact)` 產生固定回覆;`app/routers/webhook.py` 在模型有回文字、轉真人都沒發生、
簡繁轉換之後,檢查「有預約設定 + 這一輪沒成立 + 命中」就換掉回覆。順便把預設模型改成 ultra、報告補第 18 點。

**Tech Stack:** Python 3.11/3.13、FastAPI、SQLAlchemy、pytest(本機 `.venv`)

**Spec:** `docs/superpowers/specs/2026-10-30-false-booking-confirmation-design.md`(commit 445e3da)

## Global Constraints

- 分支 `fix/false-booking-confirmation`(從 master 9c41b9f 開)。repo 的 commit 信箱已設 noreply,不要改
- 跑測試一律:`.venv/Scripts/python -m pytest --ignore=tests/test_eval_jev.py -q`(在 `C:\Users\User\Desktop\universal-ai-helpdesk`)。
  `tests/test_eval_jev.py` 是使用者沒 commit 的檔案,本來就有一個失敗,不准改、不准刪、不准 commit
- 使用者沒 commit 的檔案:`scripts/eval_jev.py`、`scripts/eval_jev_cases.yaml`、`tests/test_eval_jev.py`、`.env.example` 裡的 Jev 那一行。
  `git add` 一律寫明檔名,**不准** `git add -A` / `git add .`
- 判斷規則照 spec 第三節,一字不改:
  `已(經)?(幫|為)(您|妳|你)?` 接 `訂|預約|掛號|保留|完成(訂位|預約|掛號)`;`(訂位|預約|掛號)(已)?(完成|成功)`;
  `已(經)?(訂好|訂妥|完成(訂位|預約|掛號))`;`訂妥`
- 程式寫的中文標點照 `confirmation_text`:逗號、冒號用半形 `,` `:`,句號用全形 `。`
- log 不准出現客人姓名、電話或模型回覆原文;只記命中的那段字
- 不改 prompt(`booking_prompt` 輸出的字一個都不能變)
- 測試假資料只用 林雅婷 / 0937-218-406 / 王小明 / 0912-345-678,不准用任何真實號碼
- commit 訊息結尾:`Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`

## 檔案

| 檔案 | 動作 | 負責 |
|---|---|---|
| `app/booking.py` | 改 | 新增 `CLAIM_RE`、`claims_booked`、`_needs`、`unconfirmed_text`;`booking_prompt` 改用 `_needs` |
| `tests/test_booking.py` | 改 | 規則正反例、固定回覆兩個行業、prompt 不變 |
| `app/routers/webhook.py` | 改 | 送出模型文字前的安全網 |
| `tests/test_webhook.py` | 改 | 替換、10/30 回歸、duplicate 放行、沒預約設定放行、簡體 |
| `app/config.py`、`tests/test_config.py`、`.env.example` | 改 | 預設模型改 ultra |
| `docs/report.md` | 改 | 第七節第 18 點、第八節第 19 點、第十節「十八個坑」 |

---

### Task 1: 判斷規則與固定回覆(`app/booking.py`)

**Files:**
- Modify: `app/booking.py`(`_asks_party` 之後加 `_needs`;`booking_prompt` 的 needs 那 5 行;`confirmation_text` 之後加 `CLAIM_RE`、`claims_booked`、`unconfirmed_text`)
- Test: `tests/test_booking.py`(檔尾新增一段)

**Interfaces:**
- Produces:
  - `booking.claims_booked(text: str) -> str | None` —— 命中回傳命中的那段字,沒命中回 `None`
  - `booking.unconfirmed_text(config: dict, contact: str | None) -> str`

- [ ] **Step 1: 寫失敗的測試**

在 `tests/test_booking.py` 檔尾加上(`RESTAURANT`、`CLINIC`、`CONTACT`、`date` 檔頭已經有):

```python


# --- 假確認安全網(spec 2026-10-30)-------------------------------------------

# 都出自真實回覆:2026-10-27 訂位編造量測(假資料)、線上 10/16 與 10/30(名字電話換成假資料)
CLAIMS = [
    "已幫您訂妥 10/30(週五) 20:00 四位,林雅婷 0937-218-406。",
    "已幫您訂好 2026-10-30(週五)20:00,4 位,林雅婷 0937-218-406。",
    "已幫妳訂好 10/30(週五)20:00 四位,名稱林雅婷、電話 0937-218-406。",
    "已幫您完成訂位:2026-10-30(週五)晚間 20:00,4 位,林先生,0937218406。",
    "已為您訂下週五(2026-10-23)晚上8點4位,王小明先生/小姐,電話0912345678。",
    "已為您訂好下週五(2026-11-06)晚上8點3位,林雅婷 先生/小姐,電話 0937-218-406。",
    "好的,2026-10-30(週五)20:00、4 位、林雅婷、0937-218-406,已幫您訂妥。",
    "好的,2026-10-30(週五)20:00、4位、林雅婷、0937-218-406。訂位完成,請準時前來。",
    "訂位完成:10/30(五)20:00 四位,林雅婷 0937-218-406。",
    "已為您預約 11/06(五)09:30,看診項目:青春痘。",
    "預約成功,請準時報到。",
    "已經訂好了,到時見。",
]
# 正常回覆:講的是「接下來」或「還沒」,不是已經完成
NOT_CLAIMS = [
    "好的,10/30(週五)20:00 四位還有位置。請提供您的聯絡電話,我幫您完成訂位。",
    "請提供您的聯絡電話以完成預約。",
    "可以訂喔,請問訂位人姓名?",
    "好的,2026-10-30(週五)20:00 還有位子。還差訂位人姓名,請補一下。",
    "電話號碼似乎少了一碼,請再確認一次完整的手機號碼。",
    "還沒為您訂好,請再提供電話。",
    "您這個時段已經有訂位了,要改人數請直接來電 02-2345-6789",
    "週一公休。",
]


@pytest.mark.parametrize("text", CLAIMS)
def test_a_reply_saying_the_booking_is_done_is_recognised(text):
    assert booking.claims_booked(text)


@pytest.mark.parametrize("text", NOT_CLAIMS)
def test_replies_about_what_happens_next_are_not_claims(text):
    assert booking.claims_booked(text) is None


def test_the_claim_check_returns_the_matched_words_for_the_log():
    assert booking.claims_booked("好的,已幫您訂妥。") == "已幫您訂"


def test_the_unconfirmed_reply_asks_for_everything_at_once_in_the_restaurants_words():
    assert booking.unconfirmed_text(RESTAURANT, CONTACT) == (
        "抱歉,這次的訂位還沒有建立成功。請把日期、時段、人數、姓名、電話一次傳給我,"
        "我馬上為您訂位。如果是想確認之前的訂位,請直接聯繫 02-2345-6789。")


def test_the_clinic_unconfirmed_reply_asks_for_the_service_not_the_party_size():
    assert booking.unconfirmed_text(CLINIC, "02-8765-4321") == (
        "抱歉,這次的預約還沒有建立成功。請把日期、時段、看診項目、姓名、電話一次傳給我,"
        "我馬上為您預約。如果是想確認之前的預約,請直接聯繫 02-8765-4321。")


def test_without_a_contact_the_unconfirmed_reply_drops_the_last_sentence():
    assert booking.unconfirmed_text(RESTAURANT, None) == (
        "抱歉,這次的訂位還沒有建立成功。請把日期、時段、人數、姓名、電話一次傳給我,"
        "我馬上為您訂位。")


def test_the_prompt_still_lists_what_a_booking_needs():
    """_needs 抽出來共用之後,prompt 那一行的字不能變。"""
    r = booking.booking_prompt(RESTAURANT, today=date(2026, 10, 6), contact=CONTACT)
    c = booking.booking_prompt(CLINIC, today=date(2026, 10, 6), contact=CONTACT)
    assert "- 建立訂位需要:日期、時段、人數、姓名、電話。缺什麼就一次問完。" in r
    assert "- 建立預約需要:日期、時段、看診項目、姓名、電話。缺什麼就一次問完。" in c
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `.venv/Scripts/python -m pytest tests/test_booking.py -q -k "recognised or claim or unconfirmed or still_lists"`
Expected: 新的 claim / unconfirmed 測試 FAIL(`AttributeError: module 'app.booking' has no attribute 'claims_booked'` / `unconfirmed_text`);
`test_the_prompt_still_lists_what_a_booking_needs` PASS(現有行為)

- [ ] **Step 3: 實作**

`app/booking.py`,在 `_asks_party` 之後(`def _fail` 之前)加:

```python
def _needs(config: dict) -> list[str]:
    """建立預約需要的資料。prompt 跟擋下假確認時的固定回覆用同一份,兩邊才不會講得不一樣。"""
    needs = ["日期", "時段"]
    if _asks_party(config):
        needs.append("人數")
    needs += [f["label"] for f in config.get("fields") or []]
    return needs + ["姓名", "電話"]
```

`booking_prompt` 裡把這 5 行:

```python
    needs = ["日期", "時段"]
    if _asks_party(config):
        needs.append("人數")
    needs += [f["label"] for f in config.get("fields") or []]
    needs += ["姓名", "電話"]
```

換成:

```python
    needs = _needs(config)
```

`confirmation_text` 之後(`def staff_message` 之前)加:

```python
# 宣稱預約「已經成立」的說法(spec 2026-10-30 第三節)。只抓已經完成的講法 ——
# 「我幫您完成訂位」「以便完成預約」講的是接下來,正常回覆天天都有,抓了會把請客人補資料的回覆換掉。
_YOU = "(?:您|妳|你)?"
_DONE = "(?:訂位|預約|掛號)"
CLAIM_RE = re.compile(
    rf"已(?:經)?(?:幫|為){_YOU}(?:訂|預約|掛號|保留|完成{_DONE})"
    rf"|{_DONE}(?:已)?(?:完成|成功)"
    rf"|已(?:經)?(?:訂好|訂妥|完成{_DONE})"
    rf"|訂妥")


def claims_booked(text: str) -> str | None:
    """回覆是否宣稱預約已經成立。命中回傳命中的那段字(給 log 用,不記原文),沒有回 None。"""
    m = CLAIM_RE.search(text)
    return m.group(0) if m else None


def unconfirmed_text(config: dict, contact: str | None) -> str:
    """擋下假確認時送給客人的固定回覆。

    請客人一次傳來:一句話講完的訂位實測 10/10 成立(2026-10-27 量測)。最後一句是因為
    模型偶爾會提到上一輪已經成立的預約 —— 這一輪沒成立就會被擋,兩種情況這句都是實話。
    """
    label = config["label"]
    text = (f"抱歉,這次的{label}還沒有建立成功。請把{'、'.join(_needs(config))}一次傳給我,"
            f"我馬上為您{label}。")
    if contact:
        text += f"如果是想確認之前的{label},請直接聯繫 {contact}。"
    return text
```

- [ ] **Step 4: 跑測試確認通過**

Run: `.venv/Scripts/python -m pytest tests/test_booking.py -q`
Expected: 全部 PASS(含原本的 prompt 測試 —— prompt 的字沒變)

- [ ] **Step 5: Commit**

```bash
git add app/booking.py tests/test_booking.py
git commit -m "feat(booking): 認得「已經訂好」的說法,並產生擋下假確認時的固定回覆

claims_booked 只抓已經完成的講法(已幫您訂妥、訂位完成…),不抓「我幫您完成訂位」
這種講接下來的。unconfirmed_text 請客人把資料一次傳來,要的欄位跟 prompt 共用 _needs。

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: webhook 接上安全網(`app/routers/webhook.py`)

**Files:**
- Modify: `app/routers/webhook.py`(`process_text_event` 裡 `model_text = ensure_traditional(agent.text)` 與名字還原之後、`answer = expired_prefix + model_text` 之前)
- Test: `tests/test_webhook.py`(檔尾新增一段)

**Interfaces:**
- Consumes: `booking.claims_booked(text) -> str | None`、`booking.unconfirmed_text(config, contact) -> str`(Task 1)
- 用到 webhook 裡已經有的區域變數:`booking_config`、`contact`、`booked`(`agent.bookings or ((agent.booking,) if agent.booking else ())`)、`line_user_id`、`expired_prefix`

- [ ] **Step 1: 寫失敗的測試**

在 `tests/test_webhook.py` 檔尾加上(`BOOKING`、`bookable`、`_llm_seq`、`_tool_calls`、`_args`、`_replies`、`line_out`、
`ToolCall`、`LLMResult`、`Booking`、`booking`、`select`、`date`、`User`、`Company`、`ChatHistory` 前面都已經 import 或定義):

```python


# --- 假確認安全網(spec 2026-10-30)-------------------------------------------

UNCONFIRMED = booking.unconfirmed_text(BOOKING, "02-2345-6789")


def test_a_claimed_booking_without_any_booking_this_turn_is_replaced(
        bookable, line_out, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.WARNING, logger="app.routers.webhook")
    claim = "已為您訂好下週五晚上8點3位,林雅婷 先生/小姐,電話 0937-218-406。祝您用餐愉快!"
    _llm_seq(monkeypatch, LLMResult(claim, token_count=1, latency_ms=1))
    with TestClient(app) as client:
        _post(client, _body(text="0937-218-406"))
    assert _replies(line_out) == [UNCONFIRMED]
    with session_scope() as db:
        assert db.scalars(select(Booking)).all() == []
        last = db.scalars(select(ChatHistory).order_by(ChatHistory.id.desc())).first()
        assert last.content == UNCONFIRMED      # 紀錄存的是客人實際看到的
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "假確認擋下" in logged and "已為您訂" in logged
    assert "0937" not in logged and "林雅婷" not in logged   # log 不放個資


def test_the_live_false_confirmation_from_2026_10_30_is_caught(bookable, line_out, monkeypatch):
    """10/30 線上 LINE 重現(假資料):第 2 則電話少碼被程式擋下,第 3 則客人只補電話,
    模型沒呼叫工具就說訂好了。第 3 則要收到固定回覆,而且一筆預約都沒有。"""
    day = bookable
    asked = "下週五晚上8點3位,還有空位。訂位還需要您的姓名與電話。"
    short = "電話號碼似乎少了一碼,請再確認一次完整的手機號碼。"
    _llm_seq(monkeypatch,
             LLMResult(asked, token_count=1, latency_ms=1),
             _tool_calls(ToolCall("create_booking",
                                  _args(day, party_size=3, name="林雅婷", phone="0937-2184"),
                                  id="c1")),
             LLMResult(short, token_count=1, latency_ms=1),
             LLMResult(f"已為您訂好{day} 晚上8點3位,林雅婷 先生/小姐,電話 0937-218-406。"
                       "祝您用餐愉快!", token_count=1, latency_ms=1))
    with TestClient(app) as client:
        _post(client, _body(text="下週五晚上8點3位", msg_id="M1"))
        _post(client, _body(text="林雅婷 0937-2184", msg_id="M2"))
        _post(client, _body(text="0937-218-406", msg_id="M3"))
    assert _replies(line_out) == [asked, short, UNCONFIRMED]
    with session_scope() as db:
        assert db.scalars(select(Booking)).all() == []


def test_a_duplicate_booking_this_turn_counts_as_booked(bookable, line_out, monkeypatch):
    """客人重送同一筆:工具回 duplicate,那筆預約是真的存在,模型的確認照送(spec 決策 5)。"""
    day = bookable
    with session_scope() as db:
        acme = db.scalar(select(Company).where(Company.slug == "acme"))
        user = User(company_id=acme.id, line_user_id="U1")
        db.add(user)
        db.flush()
        db.add(Booking(company_id=acme.id, user_id=user.id, booking_date=date.fromisoformat(day),
                       booking_time="20:00", party_size=4, name="王小明", phone="0912345678",
                       status="confirmed"))
    _llm_seq(monkeypatch,
             _tool_calls(ToolCall("create_booking", _args(day), id="c1")),
             LLMResult("已為您訂好", token_count=1, latency_ms=1))
    with TestClient(app) as client:
        _post(client, _body(text="週五晚上 8 點 4 位,王小明 0912-345-678"))
    assert _replies(line_out) == ["已為您訂好"]


def test_without_a_booking_config_the_same_words_go_out_as_is(line_out, monkeypatch):
    """電商沒有預約:同樣的字不擋(spec 第一節)。"""
    _llm_seq(monkeypatch, LLMResult("已為您訂好", token_count=1, latency_ms=1))
    with TestClient(app) as client:
        _post(client, _body(text="訂位"))
    assert _replies(line_out) == ["已為您訂好"]


def test_a_claim_in_simplified_chinese_is_caught_after_conversion(bookable, line_out, monkeypatch):
    """判斷放在簡繁轉換之後(spec 決策 6):模型偶爾輸出簡體。"""
    _llm_seq(monkeypatch, LLMResult("已为您订好,4 位。", token_count=1, latency_ms=1))
    with TestClient(app) as client:
        _post(client, _body(text="0937-218-406"))
    assert _replies(line_out) == [UNCONFIRMED]


def test_a_promised_transfer_still_wins_over_the_false_confirmation_check(
        bookable, rules, line_out, monkeypatch):
    """轉真人排在前面(spec 第五節):回覆同時說訂好了、又說要轉接 —— 客人被轉,收到店家的通用句。"""
    _llm_seq(monkeypatch, LLMResult("已為您訂好,另外我將為您轉接店長。", token_count=1, latency_ms=1))
    with TestClient(app) as client:
        _post(client, _body(text="訂位"))
    assert _replies(line_out) == [GENERIC_SCRIPT]
    assert _mode()[0] == ConversationMode.HUMAN
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `.venv/Scripts/python -m pytest tests/test_webhook.py -q -k "claimed or live_false or duplicate_booking_this_turn or same_words or simplified_chinese_is_caught or still_wins"`
Expected: `..._is_replaced`、`..._is_caught`(兩條)FAIL(客人收到的是模型原文);
`test_a_duplicate_booking_this_turn_counts_as_booked`、`test_without_a_booking_config_the_same_words_go_out_as_is`、
`test_a_promised_transfer_still_wins_over_the_false_confirmation_check` PASS(現有行為,之後也要維持)。
`GENERIC_SCRIPT`、`_mode`、`ConversationMode`、`rules` 也是檔案前面就有的

- [ ] **Step 3: 實作**

`app/routers/webhook.py`,在這段之後:

```python
            model_text = ensure_traditional(agent.text)
            if agent.booking:
                # OpenCC 會改到常見姓氏(余→餘、于→於):訂位人的名字換回客人給的寫法
                name = agent.booking["name"]
                converted = ensure_traditional(name)
                if converted != name:
                    model_text = model_text.replace(converted, name)
```

緊接著(`# 只有模型真的回了答案才加` 那行之前)加:

```python
            # 假確認安全網(spec 2026-10-30):模型說訂好了,這一輪卻沒有成立任何預約 ——
            # 2026-10-27 量測 S6、10/30 線上都出現過,客人會以為訂到了。不送那句,改送固定回覆
            # 請客人一次傳來(一句話講完的訂位實測 10/10 成立)。放在簡繁轉換之後,簡體的也抓得到。
            # log 只記命中的字:原文有客人姓名電話。
            claimed = (booking.claims_booked(model_text)
                       if booking_config and not booked else None)
            if claimed:
                logger.warning("假確認擋下 客人 …%s 命中「%s」", line_user_id[-6:], claimed)
                model_text = booking.unconfirmed_text(booking_config, contact)
```

(`answer = expired_prefix + model_text`、`tokens, latency = agent.token_count, agent.latency_ms` 照舊 —— 額度確實花了)

- [ ] **Step 4: 跑測試確認通過**

Run: `.venv/Scripts/python -m pytest tests/test_webhook.py -q`
Expected: 全部 PASS(含原本的預約測試:`test_a_booking_conversation_saves_the_booking_and_sends_the_models_answer` 這類有成立預約的照送原文)

- [ ] **Step 5: 跑全部測試**

Run: `.venv/Scripts/python -m pytest --ignore=tests/test_eval_jev.py -q`
Expected: 全部 PASS(數量 = master 的 546 + 這次新增的)

- [ ] **Step 6: Commit**

```bash
git add app/routers/webhook.py tests/test_webhook.py
git commit -m "fix(webhook): 模型說訂好了、這一輪卻沒成立預約時,改送固定回覆

2026-10-27 量測 S6 與 10/30 線上 LINE 都出現過:多句對話的最後一步,模型沒呼叫
create_booking 就回「已為您訂好」。有預約設定、這一輪沒成立(含 duplicate 算成立)、
回覆命中 claims_booked 時,改送 unconfirmed_text。判斷在簡繁轉換之後;log 只記命中的字。

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: 預設模型改成 ultra

**Files:**
- Modify: `app/config.py:29-34`(註解與 `nvidia_model` 預設)
- Modify: `.env.example`(NVIDIA 段的註解與 `NVIDIA_MODEL=`)—— **只 stage 這兩行**
- Test: `tests/test_config.py`(新增一條)

- [ ] **Step 1: 寫失敗的測試**

`tests/test_config.py`,接在 `test_default_models_both_support_tool_calling` 後面:

```python


def test_the_default_nvidia_model_is_ultra(monkeypatch):
    """2026-10-27 起 super 在 NVIDIA 回 404「Function not found for account」(/v1/models 還列著它)。
    預設停在 super 的話,主機 .env 少一行、或在別台重架,bot 就直接壞。"""
    monkeypatch.delenv("NVIDIA_MODEL", raising=False)
    s = Settings(_env_file=None, fernet_key="k", openrouter_api_key="k")
    assert s.nvidia_model == "nvidia/nemotron-3-ultra-550b-a55b"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `.venv/Scripts/python -m pytest tests/test_config.py -q -k ultra`
Expected: FAIL(`'nvidia/nemotron-3-super-120b-a12b' == 'nvidia/nemotron-3-ultra-550b-a55b'`)

- [ ] **Step 3: 改 `app/config.py`**

把:

```python
    # 2026-10-05 實測 10 題(當時思考是開的),super 轉真人 5/5、誤轉 0、中位數 1.5 秒 —— 同一顆
    # 模型在 OpenRouter 的 :free 版 9/24 四次全限流。免費層是每分鐘約 40 次,
    # 沒有每日上限。模型名不帶 :free:那是 OpenRouter 的命名,NVIDIA 不認得。
    nvidia_api_key: str = ""
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_model: str = "nvidia/nemotron-3-super-120b-a12b"
```

換成:

```python
    # 2026-10-05 實測 10 題(當時思考是開的):super 轉真人 5/5、中位數 1.5 秒;ultra 5/5、3.4 秒。
    # 2026-10-27 起 super 在 NVIDIA 回 404(/v1/models 還列著它),預設改成 ultra。
    # 免費層是每分鐘約 40 次(10/16 下午實測二十幾次就 429),沒有每日上限。
    # 模型名不帶 :free:那是 OpenRouter 的命名,NVIDIA 不認得。
    nvidia_api_key: str = ""
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_model: str = "nvidia/nemotron-3-ultra-550b-a55b"
```

- [ ] **Step 4: 改 `.env.example`,只 stage 模型那兩行**

工作區的 `.env.example` 有使用者沒 commit 的 Jev 那一行,不能整份 `git add`。做法:從 HEAD 版本改出要 commit 的內容寫進 index,
工作區那份也套用同樣的修改(Jev 那行留著)。在 repo 根目錄、Git Bash 執行:

```bash
.venv/Scripts/python - <<'PY'
import subprocess
OLD_NOTE = "# 留空就只走 OpenRouter。2026-10-05 實測 10 題:super 轉真人 5/5、中位數 1.5 秒。"
NEW_NOTE = ("# 留空就只走 OpenRouter。2026-10-27 起 super 在 NVIDIA 回 404,改用 ultra"
            "(2026-10-05 實測 10 題:ultra 轉真人 5/5、中位數 3.4 秒)。")
OLD_MODEL = "NVIDIA_MODEL=nvidia/nemotron-3-super-120b-a12b"
NEW_MODEL = "NVIDIA_MODEL=nvidia/nemotron-3-ultra-550b-a55b"

def fix(b: bytes) -> bytes:
    s = b.decode("utf-8")
    assert s.count(OLD_NOTE) == 1 and s.count(OLD_MODEL) == 1, "找不到要改的那兩行"
    return s.replace(OLD_NOTE, NEW_NOTE).replace(OLD_MODEL, NEW_MODEL).encode("utf-8")

head = subprocess.run(["git", "show", "HEAD:.env.example"], capture_output=True, check=True).stdout
blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], input=fix(head),
                      capture_output=True, check=True).stdout.decode().strip()
subprocess.run(["git", "update-index", "--cacheinfo", f"100644,{blob},.env.example"], check=True)
with open(".env.example", "rb") as f:
    work = f.read()
with open(".env.example", "wb") as f:
    f.write(fix(work))
print("staged", blob)
PY
```

確認:

```bash
git diff --cached .env.example     # 只有註解與 NVIDIA_MODEL 兩行
git diff .env.example              # 只剩使用者的 Jev 那一行
```

Expected:第一個 diff 只有兩行 `-`/`+`;第二個 diff 只有 Jev 那一行。不是的話停下來,`git restore --staged .env.example` 後回報。

- [ ] **Step 5: 跑測試確認通過**

Run: `.venv/Scripts/python -m pytest --ignore=tests/test_eval_jev.py -q`
Expected: 全部 PASS

- [ ] **Step 6: Commit**

```bash
git add app/config.py tests/test_config.py
git commit -m "fix(config): 預設模型從已失效的 super 改成 ultra

2026-10-27 起 nemotron-3-super-120b-a12b 在 NVIDIA 回 404,線上靠主機 .env 多一行才改成
ultra;預設與 .env.example 跟著改,主機 .env 少那一行或在別台重架時才不會直接壞。
OpenRouter 那邊的 :free 備援沒驗證過,不動。

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

(`.env.example` 已經在 Step 4 stage 好,這個 commit 會一起帶進去;**不要**再 `git add .env.example`)

確認:`git show --stat HEAD` 列出 `.env.example`、`app/config.py`、`tests/test_config.py` 三個檔案;`git status -s` 裡 `.env.example` 還是 ` M`(Jev 那行)。

---

### Task 4: 用正式程式重播、補報告

**Files:**
- Modify: `docs/report.md`(第七節加第 18 點、第八節加第 19 點、第十節)
- 一次性腳本放 scratchpad,不進 repo

- [ ] **Step 1: 重播 10/27 量測紀錄**

Run(repo 根目錄;腳本用 heredoc 直接跑,不存檔):

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python - .superpowers/sdd/2026-10-22-booking-evidence/runs/20261027T095011/records.jsonl <<'PY'
import json, sys
sys.path.insert(0, ".")
from app import booking
from app.agent.zh import ensure_traditional
st = {}
for line in open(sys.argv[1], encoding="utf-8"):
    if not line.strip():
        continue
    r = json.loads(line)
    for i, t in enumerate(r["turns"]):
        calls = t.get("calls") or []
        booked = any(c.get("tool") == "create_booking" and (c.get("result") or {}).get("ok")
                     for c in calls)
        hit = booking.claims_booked(ensure_traditional(t.get("reply") or ""))
        st[(booked, bool(hit))] = st.get((booked, bool(hit)), 0) + 1
        if hit and not booked:
            print("擋下", r["case"], "run", r["run"], "turn", i, "命中", hit)
print({f"booked={a} claim={b}": n for (a, b), n in sorted(st.items())})
PY
```

Expected(跟 spec 第三節一致):
```
擋下 S6 run 4 turn 3 命中 已幫您訂
擋下 S6 run 5 turn 3 命中 訂位完成
{'booked=False claim=False': 40, 'booked=False claim=True': 2, 'booked=True claim=True': 13}
```
不一致就停下來回報,不要改規則去湊數字。

- [ ] **Step 2: 重播線上紀錄(只看數字,不把內容存進 repo)**

這一步要 SSH 到 GCP 主機(唯讀查詢)。由主控者執行;回應含客人姓名電話,**只印計數與時間**。
先照專案記憶 universal-ai-helpdesk-project 設好 `HELPDESK_SSH`(帳號@主機,不寫進 repo):

```bash
cat <<'SQL' | ssh -o BatchMode=yes -i ~/.ssh/helpdesk_gcp "$HELPDESK_SSH" 'cd ~/universal-ai-helpdesk && docker compose exec -T db sh -c "psql -U \$POSTGRES_USER -d \$POSTGRES_DB -At"' \
  | PYTHONIOENCODING=utf-8 .venv/Scripts/python -c "
import json, sys
sys.path.insert(0, '.')
from app import booking
st = {}
for line in sys.stdin:
    if not line.strip():
        continue
    r = json.loads(line)
    hit = bool(booking.claims_booked(r['content']))
    st[(r['booked'], hit)] = st.get((r['booked'], hit), 0) + 1
    if hit:
        print(r['at'][:19], 'booked' if r['booked'] else '擋下')
print({f'booked={a} claim={b}': n for (a, b), n in sorted(st.items())})
"
select json_build_object('at', h.created_at, 'content', h.content,
  'booked', exists(select 1 from bookings b where b.user_id = h.user_id
                   and b.created_at between h.created_at - interval '60 seconds'
                                        and h.created_at + interval '5 seconds'))
from chat_histories h where lower(h.role::text) = 'assistant' order by h.created_at;
SQL
```

(主機位址不寫進 repo;這份計畫會進 git)

Expected:
```
2026-10-16 02:16:21 booked
2026-10-30 06:39:57 擋下
{'booked=False claim=False': 13, 'booked=False claim=True': 1, 'booked=True claim=True': 1}
```
(10/30 之後如果有新的對話,總數會變;要看的是「擋下的只有 10/30 那一則、10/16 那則是 booked」)

- [ ] **Step 3: 報告第七節加第 18 點**

`docs/report.md`,在第 17 點最後一段(「改成每一筆開始前才節流…數字才可信。」)之後、`## 八、限制` 之前加:

```markdown

### 18. 模型說「已幫您訂好」,其實沒訂

2026-10-27 量「模型會不會編造客人的姓名電話」(7 種情境各 5 次,模型 ultra、關思考):編造 0 次,卻量到更嚴重的事 ——
資料分 4 句講的那一組,完成的 4 段裡有 2 段最後回「已幫您訂妥」「訂位完成」,那一輪根本沒呼叫建立預約,資料庫也沒有這筆。
一句話講完的 10 次全部正確成立。10/30 在線上用手機重現:電話少碼被程式擋下之後,客人只補電話,
模型沒呼叫工具就回「已為您訂好…祝您用餐愉快」。客人會以為訂到了,到店才發現沒有位子。

原因是 webhook 對模型寫的文字照單全收:不管這一輪有沒有成立預約,都原文送出。工具過程不進對話紀錄(第八節第 7 點),
模型下一輪看不到上一輪的工具結果,多句對話的最後一步就容易「以為已經訂了」。

解法是**再一道程式判斷的安全網**(`booking.claims_booked`,設計見
[spec](superpowers/specs/2026-10-30-false-booking-confirmation-design.md)):回覆宣稱預約已經成立、這一輪卻沒有成立任何預約,
就不送那句,改送固定回覆請客人把資料一次傳來 —— 一句話講完的訂位實測 10/10 成立。
規則只抓「已幫您…」「訂位完成」這種已經完成的說法,不抓「我幫您完成訂位」「以便完成預約」(講的是接下來,正常回覆天天都有)。
用正式程式重播 10/27 的 55 則回覆與線上 15 則回覆:擋下的正好是那 3 次假確認,真的成立的 14 則照送,
53 則沒成立預約的正常回覆 0 誤擋。這道安全網是程式判斷、不改模型行為,所以不用重做真模型量測。

這跟轉真人的兩道安全網是同一個教訓:**模型說它做了什麼,要對照工具紀錄,不能只看它怎麼說**。
「沒查空位就說還有空位」也出現過,但建立時程式會再查一次名額、不會超訂,這次沒擋(第八節第 19 點)。
根本的解法是讓模型不能自己宣布成立 —— 聽懂之後送一張「確認訂位」的按鈕,客人按了才由程式建立(第九節)。
```

- [ ] **Step 4: 報告第八節加第 19 點**

`docs/report.md` 第八節,在第 18 點(「**轉真人分類層的證據是兩個時段、幾十題的量測,不是統計保證。**」那一點)的最後一行之後加:

```markdown
19. **模型會說「還有空位」卻沒查。** 2026-10-30 線上出現過(那一輪沒呼叫查空位的工具)。建立預約時程式會再查一次名額,
    不會超訂;最壞是客人留完資料才被告知額滿。假確認那道安全網(第七節第 18 點)只擋「說訂好了」,這個沒擋。
```

- [ ] **Step 5: 第十節改數字**

`docs/report.md` 第十節:`第七節那十七個坑,每一個都有對應的修正 ——` 改成 `第七節那十八個坑,每一個都有對應的修正 ——`

- [ ] **Step 6: Commit**

```bash
git add docs/report.md
git commit -m "docs(report): 第七節加第 18 點假確認、第八節加「沒查就說還有空位」

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: 推上去、開 PR(主控者做)

- [ ] **Step 1: 全部測試**

Run: `.venv/Scripts/python -m pytest --ignore=tests/test_eval_jev.py -q`
Expected: 全部 PASS

- [ ] **Step 2: 確認沒帶到使用者的檔案**

Run: `git diff --stat origin/master..HEAD` 與 `git status -s`
Expected:commit 範圍只有 spec、plan、`app/booking.py`、`app/routers/webhook.py`、`app/config.py`、`.env.example`、
`docs/report.md`、三個測試檔;`git status -s` 仍是 ` M .env.example`(Jev 那行)與三個 `??` Jev 檔

- [ ] **Step 3: push 與開 PR**

```bash
git push -u origin fix/false-booking-confirmation
gh pr create --repo inform199751-crypto/universal-ai-helpdesk --base master --head fix/false-booking-confirmation \
  --title "fix: 模型說訂好了、這一輪卻沒成立預約時,改送固定回覆" --body-file "$SCRATCH/pr-body.md"
```

`$SCRATCH` 是主控者 session 的 scratchpad 目錄(不在 repo 裡)。`pr-body.md` 要有:問題(10/27 量測 S6、10/30 線上重現)、做法、重播數字(Task 4 Step 1、2 的實際輸出)、預設模型改 ultra、
不在範圍(還有空位、prompt、確認按鈕)、部署方式(pull → build → up,不用遷移、不用 seed);結尾
`🤖 Generated with [Claude Code](https://claude.com/claude-code)`。

- [ ] **Step 4: CI**

綁定 PR、看 CI 三個 job;合併由使用者自己按。

---

### Task 6: 部署與手機驗收(使用者合併、說要部署之後)

- [ ] **Step 1: 部署**

主機:`git pull --ff-only` → `docker compose build app` → `docker compose up -d app` → 等 healthy;
容器內確認 `booking.claims_booked` 存在、`get_settings().nvidia_model` 仍是 ultra(主機 `.env` 本來就設了)。

- [ ] **Step 2: 手機驗收(請使用者傳,用假資料)**

1. 「下週五晚上8點3位」
2. 「林雅婷 0937-2184」(故意少碼)
3. 「0937-218-406」→ 若模型又假確認,要收到固定回覆;log 有「假確認擋下」
4. 一句話傳完整資料:「下週五晚上8點3位,林雅婷 0937-218-406」→ 要真的成立一筆

驗完把那筆測試預約標成 `cancelled`(SQL `update bookings set status='cancelled' where ...`,只改這一筆)。
模型行為會變,第 3 則不一定會出現假確認;沒出現就照實記「這次模型有呼叫工具」,安全網由第 2 個 Task 的回歸測試保證。
