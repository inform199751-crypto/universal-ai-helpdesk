# 預約 agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 LINE 客服變成真正的 agent:模型自己決定「查空位 → 追問 → 建立預約」,預約設定由各行業選填的 `booking.yaml` 驅動。

**Architecture:** `booking.yaml` 經 seed 存進 `companies.booking_config`;`app/booking.py` 依設定產生兩個工具、做所有檢查與名額計算;`app/agent/loop.py` 是自己寫的 tool-calling 迴圈(最多 4 步、最後一步不帶工具、整個 loop 45 秒);webhook 把原本「呼叫一次模型」換成跑一次 loop,並在「預約成立但送出的不是模型回答」時由程式補確認句。

**Tech Stack:** FastAPI、SQLAlchemy 2、Alembic、httpx、pytest(SQLite 記憶體庫;CI 另跑 PostgreSQL)

**Spec:** `docs/superpowers/specs/2026-10-06-booking-agent-design.md`

## Global Constraints

- 專案根目錄 `C:\Users\User\Desktop\universal-ai-helpdesk`(Windows、Git Bash),分支 `feat/booking-agent`。
- 測試指令:`PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider --ignore=tests/test_eval_jev.py`
  (單檔把 `--ignore=…` 換成檔案路徑)。基準:333 passed、1 skipped、2 warnings(既有的 starlette 棄用警告)。
- 程式要同時在 SQLite 與 PostgreSQL 上正確;CI 在 Python 3.11 與 3.13 各跑一輪。
- 工具名稱:`check_availability`、`create_booking`;既有的轉真人工具 `transfer_to_human` 不變。
- 台灣時間一律 `timezone(timedelta(hours=8))`(台灣沒有日光節約,不用 zoneinfo);星期字串 `"一二三四五六日"`,`date.weekday()` 當索引。
- 設定:`agent_max_steps = 4`、`agent_total_budget_seconds = 45.0`;「正在輸入」動畫秒數 = 45。
- Alembic revision:`c5e8a1f3d920`,`down_revision = "7b1e4c2a9d30"`。
- 確認句格式:`已為您{label} MM/DD(週){HH:MM},{n} 位,{label}人{姓名}。`(`max_party` 為 1 時沒有人數;有 `fields` 時在姓名前插入 `{欄位名}:{值}`)。
- 店員推播格式:`【新{label}】MM/DD(週){HH:MM} {n} 位 {姓名} {電話}`(`max_party` 為 1 時沒有人數;有 `fields` 時在姓名前插入值)。
- log 不記電話;客人只記 LINE id 後 6 碼。
- 程式註解用繁體中文、寫「為什麼」,半形標點,跟專案既有風格一致。
- **不要碰**:`.env.example`、`scripts/eval_jev.py`、`scripts/eval_jev_cases.yaml`、`tests/test_eval_jev.py`。只 `git add` 明確路徑,不要 `git add -A` / `git add .`。
- 工作區檔案是 CRLF(autocrlf=true),編輯時讓 diff 只出現想改的行。
- commit 訊息中文、`type(scope): 摘要`,最後一行是 `Co-Authored-By:` trailer,寫實際執行該 commit 的模型。
- 不 push、不 merge;PR 由 controller 開,**合併由使用者按**。

## 檔案結構

| 檔案 | 責任 |
|---|---|
| `industries/restaurant/booking.yaml`、`industries/clinic/booking.yaml`(新) | 時段、名額、要問的欄位 |
| `app/knowledge/loader.py` | 選填檔案 `booking` |
| `app/knowledge/validate.py` | 規則 11(格式,ERROR)、規則 12(時段在營業時間內,BLOCK) |
| `app/models/booking.py`(新)、`app/models/company.py`、`app/models/__init__.py` | `bookings` 表、`companies.booking_config` |
| `alembic/versions/c5e8a1f3d920_bookings.py`(新) | 上面兩者的遷移 |
| `app/cli.py` | seed 寫 `booking_config`;新指令 `bookings` |
| `app/booking.py`(新) | 工具定義、預約說明、查空位、建立預約、確認句、店員通知文字 |
| `app/agent/llm.py` | 回傳所有 tool_calls(含 id);`complete()` 接受 `deadline` |
| `app/agent/loop.py`(新) | agent loop |
| `app/routers/webhook.py`、`app/config.py` | 接上 loop、確認句、推播店員、動畫秒數 |
| 文件 | README、report、`industries/README.md`、demo-checklist |

---

### Task 1: `booking.yaml`、載入與驗證規則 11、12

**Files:**
- Create: `industries/restaurant/booking.yaml`、`industries/clinic/booking.yaml`
- Modify: `app/knowledge/loader.py`
- Modify: `app/knowledge/validate.py`
- Test: `tests/test_validate.py`、`tests/test_industries.py`

**Interfaces:**
- Produces: `load_industry(path)` 回傳的 dict 在有 `booking.yaml` 時多一個 `"booking"` 鍵(沒有就不存在,一律用 `data.get("booking")` 讀);`validate()` 會回報規則 11、12。Task 2 的 seed 讀 `data.get("booking")`。

- [ ] **Step 1: 寫失敗的測試**

`tests/test_validate.py` 檔尾加:

```python
# --- 規則 11、12:booking.yaml ------------------------------------------------

BOOKING = {"label": "訂位", "days": ["二", "三", "四", "五", "六", "日"],
           "times": ["18:00", "19:00", "20:00"], "capacity": 5, "max_party": 6,
           "advance_days": 14, "fields": []}


def _with_booking(hours="17:00-24:00(週一公休)", **over):
    data = _ok_data()
    data["company"] = {**data["company"], "hours": hours}
    data["booking"] = {**BOOKING, **over}
    return data


def test_a_valid_booking_has_no_findings():
    assert validate(_with_booking()) == []


@pytest.mark.parametrize("over, fragment", [
    ({"label": ""}, "label"),
    ({"days": ["八"]}, "days"),
    ({"days": ["二", "二"]}, "days 有重複"),
    ({"days": []}, "days"),
    ({"times": ["18:00", "18:00"]}, "times 有重複"),
    # 沒加引號的 18:00 會被 PyYAML 當成 60 進位整數 1080 —— 寫錯的人看不出來
    ({"times": [1080]}, "HH:MM"),
    ({"times": ["8pm"]}, "HH:MM"),
    ({"capacity": -1}, "capacity"),
    ({"max_party": 0}, "max_party"),
    ({"advance_days": 61}, "advance_days"),
    ({"advance_days": True}, "advance_days"),
    ({"fields": [{"key": "service", "label": "看診項目"}]}, "fields[0]"),
    ({"fields": [{"key": "phone", "label": "電話", "options": ["x"]}]}, "保留字"),
])
def test_rule11_malformed_booking_is_error(over, fragment):
    findings = [f for f in validate(_with_booking(**over)) if f.rule == 11]
    assert findings and all(f.level == "ERROR" for f in findings)
    assert any(fragment in f.message for f in findings)


def test_rule11_booking_that_is_not_a_mapping_is_error():
    data = _ok_data()
    data["booking"] = ["18:00"]
    assert _levels(validate(data), 11) == ["ERROR"]


def test_rule12_a_slot_outside_opening_hours_is_block():
    findings = validate(_with_booking(times=["15:00", "18:00"]))
    assert [(f.level, f.rule) for f in findings] == [("BLOCK", 12)]
    assert "15:00" in findings[0].message


def test_rule12_slots_may_fall_in_any_of_several_ranges():
    data = _with_booking(hours="09:00-12:00、14:00-21:00(週日休診)",
                         times=["09:30", "14:30"])
    assert validate(data) == []


def test_rule12_a_range_past_midnight_is_understood():
    assert validate(_with_booking(hours="18:00-02:00", times=["01:00", "19:00"])) == []


def test_rule12_is_skipped_for_round_the_clock_shops():
    assert validate(_with_booking(hours="24小時營業", times=["03:00"])) == []
```

檔頭補 `import pytest`(若還沒有)。

`tests/test_industries.py` 檔尾加:

```python
# --- booking.yaml(選填) ---------------------------------------------------

def test_restaurant_and_clinic_take_bookings_but_ecommerce_does_not():
    assert load_industry(INDUSTRIES / "restaurant")["booking"]["label"] == "訂位"
    assert load_industry(INDUSTRIES / "clinic")["booking"]["label"] == "預約"
    assert "booking" not in load_industry(INDUSTRIES / "ecommerce")


@pytest.mark.parametrize("industry", ALL)
def test_booking_slots_fall_inside_opening_hours(industry):
    assert [f for f in run_validate(industry) if f.rule in (11, 12)] == []
```

注意:既有的 `test_industry_has_all_five_files` 斷言 `set(data)` 恰好是五個鍵 —— 這個測試要保持原樣、繼續通過,
所以 loader **只在檔案存在時**才放 `"booking"` 鍵,而且那個測試要改成只看必填的五份:

```python
@pytest.mark.parametrize("industry", ALL)
def test_industry_has_all_five_files(industry):
    data = load_industry(INDUSTRIES / industry)
    assert {"company", "faq", "policies", "escalation", "glossary"} <= set(data)
    assert set(data) - {"company", "faq", "policies", "escalation", "glossary"} <= {"booking"}
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider tests/test_validate.py tests/test_industries.py`
Expected: 規則 11 的 parametrize 全部 failed(`findings` 是空的)、規則 12 的 BLOCK 測試 failed、
`test_restaurant_and_clinic_take_bookings_but_ecommerce_does_not` failed(`KeyError: 'booking'`)。
`test_a_valid_booking_has_no_findings` 與其他三個規則 12 的「沒有 finding」測試會 passed —— 它們守的是「不要誤報」,
Step 5 的變異檢查會證明它們有用。

- [ ] **Step 3: 新增兩份 `booking.yaml`**

`industries/restaurant/booking.yaml`:

```yaml
# 預約設定(選填)。有這份檔案,bot 才會幫客人訂位;沒有就只會請客人來電。
# 名額以「組」計:一筆訂位佔一個名額,不管幾位。
# 時段一定要加引號 —— 沒加的 18:00 會被 YAML 當成 60 進位整數 1080(規則 11 會擋)。
label: 訂位
days: [二, 三, 四, 五, 六, 日]
times: ["18:00", "19:00", "20:00"]
capacity: 5
max_party: 6
advance_days: 14
fields: []
```

`industries/clinic/booking.yaml`:

```yaml
# 預約設定(選填)。一次一位病人,所以 max_party 是 1(不問人數)。
# 時段一定要加引號 —— 沒加的 09:30 會被 YAML 當成 60 進位整數(規則 11 會擋)。
label: 預約
days: [一, 二, 三, 四, 五, 六]
times: ["09:30", "10:30", "14:30", "15:30"]
capacity: 3
max_party: 1
advance_days: 14
fields:
  - key: service
    label: 看診項目
    options: [一般皮膚, 青春痘, 醫美諮詢]
```

- [ ] **Step 4: 實作 loader 與 validate**

`app/knowledge/loader.py`:docstring 改成「把一個行業資料夾的 YAML 讀成一個 dict。」,在 `FILES = (...)` 之後加:

```python
# 選填:沒有這份檔案就不提供那項功能,dict 裡也不會有這個鍵(一律用 .get 讀)。
# booking —— 預約(電商沒有)。
OPTIONAL_FILES = ("booking",)
```

並在 `if missing:` 之前加:

```python
    for name in OPTIONAL_FILES:
        f = path / f"{name}.yaml"
        if f.exists():
            data[name] = yaml.safe_load(f.read_text(encoding="utf-8-sig"))
```

`app/knowledge/validate.py`:在 `HOURS_RE = ...` 之後加常數與函式:

```python
WEEKDAYS = "一二三四五六日"
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
HOURS_RANGE_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[-~]\s*(\d{1,2}):(\d{2})")
# 這幾個 key 工具本身就有,fields 再用一次的話,工具定義的參數會互相蓋掉
RESERVED_FIELD_KEYS = ("name", "phone", "party_size", "date", "time")


def _minutes(hh: str, mm: str) -> int:
    return int(hh) * 60 + int(mm)


def _is_int(v: Any) -> bool:
    # bool 是 int 的子類別:advance_days: true 不能被當成 1
    return isinstance(v, int) and not isinstance(v, bool)


def _validate_booking(booking: Any, hours: str) -> list[Finding]:
    out: list[Finding] = []

    def error(message: str) -> None:
        out.append(Finding("ERROR", 11, f"booking.yaml {message}"))

    if not isinstance(booking, dict):
        error("格式不對,應該是一組設定(label、days、times …)")
        return out
    if not str(booking.get("label") or "").strip():
        error("缺少 label(例如「訂位」「預約」)")

    days = booking.get("days")
    if not isinstance(days, list) or not days:
        error("days 要列出至少一天")
    else:
        bad = [d for d in days if str(d) not in WEEKDAYS or len(str(d)) != 1]
        if bad:
            error(f"days 只能是「一」到「日」:{bad}")
        if len({str(d) for d in days}) != len(days):
            error("days 有重複")

    times = booking.get("times")
    if not isinstance(times, list) or not times:
        error("times 要列出至少一個時段")
        times = []
    else:
        bad = [t for t in times if not TIME_RE.match(str(t))]
        if bad:
            error(f"times 要是加了引號的 HH:MM:{bad}")
        if len({str(t) for t in times}) != len(times):
            error("times 有重複")

    for key, low, high in (("capacity", 0, None), ("max_party", 1, None),
                           ("advance_days", 1, 60)):
        value = booking.get(key)
        if not _is_int(value) or value < low or (high is not None and value > high):
            span = f"{low} 以上" if high is None else f"{low} 到 {high}"
            error(f"{key} 要是 {span} 的整數")

    fields = booking.get("fields") or []
    if not isinstance(fields, list):
        error("fields 要是清單")
        fields = []
    for i, field in enumerate(fields):
        if (not isinstance(field, dict) or not field.get("key")
                or not field.get("label") or not field.get("options")):
            error(f"fields[{i}] 要有 key、label 與非空的 options")
        elif field["key"] in RESERVED_FIELD_KEYS:
            error(f"fields[{i}] 的 key「{field['key']}」是保留字")

    # 規則 12:時段要落在營業時間內。解析不出區間(例如「24 小時營業」)就不檢查 ——
    # 營業時間本身能不能解析是規則 8 的事。跨過午夜的區間(18:00-02:00)也要認得。
    ranges = [(_minutes(a, b), _minutes(c, d))
              for a, b, c, d in HOURS_RANGE_RE.findall(hours)]
    if ranges:
        for t in times:
            if not TIME_RE.match(str(t)):
                continue
            m = _minutes(*str(t).split(":"))
            if not any((start <= m < end) if start < end else (m >= start or m < end)
                       for start, end in ranges):
                out.append(Finding("BLOCK", 12,
                                   f"booking.yaml 的時段 {t} 不在營業時間「{hours}」內"))
    return out
```

在 `validate()` 裡、規則 8 的檢查之後、`return sorted(...)` 之前加:

```python
    # 規則 11、12:booking.yaml(選填,沒有就不檢查)
    if data.get("booking") is not None:
        out.extend(_validate_booking(data["booking"], hours))
```

並把模組 docstring 不動;`hours` 已經是規則 8 那段算好的字串。

- [ ] **Step 5: 跑測試確認通過,再做變異檢查**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider tests/test_validate.py tests/test_industries.py`
Expected: 全部 passed。

變異檢查(先 `cp app/knowledge/validate.py "$TEMP/validate_orig.py"`,每個改動各跑一次上面兩個檔,
每個都要有測試失敗,最後 `cp "$TEMP/validate_orig.py" app/knowledge/validate.py` 並確認 `cmp` 一致):

| 改動 | 應該抓到的測試 |
|---|---|
| `if start < end else (m >= start or m < end)` → `if True else False`(只認不跨午夜) | `test_rule12_a_range_past_midnight_is_understood` |
| `if ranges:` → `if True:`(解析不出區間也檢查) | `test_rule12_is_skipped_for_round_the_clock_shops` |
| `not any(` → `not all(`(要落在每一段) | `test_rule12_slots_may_fall_in_any_of_several_ranges` |
| `_is_int(value)` → `isinstance(value, int)` | `test_rule11_malformed_booking_is_error[over10-advance_days]` |

- [ ] **Step 6: 跑全套**

Run: 全套測試指令。Expected: 0 failed。

- [ ] **Step 7: Commit**

```bash
git add industries/restaurant/booking.yaml industries/clinic/booking.yaml app/knowledge/loader.py app/knowledge/validate.py tests/test_validate.py tests/test_industries.py
git commit -m "feat(knowledge): 選填的 booking.yaml 與驗證規則 11、12

餐廳與診所各一份(時段、名額、要問的欄位),電商沒有就不提供預約。
規則 11 擋格式錯誤(含沒加引號被 YAML 當成整數的時段);規則 12 把
落在營業時間外的時段列為 BLOCK,跨午夜的營業時間也認得。

Co-Authored-By: <實際執行的模型>"
```

---

### Task 2: `bookings` 表、`companies.booking_config` 與 seed

**Files:**
- Create: `app/models/booking.py`
- Modify: `app/models/company.py`(`contact` 欄位之後)、`app/models/__init__.py`
- Create: `alembic/versions/c5e8a1f3d920_bookings.py`
- Modify: `app/cli.py`(`run_seed` 裡 `company.contact = ...` 之後)
- Test: `tests/test_alembic.py`、`tests/test_cli.py`

**Interfaces:**
- Consumes: `data.get("booking")`(Task 1)
- Produces:`app.models.Booking`(欄位 `id`、`company_id`、`user_id`、`booking_date: date`、`booking_time: str`、
  `party_size: int`、`extra: dict`、`name`、`phone`、`status: str`、`created_at`);`Company.booking_config: dict | None`。
  Task 3、6、7 使用。

- [ ] **Step 1: 寫失敗的測試**

`tests/test_alembic.py` 檔尾加:

```python
CONTACT_INDEX = "7b1e4c2a9d30"


def test_upgrade_adds_bookings_and_booking_config(alembic_sqlite):
    cfg, url = alembic_sqlite
    command.upgrade(cfg, "head")
    assert "booking_config" in _columns(url)
    engine = create_engine(url)
    try:
        insp = inspect(engine)
        assert "bookings" in insp.get_table_names()
        assert "ix_bookings_slot" in {i["name"] for i in insp.get_indexes("bookings")}
    finally:
        engine.dispose()


def test_downgrade_removes_bookings_and_booking_config(alembic_sqlite):
    cfg, url = alembic_sqlite
    command.upgrade(cfg, "head")
    command.downgrade(cfg, CONTACT_INDEX)
    assert "booking_config" not in _columns(url)
    engine = create_engine(url)
    try:
        assert "bookings" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
```

`tests/test_cli.py` 檔尾加:

```python
def test_seed_stores_the_booking_config():
    cid = run_seed("restaurant", slug="bistro", channel_secret=SECRET, channel_token=TOKEN)
    with session_scope() as db:
        config = db.get(Company, cid).booking_config
    assert config["label"] == "訂位" and config["times"] == ["18:00", "19:00", "20:00"]


def test_switching_to_an_industry_without_bookings_clears_the_config():
    """換成電商卻留著餐廳的預約設定,電商的客服就會開始幫人「訂位」。"""
    cid = run_seed("restaurant", slug="bistro", channel_secret=SECRET, channel_token=TOKEN)
    run_seed("ecommerce", slug="bistro")
    with session_scope() as db:
        assert db.get(Company, cid).booking_config is None
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider tests/test_alembic.py tests/test_cli.py -k "booking"`
Expected: 4 failed(alembic 兩個是 `booking_config` 不在欄位裡;cli 兩個是 `AttributeError: 'Company' object has no attribute 'booking_config'`)。

- [ ] **Step 3: 實作 model**

新檔 `app/models/booking.py`:

```python
"""bookings —— 預約。設計見 docs/superpowers/specs/2026-10-06-booking-agent-design.md 第四節。"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        # 查名額:某家公司、某天、某時段有幾筆
        Index("ix_bookings_slot", "company_id", "booking_date", "booking_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    # 哪位 LINE 客人訂的(防重複:同一人同一時段只會有一筆)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    booking_date: Mapped[date] = mapped_column(Date, nullable=False)
    booking_time: Mapped[str] = mapped_column(String(5), nullable=False)  # HH:MM
    party_size: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # booking.yaml 的 fields(例如診所的看診項目)
    extra: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)  # 去掉 - 與空白
    # 先只有 confirmed;欄位預留給之後的取消
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="confirmed")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
```

`app/models/company.py`,在 `contact` 欄位定義之後加:

```python
    # booking.yaml 的內容(seed 時寫入,執行時不讀 YAML 檔 —— 跟 escalation_rules 一樣)。
    # None = 這家店不提供預約,工具清單裡也不會有預約工具。
    booking_config: Mapped[dict | None] = mapped_column(JSON)
```

(`JSON` 已經在 `company.py` 的 import 裡。)

`app/models/__init__.py`:加 `from app.models.booking import Booking`,`__all__` 加 `"Booking"`。

- [ ] **Step 4: 實作遷移**

新檔 `alembic/versions/c5e8a1f3d920_bookings.py`:

```python
"""bookings and companies.booking_config

Revision ID: c5e8a1f3d920
Revises: 7b1e4c2a9d30
Create Date: 2026-10-07 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c5e8a1f3d920'
down_revision: Union[str, Sequence[str], None] = '7b1e4c2a9d30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 可為空:既有的公司是 NULL(不提供預約),重跑 seed 才會填上。
    # batch_alter_table:SQLite 的 DROP COLUMN 要靠 batch 模式重建表(downgrade 用得到)。
    with op.batch_alter_table("companies") as batch:
        batch.add_column(sa.Column("booking_config", sa.JSON(), nullable=True))
    # 新表沒有既有資料列,所以 NOT NULL 欄位不需要 server_default
    op.create_table(
        "bookings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("company_id", sa.String(length=36),
                  sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(length=36),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("booking_date", sa.Date(), nullable=False),
        sa.Column("booking_time", sa.String(length=5), nullable=False),
        sa.Column("party_size", sa.Integer(), nullable=False),
        sa.Column("extra", sa.JSON(), nullable=False),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("phone", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_bookings_slot", "bookings",
                    ["company_id", "booking_date", "booking_time"])


def downgrade() -> None:
    op.drop_index("ix_bookings_slot", table_name="bookings")
    op.drop_table("bookings")
    with op.batch_alter_table("companies") as batch:
        batch.drop_column("booking_config")
```

- [ ] **Step 5: seed 寫入**

`app/cli.py` 的 `run_seed`,在 `company.contact = data["company"]["contact"]` 之後加:

```python
        # 沒有 booking.yaml 的行業要寫回 None —— 從餐廳換成電商時,不能留著餐廳的預約設定
        company.booking_config = data.get("booking")
```

- [ ] **Step 6: 跑測試確認通過**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider tests/test_alembic.py tests/test_cli.py tests/test_models.py`
Expected: 全部 passed。

- [ ] **Step 7: 跑全套,Commit**

Run: 全套測試指令。Expected: 0 failed。

```bash
git add app/models/booking.py app/models/company.py app/models/__init__.py alembic/versions/c5e8a1f3d920_bookings.py app/cli.py tests/test_alembic.py tests/test_cli.py
git commit -m "feat(db): bookings 表與 companies.booking_config

seed 把 booking.yaml 寫進 booking_config;換成沒有預約的行業時寫回 None。
bookings 以(公司、日期、時段)建索引給查名額用,status 先只有 confirmed。

Co-Authored-By: <實際執行的模型>"
```

---

### Task 3: 預約的業務邏輯 `app/booking.py`

**Files:**
- Create: `app/booking.py`
- Create: `tests/test_booking.py`

**Interfaces:**
- Consumes: `Booking`、`Company`(Task 2)
- Produces(Task 6、7 使用):
  - 常數 `TAIPEI`、`WEEKDAYS = "一二三四五六日"`、`CHECK_TOOL = "check_availability"`、`CREATE_TOOL = "create_booking"`
  - `today_taipei(now: datetime) -> date`
  - `build_tools(config: dict) -> list[dict]`(順序:查空位、建立預約)
  - `booking_prompt(config: dict, *, today: date, contact: str | None) -> str`
  - `check_availability(db, *, company_id: str, config: dict, contact: str | None, now: datetime, arguments: dict) -> dict`
  - `create_booking(db, *, company_id: str, user_pk: str, config: dict, contact: str | None, now: datetime, arguments: dict) -> dict`
    成功 `{"ok": True, "booking": {...}}`(重複時多 `"duplicate": True`),失敗 `{"ok": False, "error": "..."}`;
    `booking` 的鍵:`date`(ISO)、`weekday`、`time`、`party_size`、`name`、`phone`、`extra`
  - `confirmation_text(config: dict, booking: dict) -> str`、`staff_message(config: dict, booking: dict) -> str`

- [ ] **Step 1: 寫失敗的測試**

新檔 `tests/test_booking.py`:

```python
"""預約的業務邏輯。設計見 docs/superpowers/specs/2026-10-06-booking-agent-design.md 第五節。

用真的資料庫:名額、防重複都是查詢,mock 掉就什麼都沒驗到。
「現在」固定在台灣 2026-10-06(二)12:00 —— 週一公休、14 天範圍的邊界都看得到。
"""

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

from app import booking
from app.database import Base, engine, session_scope
from app.models import Booking, Company, User

NOW = datetime(2026, 10, 6, 4, 0, tzinfo=timezone.utc)        # 台灣 10/06(二)12:00
EVENING = datetime(2026, 10, 7, 10, 30, tzinfo=timezone.utc)   # 台灣 10/07(三)18:30
CONTACT = "02-2345-6789"
RESTAURANT = {"label": "訂位", "days": ["二", "三", "四", "五", "六", "日"],
              "times": ["18:00", "19:00", "20:00"], "capacity": 5, "max_party": 6,
              "advance_days": 14, "fields": []}
CLINIC = {"label": "預約", "days": ["一", "二", "三", "四", "五", "六"],
          "times": ["09:30", "10:30", "14:30", "15:30"], "capacity": 3, "max_party": 1,
          "advance_days": 14,
          "fields": [{"key": "service", "label": "看診項目",
                      "options": ["一般皮膚", "青春痘", "醫美諮詢"]}]}
OK_ARGS = {"date": "2026-10-09", "time": "20:00", "party_size": 4,
           "name": "王小明", "phone": "0912-345-678"}
CLINIC_ARGS = {"date": "2026-10-09", "time": "09:30", "name": "王小明",
               "phone": "0912345678", "service": "青春痘"}


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def ids():
    with session_scope() as db:
        c = Company(slug="bistro", name="微醺", industry="restaurant",
                    vector_collection="kb_bistro",
                    line_channel_secret_enc=b"x", line_channel_token_enc=b"x")
        db.add(c)
        db.flush()
        u = User(company_id=c.id, line_user_id="U1")
        db.add(u)
        db.flush()
        return c.id, u.id


def _user(company_id, line_user_id):
    with session_scope() as db:
        u = User(company_id=company_id, line_user_id=line_user_id)
        db.add(u)
        db.flush()
        return u.id


def _create(ids, args=None, *, config=RESTAURANT, now=NOW, user_pk=None):
    company_id, me = ids
    with session_scope() as db:
        return booking.create_booking(
            db, company_id=company_id, user_pk=user_pk or me, config=config,
            contact=CONTACT, now=now, arguments=dict(OK_ARGS if args is None else args))


def _check(ids, args, *, config=RESTAURANT, now=NOW):
    with session_scope() as db:
        return booking.check_availability(db, company_id=ids[0], config=config,
                                          contact=CONTACT, now=now, arguments=args)


def _rows():
    with session_scope() as db:
        return db.scalars(select(Booking)).all()


# --- 工具定義 -----------------------------------------------------------------

def test_restaurant_tools_ask_for_party_size_and_offer_only_its_slots():
    check, create = booking.build_tools(RESTAURANT)
    assert check["function"]["name"] == "check_availability"
    assert create["function"]["name"] == "create_booking"
    params = create["function"]["parameters"]
    assert params["properties"]["time"]["enum"] == ["18:00", "19:00", "20:00"]
    assert params["properties"]["party_size"]["maximum"] == 6
    assert set(params["required"]) == {"date", "time", "party_size", "name", "phone"}
    assert set(check["function"]["parameters"]["required"]) == {"date", "party_size"}


def test_clinic_tools_ask_for_the_service_instead_of_party_size():
    check, create = booking.build_tools(CLINIC)
    props = create["function"]["parameters"]["properties"]
    assert "party_size" not in props
    assert "party_size" not in check["function"]["parameters"]["properties"]
    assert props["service"]["enum"] == ["一般皮膚", "青春痘", "醫美諮詢"]
    assert "service" in create["function"]["parameters"]["required"]


# --- 預約說明(日期對照表) ----------------------------------------------------

def test_the_prompt_gives_today_and_a_date_table_with_closed_days():
    p = booking.booking_prompt(RESTAURANT, today=date(2026, 10, 6), contact=CONTACT)
    assert "今天是 2026-10-06(二)" in p
    assert "2026-10-09(五)" in p
    assert "2026-10-12(一,公休)" in p
    assert "2026-10-20(二)" in p and "2026-10-21" not in p   # 今天起算 14 天
    assert "不要反問" in p
    assert "最多 6 位" in p and CONTACT in p


def test_the_clinic_prompt_asks_for_the_service_not_the_party_size():
    p = booking.booking_prompt(CLINIC, today=date(2026, 10, 6), contact=CONTACT)
    assert "看診項目" in p
    assert "人數" not in p and "最多" not in p


# --- 查空位 -------------------------------------------------------------------

def test_an_open_day_lists_every_slot(ids):
    assert _check(ids, {"date": "2026-10-09", "party_size": 4}) == {
        "date": "2026-10-09", "weekday": "五", "open": True,
        "slots": [{"time": "18:00", "available": True},
                  {"time": "19:00", "available": True},
                  {"time": "20:00", "available": True}]}


@pytest.mark.parametrize("args, fragment", [
    ({"date": "2026-10-12", "party_size": 2}, "10/12(一)沒有開放訂位"),
    ({"date": "2026-10-21", "party_size": 2}, "只能訂位 10/06~10/20"),
    ({"date": "2026-10-05", "party_size": 2}, "只能訂位 10/06~10/20"),
    ({"date": "10月9日", "party_size": 2}, "YYYY-MM-DD"),
    ({"date": "2026-10-09", "party_size": 7}, "超過 6 位請直接聯繫 02-2345-6789"),
    ({"date": "2026-10-09", "party_size": 0}, "人數至少 1 位"),
    ({"date": "2026-10-09"}, "請先問客人幾位"),
])
def test_check_explains_why_it_cannot_answer(ids, args, fragment):
    assert fragment in _check(ids, args)["error"]


def test_a_full_slot_is_reported_unavailable(ids):
    company_id, _ = ids
    for i in range(5):
        assert _create(ids, {**OK_ARGS, "time": "19:00"},
                       user_pk=_user(company_id, f"F{i}"))["ok"]
    slots = _check(ids, {"date": "2026-10-09", "party_size": 2})["slots"]
    assert [s["available"] for s in slots] == [True, False, True]


def test_slots_already_past_today_are_unavailable(ids):
    slots = _check(ids, {"date": "2026-10-07", "party_size": 2}, now=EVENING)["slots"]
    assert [s["available"] for s in slots] == [False, True, True]


# --- 建立預約 -----------------------------------------------------------------

def test_a_valid_booking_is_saved_with_a_normalised_phone(ids):
    assert _create(ids) == {"ok": True, "booking": {
        "date": "2026-10-09", "weekday": "五", "time": "20:00", "party_size": 4,
        "name": "王小明", "phone": "0912345678", "extra": {}}}
    (row,) = _rows()
    assert (row.booking_date, row.booking_time, row.party_size, row.phone, row.status) == (
        date(2026, 10, 9), "20:00", 4, "0912345678", "confirmed")


@pytest.mark.parametrize("patch, fragment", [
    ({"time": "17:00"}, "時段只有 18:00、19:00、20:00"),
    ({"date": "2026-10-12"}, "沒有開放訂位"),
    ({"date": "2026-10-21"}, "只能訂位 10/06~10/20"),
    ({"party_size": 7}, "超過 6 位"),
    ({"party_size": "四"}, "人數要是數字"),
    ({"party_size": True}, "人數要是數字"),
    ({"name": "  "}, "姓名"),
    ({"name": "王" * 51}, "姓名"),
    ({"phone": "12345"}, "電話格式"),
    ({"phone": "0912-345-6789"}, "電話格式"),
])
def test_invalid_bookings_are_refused_with_a_reason(ids, patch, fragment):
    result = _create(ids, {**OK_ARGS, **patch})
    assert result["ok"] is False and fragment in result["error"]
    assert _rows() == []


@pytest.mark.parametrize("phone, stored", [
    ("0912 345 678", "0912345678"),
    ("02-2345-6789", "0223456789"),
])
def test_mobile_and_landline_numbers_are_accepted(ids, phone, stored):
    assert _create(ids, {**OK_ARGS, "phone": phone})["booking"]["phone"] == stored


def test_a_slot_already_past_today_is_refused(ids):
    result = _create(ids, {**OK_ARGS, "date": "2026-10-07", "time": "18:00"}, now=EVENING)
    assert result["ok"] is False and "今天 18:00 已經過了" in result["error"]


def test_a_full_slot_is_refused(ids):
    company_id, _ = ids
    for i in range(5):
        assert _create(ids, user_pk=_user(company_id, f"F{i}"))["ok"]
    result = _create(ids)
    assert result["ok"] is False and "10/09 20:00 剛好滿了" in result["error"]
    assert len(_rows()) == 5


def test_the_last_free_slot_can_still_be_booked(ids):
    company_id, _ = ids
    for i in range(4):
        _create(ids, user_pk=_user(company_id, f"F{i}"))
    assert _create(ids)["ok"] is True


def test_booking_the_same_slot_twice_returns_the_first_booking(ids):
    """模型偶爾會重複呼叫 —— 客人不能被訂兩次。"""
    _create(ids)
    again = _create(ids, {**OK_ARGS, "party_size": 2})
    assert again["ok"] is True and again["duplicate"] is True
    assert again["booking"]["party_size"] == 4
    assert len(_rows()) == 1


def test_the_clinic_stores_the_service_and_ignores_any_party_size(ids):
    result = _create(ids, {**CLINIC_ARGS, "party_size": 3}, config=CLINIC)
    assert result["ok"] is True
    assert result["booking"]["party_size"] == 1
    assert result["booking"]["extra"] == {"service": "青春痘"}


def test_the_clinic_refuses_a_service_it_does_not_offer(ids):
    result = _create(ids, {**CLINIC_ARGS, "service": "牙齒"}, config=CLINIC)
    assert result["ok"] is False
    assert "看診項目只能選:一般皮膚、青春痘、醫美諮詢" in result["error"]


# --- 文字 ---------------------------------------------------------------------

BOOKED = {"date": "2026-10-09", "weekday": "五", "time": "20:00", "party_size": 4,
          "name": "王小明", "phone": "0912345678", "extra": {}}
CLINIC_BOOKED = {**BOOKED, "time": "09:30", "party_size": 1, "extra": {"service": "青春痘"}}


def test_confirmation_texts():
    assert booking.confirmation_text(RESTAURANT, BOOKED) == (
        "已為您訂位 10/09(五)20:00,4 位,訂位人王小明。")
    assert booking.confirmation_text(CLINIC, CLINIC_BOOKED) == (
        "已為您預約 10/09(五)09:30,看診項目:青春痘,預約人王小明。")


def test_staff_messages():
    assert booking.staff_message(RESTAURANT, BOOKED) == (
        "【新訂位】10/09(五)20:00 4 位 王小明 0912345678")
    assert booking.staff_message(CLINIC, CLINIC_BOOKED) == (
        "【新預約】10/09(五)09:30 青春痘 王小明 0912345678")
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider tests/test_booking.py`
Expected: 收集時 `ImportError: cannot import name 'booking' from 'app'`。

- [ ] **Step 3: 實作 `app/booking.py`**

```python
"""預約:工具定義、每次請求附加的預約說明、查空位、建立預約、確認與通知文字。

設計見 docs/superpowers/specs/2026-10-06-booking-agent-design.md 第五節。
所有檢查都在這裡 —— 模型只負責對話,它是會出錯的那一方。不通過就回傳
中文錯誤(`{"ok": False, "error": ...}`),讓模型轉述給客人。

這個模組不開 session、不送訊息:session 由呼叫端給,送出是 webhook 的事。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Booking, Company

# 台灣沒有日光節約,固定 UTC+8 就對,不必依賴容器裡有沒有時區資料庫
TAIPEI = timezone(timedelta(hours=8))
WEEKDAYS = "一二三四五六日"
CHECK_TOOL = "check_availability"
CREATE_TOOL = "create_booking"
CONFIRMED = "confirmed"
# 手機 09 開頭 10 碼、市話 0 開頭 9-10 碼(去掉 - 與空白之後)
PHONE_RE = re.compile(r"^0\d{8,9}$")
NAME_MAX = 50


def today_taipei(now: datetime) -> date:
    return now.astimezone(TAIPEI).date()


def _weekday(d: date) -> str:
    return WEEKDAYS[d.weekday()]


def _md(d: date) -> str:
    return f"{d.month:02d}/{d.day:02d}"


def _asks_party(config: dict) -> bool:
    return config["max_party"] > 1


def _fail(error: str) -> dict:
    return {"ok": False, "error": error}


# --- 工具定義與預約說明 ---------------------------------------------------------

def build_tools(config: dict) -> list[dict]:
    """由 booking_config 產生兩個工具:換行業時自動跟著變,跟轉真人工具同一套做法。"""
    label = config["label"]
    date_param = {"type": "string", "description": "YYYY-MM-DD,照日期對照表換算"}
    party_param = {"type": "integer", "minimum": 1, "maximum": config["max_party"],
                   "description": "人數"}
    check_props: dict = {"date": date_param}
    create_props: dict = {
        "date": date_param,
        "time": {"type": "string", "enum": list(config["times"]), "description": "時段"},
        "name": {"type": "string", "description": f"{label}人姓名"},
        "phone": {"type": "string", "description": "聯絡電話"},
    }
    if _asks_party(config):
        check_props["party_size"] = party_param
        create_props["party_size"] = party_param
    for field in config.get("fields") or []:
        create_props[field["key"]] = {"type": "string", "enum": list(field["options"]),
                                      "description": field["label"]}
    return [
        {"type": "function", "function": {
            "name": CHECK_TOOL,
            "description": f"查某一天各時段還能不能{label}。建立{label}前一定要先查。",
            "parameters": {"type": "object", "properties": check_props,
                           "required": list(check_props)}}},
        {"type": "function", "function": {
            "name": CREATE_TOOL,
            "description": f"建立{label}。只有查過空位、而且資料都齊全時才呼叫。",
            "parameters": {"type": "object", "properties": create_props,
                           "required": list(create_props)}}},
    ]


def booking_prompt(config: dict, *, today: date, contact: str | None) -> str:
    """接在 system prompt 後面的預約說明。每次請求現算 —— 日期每天在變,不存資料庫。

    日期對照表是實測的教訓:讓模型自己從「今天星期幾」推算「週五」會算錯,
    給它一張表照著查就不會。
    """
    label = config["label"]
    open_days = set(config["days"])
    span = (today + timedelta(days=n) for n in range(config["advance_days"] + 1))
    table = "、".join(
        f"{d.isoformat()}({_weekday(d)}{'' if _weekday(d) in open_days else ',公休'})"
        for d in span)
    needs = ["日期", "時段"]
    if _asks_party(config):
        needs.append("人數")
    needs += [f["label"] for f in config.get("fields") or []]
    needs += ["姓名", "電話"]
    lines = [
        f"## {label}",
        f"今天是 {today.isoformat()}({_weekday(today)}),時區是台灣。",
        f"可以{label}的日期對照(換算日期一律照這張表,不要自己推算):{table}",
        f"時段:{'、'.join(config['times'])}。",
        "- 客人只說星期幾(例如「週五」)而沒有特別說明時,就是從今天起算最近的那一天,"
        "不要反問是哪一週。",
        f"- 建立{label}前一定要先用 {CHECK_TOOL} 查空位,依查詢結果回答;不可以自己編造空位。",
        f"- 建立{label}需要:{'、'.join(needs)}。缺什麼就一次問完。",
    ]
    if _asks_party(config):
        lines.append(f"- 每筆最多 {config['max_party']} 位,超過請客人直接聯繫 {contact or '店家'}。")
    lines.append(f"- {CREATE_TOOL} 成功後,用一句話確認內容;失敗時照工具回傳的原因向客人說明。")
    return "\n".join(lines)


# --- 檢查 ---------------------------------------------------------------------

def _parse_date(config: dict, raw, now: datetime) -> tuple[date | None, str | None]:
    label = config["label"]
    try:
        d = date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None, "日期格式要是 YYYY-MM-DD"
    today = today_taipei(now)
    last = today + timedelta(days=config["advance_days"])
    if not today <= d <= last:
        return None, f"只能{label} {_md(today)}~{_md(last)}"
    if _weekday(d) not in config["days"]:
        return None, f"{_md(d)}({_weekday(d)})沒有開放{label}"
    return d, None


def _party(config: dict, raw, contact: str | None) -> tuple[int | None, str | None]:
    if raw is None:
        return None, "還不知道人數,請先問客人幾位"
    if isinstance(raw, bool):  # bool 是 int 的子類別:True 不能被當成 1 位
        return None, "人數要是數字"
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None, "人數要是數字"
    if n < 1:
        return None, "人數至少 1 位"
    if n > config["max_party"]:
        return None, f"超過 {config['max_party']} 位請直接聯繫 {contact or '店家'}"
    return n, None


def _slot_passed(d: date, slot: str, now: datetime) -> bool:
    local = now.astimezone(TAIPEI)
    if d != local.date():
        return False
    hh, mm = (int(x) for x in slot.split(":"))
    return local.hour * 60 + local.minute >= hh * 60 + mm


def _taken(db: Session, company_id: str, d: date, slot: str) -> int:
    return db.scalar(select(func.count()).select_from(Booking).where(
        Booking.company_id == company_id, Booking.booking_date == d,
        Booking.booking_time == slot, Booking.status == CONFIRMED))


def _public(row: Booking) -> dict:
    return {"date": row.booking_date.isoformat(), "weekday": _weekday(row.booking_date),
            "time": row.booking_time, "party_size": row.party_size, "name": row.name,
            "phone": row.phone, "extra": dict(row.extra or {})}


# --- 兩個工具的實作 -------------------------------------------------------------

def check_availability(db: Session, *, company_id: str, config: dict,
                       contact: str | None, now: datetime, arguments: dict) -> dict:
    """只回能不能訂,不回剩幾組。"""
    d, error = _parse_date(config, arguments.get("date"), now)
    if error:
        return {"error": error}
    if _asks_party(config):
        _, error = _party(config, arguments.get("party_size"), contact)
        if error:
            return {"error": error}
    slots = [{"time": t,
              "available": (not _slot_passed(d, t, now)
                            and _taken(db, company_id, d, t) < config["capacity"])}
             for t in config["times"]]
    return {"date": d.isoformat(), "weekday": _weekday(d), "open": True, "slots": slots}


def create_booking(db: Session, *, company_id: str, user_pk: str, config: dict,
                   contact: str | None, now: datetime, arguments: dict) -> dict:
    label = config["label"]
    d, error = _parse_date(config, arguments.get("date"), now)
    if error:
        return _fail(error)
    slot = str(arguments.get("time") or "")
    if slot not in config["times"]:
        return _fail(f"時段只有 {'、'.join(config['times'])}")
    if _slot_passed(d, slot, now):
        return _fail(f"今天 {slot} 已經過了")
    party = 1
    if _asks_party(config):
        party, error = _party(config, arguments.get("party_size"), contact)
        if error:
            return _fail(error)
    name = str(arguments.get("name") or "").strip()
    if not name or len(name) > NAME_MAX:
        return _fail(f"請提供{label}人姓名({NAME_MAX} 字以內)")
    phone = re.sub(r"[\s\-]", "", str(arguments.get("phone") or ""))
    if not PHONE_RE.match(phone):
        return _fail("電話格式看起來不對,請客人再確認一次")
    extra: dict = {}
    for field in config.get("fields") or []:
        value = str(arguments.get(field["key"]) or "")
        if value not in field["options"]:
            return _fail(f"{field['label']}只能選:{'、'.join(field['options'])}")
        extra[field["key"]] = value

    # 防重複:模型偶爾會重複呼叫 —— 同一位客人同一時段只會有一筆
    existing = db.scalar(select(Booking).where(
        Booking.company_id == company_id, Booking.user_id == user_pk,
        Booking.booking_date == d, Booking.booking_time == slot,
        Booking.status == CONFIRMED))
    if existing is not None:
        return {"ok": True, "duplicate": True, "booking": _public(existing)}

    # 名額:先鎖住這家公司的列再數 —— 兩個人同時搶最後一個名額時,第二個人要等
    # 第一個人 commit 完才數得到,看到的是已滿。SQLite 本來就一次只允許一個寫入。
    db.execute(select(Company.id).where(Company.id == company_id).with_for_update())
    if _taken(db, company_id, d, slot) >= config["capacity"]:
        return _fail(f"{_md(d)} {slot} 剛好滿了,請換個時段")
    row = Booking(company_id=company_id, user_id=user_pk, booking_date=d,
                  booking_time=slot, party_size=party, extra=extra, name=name,
                  phone=phone, status=CONFIRMED)
    db.add(row)
    db.flush()
    return {"ok": True, "booking": _public(row)}


# --- 文字 ---------------------------------------------------------------------

def _when(booking: dict) -> str:
    d = date.fromisoformat(booking["date"])
    return f"{_md(d)}({booking['weekday']}){booking['time']}"


def confirmation_text(config: dict, booking: dict) -> str:
    """預約成立、但送出去的不是模型自己的回答時,由程式補上的確認句。"""
    label = config["label"]
    parts = [f"已為您{label} {_when(booking)}"]
    if _asks_party(config):
        parts.append(f"{booking['party_size']} 位")
    for field in config.get("fields") or []:
        value = booking["extra"].get(field["key"])
        if value:
            parts.append(f"{field['label']}:{value}")
    parts.append(f"{label}人{booking['name']}")
    return ",".join(parts) + "。"


def staff_message(config: dict, booking: dict) -> str:
    parts = [f"【新{config['label']}】{_when(booking)}"]
    if _asks_party(config):
        parts.append(f"{booking['party_size']} 位")
    for field in config.get("fields") or []:
        value = booking["extra"].get(field["key"])
        if value:
            parts.append(value)
    parts += [booking["name"], booking["phone"]]
    return " ".join(parts)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider tests/test_booking.py`
Expected: 全部 passed。

- [ ] **Step 5: 變異檢查(手動,改完還原)**

先 `cp app/booking.py "$TEMP/booking_orig.py"`;逐一套用、各跑一次 `tests/test_booking.py`;
最後 `cp "$TEMP/booking_orig.py" app/booking.py` 並確認 `cmp` 一致。

| 改動 | 應該抓到的測試 |
|---|---|
| `>= config["capacity"]` → `> config["capacity"]` | `test_a_full_slot_is_refused` |
| 拿掉防重複那段(`existing` 查詢與 return) | `test_booking_the_same_slot_twice_returns_the_first_booking` |
| `today_taipei(now)` → `now.date()`(用 UTC 日期) | `test_slots_already_past_today_are_unavailable` 或 `test_a_slot_already_past_today_is_refused` |
| `_weekday(d) not in config["days"]` → `False` | `test_check_explains_why_it_cannot_answer[args0-…]` |
| `PHONE_RE` 改成 `r"^0\d{8,10}$"` | `test_invalid_bookings_are_refused_with_a_reason[patch9-電話格式]` |
| `booking_prompt` 的對照表 `range(config["advance_days"] + 1)` → `range(config["advance_days"])` | `test_the_prompt_gives_today_and_a_date_table_with_closed_days` |

- [ ] **Step 6: 跑全套,Commit**

Run: 全套測試指令。Expected: 0 failed。

```bash
git add app/booking.py tests/test_booking.py
git commit -m "feat(booking): 查空位、建立預約與所有檢查

工具定義由 booking_config 產生;每次請求附加日期對照表(實測:模型自己算
「週五」會錯)。建立時逐項檢查日期範圍、公休、已過的時段、人數、電話、
選項,防重複,鎖住公司列再數名額。確認句與店員通知文字也在這裡。

Co-Authored-By: <實際執行的模型>"
```

---

### Task 4: `llm.py` 回傳所有工具呼叫、接受截止時間

**Files:**
- Modify: `app/agent/llm.py`
- Test: `tests/test_agent.py`

**Interfaces:**
- Produces(Task 5、6 使用):
  - `ToolCall(name, arguments, id="")` —— `id` 不參與比較(`field(default="", compare=False)`)
  - `LLMResult.tool_calls: tuple[ToolCall, ...] = ()`(放最後;`tool_call` 保留 = 第一個)
  - `complete(messages, *, tools=None, client=None, deadline: float | None = None) -> LLMResult`;
    `deadline` 是 `time.monotonic()` 的秒數,有傳就取代 `llm_total_budget_seconds`

- [ ] **Step 1: 寫失敗的測試**

`tests/test_agent.py` 檔尾加(`_http`、`_ok`、`TOOL`、`_by_model`、`_trickling`、`TRICKLE_SECONDS`、
`PREFERRED`、`FALLBACK`、`_json`、`_time` 都是檔案裡既有的):

```python
# --- agent loop 需要的:所有工具呼叫、截止時間 ---------------------------------

def _two_tool_calls():
    return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "check_availability", "arguments": '{"date": "2026-10-09"}'}},
        {"id": "c2", "type": "function",
         "function": {"name": "create_booking", "arguments": '{"time": "20:00"}'}}]}}],
        "usage": {"total_tokens": 9}})


def test_every_tool_call_is_returned_with_its_id():
    """工具結果放回對話時,tool_call_id 要對得上 —— 只回第一個的話,第二個工具永遠不會被執行。"""
    r = complete([{"role": "user", "content": "嗨"}], tools=[TOOL],
                 client=_http(lambda req: _two_tool_calls()))
    assert [(c.name, c.arguments, c.id) for c in r.tool_calls] == [
        ("check_availability", {"date": "2026-10-09"}, "c1"),
        ("create_booking", {"time": "20:00"}, "c2")]
    assert r.tool_call == ToolCall("check_availability", {"date": "2026-10-09"})


def test_a_tool_call_without_an_id_gets_one():
    resp = httpx.Response(200, json={"choices": [{"message": {"tool_calls": [
        {"type": "function", "function": {"name": "check_availability", "arguments": "{}"}}]}}]})
    r = complete([{"role": "user", "content": "嗨"}], tools=[TOOL],
                 client=_http(lambda req: resp))
    assert r.tool_calls[0].id == "call_0"


def test_tool_results_are_sent_upstream_unchanged():
    msgs = [{"role": "user", "content": "嗨"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "check_availability", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": '{"ok": true}'}]
    seen = []

    def handler(request):
        seen.append(_json.loads(request.content)["messages"])
        return _ok()

    complete(msgs, client=_http(handler))
    assert seen == [msgs]


def test_a_deadline_overrides_the_default_budget():
    """agent loop 的每一步拿的是剩下的時間。不照 deadline 停的話,一步就能吃掉整個 loop 的預算。"""
    started = _time.monotonic()
    with pytest.raises(LLMError):
        complete([{"role": "user", "content": "嗨"}],
                 client=_http(_by_model({PREFERRED: _trickling(), FALLBACK: _trickling()})),
                 deadline=_time.monotonic() + 0.5)
    assert _time.monotonic() - started < TRICKLE_SECONDS
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider tests/test_agent.py -k "tool_call or deadline or upstream_unchanged"`
Expected: `test_every_tool_call_is_returned_with_its_id`、`test_a_tool_call_without_an_id_gets_one` failed
(`AttributeError: 'LLMResult' object has no attribute 'tool_calls'`);`test_a_deadline_overrides_the_default_budget`
failed(`TypeError: complete() got an unexpected keyword argument 'deadline'`);`test_tool_results_are_sent_upstream_unchanged`
passed(守既有的「原樣轉送」行為)。

- [ ] **Step 3: 實作**

`app/agent/llm.py`:

1. import 改成 `from dataclasses import dataclass, field, replace`。
2. `ToolCall` 加最後一個欄位:

```python
    # 上游給的 id。工具結果放回對話時要靠它對上是哪一個呼叫。不參與比較 ——
    # 判斷「是不是同一個呼叫」看的是名稱與參數,id 每次都不一樣。
    id: str = field(default="", compare=False)
```

3. `LLMResult` 在 `tool_call` 之後加:

```python
    # 這一步所有的工具呼叫(agent loop 會依序執行);tool_call 保留 = 第一個,
    # 既有呼叫端不用改
    tool_calls: tuple[ToolCall, ...] = ()
```

4. 把 `_parse_tool_call` 換成下面兩個函式(參數解析的邏輯照搬原本的):

```python
def _parse_arguments(raw) -> dict:
    # arguments 可能已經是字典(模型或上游直接傳的),可能是 JSON 字串,
    # 也可能是其他型別。只有字典才是有效的,其他都降級到 {}。
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _parse_tool_calls(message: dict) -> tuple[ToolCall, ...]:
    """這一步所有的工具呼叫。上游沒給 id 時自己編一個 —— 放回對話時一定要有。"""
    out = []
    for i, call in enumerate(message.get("tool_calls") or []):
        fn = call.get("function") or {}
        out.append(ToolCall(name=fn.get("name") or "",
                            arguments=_parse_arguments(fn.get("arguments")),
                            id=call.get("id") or f"call_{i}"))
    return tuple(out)
```

5. `_complete_once` 裡:
   - 超時訊息 `f"超過整體時間上限 {s.llm_total_budget_seconds:g} 秒,上游還沒送完"` 改成 `"超過時間上限,上游還沒送完"`
     (上限現在可能來自 agent loop 的 deadline,不一定是那個設定值)。
   - `tool_call = _parse_tool_call(message)` 改成:

```python
    tool_calls = _parse_tool_calls(message)
    tool_call = tool_calls[0] if tool_calls else None
```

   - log 的最後一個參數 `tool_call.name if tool_call else "無"` 改成 `",".join(c.name for c in tool_calls) or "無"`。
   - `return LLMResult(...)` 加 `tool_calls=tool_calls`。

6. `complete` 的簽名改成:

```python
def complete(messages: list[dict], *, tools: list[dict] | None = None,
             client: httpx.Client | None = None,
             deadline: float | None = None) -> LLMResult:
```

   docstring 最後一段加一句「`deadline`(`time.monotonic()` 的秒數)有傳就用它 —— agent loop 每一步拿的是剩下的時間。」
   並把 `deadline = started + s.llm_total_budget_seconds` 改成:

```python
    if deadline is None:
        deadline = started + s.llm_total_budget_seconds
```

- [ ] **Step 4: 跑測試確認通過**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider tests/test_agent.py`
Expected: 全部 passed(含既有的 `test_a_tool_call_answer_logs_the_tool_name`、`test_broken_tool_arguments_still_produce_a_tool_call`)。

- [ ] **Step 5: 跑全套,Commit**

Run: 全套測試指令。Expected: 0 failed。

```bash
git add app/agent/llm.py tests/test_agent.py
git commit -m "feat(llm): 回傳所有工具呼叫(含 id)、接受 agent loop 的截止時間

agent loop 要依序執行同一步的每個工具,並用 tool_call_id 把結果放回對話;
complete() 的 deadline 讓每一步只拿剩下的時間。tool_call 保留為第一個,
既有呼叫端不用改。

Co-Authored-By: <實際執行的模型>"
```

---

### Task 5: agent loop `app/agent/loop.py`

**Files:**
- Create: `app/agent/loop.py`
- Create: `tests/test_agent_loop.py`

**Interfaces:**
- Consumes: `LLMError`、`LLMResult`、`ToolCall`(含 `id`、`tool_calls`,Task 4);`app.agent.handoff.TOOL_NAME`
- Produces(Task 6 使用):
  - `AgentResult(text: str | None, handoff: ToolCall | None = None, booking: dict | None = None, steps: int = 0, tools_used: tuple[str, ...] = (), token_count: int | None = None, latency_ms: int = 0)`
  - `run_agent(messages: list[dict], *, tools: list[dict] | None, execute: Callable[[str, dict], dict], max_steps: int, budget_seconds: float, complete_fn: Callable[..., LLMResult]) -> AgentResult`
  - `execute` 回傳的 dict 若 `ok` 為真且有 `booking` 鍵,就記為本次成立的預約

- [ ] **Step 1: 寫失敗的測試**

新檔 `tests/test_agent_loop.py`:

```python
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
                          "tools": tools, "deadline": deadline})
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


def test_the_last_step_has_no_tools_so_the_model_must_answer():
    s = Script(_tools(_call("check_availability")),
               _tools(_call("check_availability", call_id="c2")),
               _text("請問要哪個時段?"))
    r = _run(s, lambda n, a: {"slots": []}, max_steps=3)
    assert [c["tools"] for c in s.seen] == [TOOLS, TOOLS, None]
    assert (r.text, r.steps) == ("請問要哪個時段?", 3)


def test_tool_calls_on_the_last_step_are_ignored():
    executed = []
    s = Script(_tools(_call("check_availability")),
               _tools(_call("create_booking", call_id="c2")))
    r = _run(s, lambda n, a: executed.append(n) or {"slots": []}, max_steps=2)
    assert executed == ["check_availability"] and r.text is None


def test_a_failure_after_booking_still_reports_the_booking():
    s = Script(_tools(_call("create_booking")), LLMError("上游掛了"))
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


def test_a_result_that_only_fills_tool_call_still_counts_as_a_tool_call():
    """既有的測試替身只填 tool_call、沒填 tool_calls —— loop 要一樣認得。"""
    s = Script(LLMResult("", token_count=1, latency_ms=1,
                         tool_call=ToolCall("transfer_to_human", {"category": "safety"})))
    assert _run(s).handoff == ToolCall("transfer_to_human", {"category": "safety"})


def test_no_tools_means_tools_is_none_on_every_step():
    s = Script(_text("好"))
    _run(s, tools=None)
    assert s.seen[0]["tools"] is None
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider tests/test_agent_loop.py`
Expected: 收集時 `ModuleNotFoundError: No module named 'app.agent.loop'`。

- [ ] **Step 3: 實作 `app/agent/loop.py`**

```python
"""agent loop:模型呼叫工具 → 程式執行 → 結果放回對話 → 模型再決定下一步。

設計見 docs/superpowers/specs/2026-10-06-booking-agent-design.md 第六節。
- 最多 max_steps 步;最後一步不帶工具,模型只能用文字回答,不會繞圈到什麼都沒回
- 所有步驟共用一個截止時間:每一步拿的是剩下的時間,不是每步各一份
- 轉真人優先:同一個回應裡有 transfer_to_human,其他工具一律不執行
- 這裡不碰資料庫、不送訊息:工具怎麼執行由呼叫端給的 execute 決定
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Callable

from app.agent.handoff import TOOL_NAME as HANDOFF_TOOL
from app.agent.llm import LLMError, LLMResult, ToolCall

logger = logging.getLogger(__name__)

# 剩不到一秒就不再問模型 —— 問了也等不到答案,只會讓客人多等
MIN_STEP_SECONDS = 1.0
TOOL_FAILURE = {"ok": False, "error": "系統暫時無法處理"}


@dataclass(frozen=True)
class AgentResult:
    text: str | None                 # 最後的文字回答;None = 模型失敗、超時或沒回文字
    handoff: ToolCall | None = None  # 模型要轉真人
    booking: dict | None = None      # 這次成立的預約(工具回傳的 booking)
    steps: int = 0
    tools_used: tuple[str, ...] = ()
    token_count: int | None = None
    latency_ms: int = 0


def _calls(result: LLMResult) -> tuple[ToolCall, ...]:
    # 既有的呼叫端(與測試替身)只填 tool_call、沒填 tool_calls
    if result.tool_calls:
        return result.tool_calls
    return (result.tool_call,) if result.tool_call is not None else ()


def run_agent(messages: list[dict], *, tools: list[dict] | None,
              execute: Callable[[str, dict], dict], max_steps: int,
              budget_seconds: float,
              complete_fn: Callable[..., LLMResult]) -> AgentResult:
    started = time.monotonic()
    deadline = started + budget_seconds
    msgs = list(messages)
    booking: dict | None = None
    used: list[str] = []
    tokens: int | None = None

    def finish(text: str | None, steps: int, handoff: ToolCall | None = None) -> AgentResult:
        return AgentResult(text=text, handoff=handoff, booking=booking, steps=steps,
                           tools_used=tuple(used), token_count=tokens,
                           latency_ms=int((time.monotonic() - started) * 1000))

    for step in range(1, max_steps + 1):
        if deadline - time.monotonic() < MIN_STEP_SECONDS:
            logger.warning("agent 時間用完,停在第 %d 步之前", step)
            return finish(None, step - 1)
        last = step == max_steps
        try:
            result = complete_fn(msgs, tools=None if last else tools, deadline=deadline)
        except LLMError as exc:
            logger.warning("agent 第 %d 步模型失敗:%s", step, exc)
            return finish(None, step)
        if result.token_count is not None:
            tokens = (tokens or 0) + result.token_count

        # 最後一步沒給工具;上游若還是回了工具呼叫,也不執行
        calls = () if last else _calls(result)
        if not calls:
            return finish(result.text or None, step)
        handoff = next((c for c in calls if c.name == HANDOFF_TOOL), None)
        if handoff is not None:
            return finish(None, step, handoff)

        msgs.append({"role": "assistant", "content": result.text or "", "tool_calls": [
            {"id": c.id, "type": "function",
             "function": {"name": c.name,
                          "arguments": json.dumps(c.arguments, ensure_ascii=False)}}
            for c in calls]})
        for c in calls:
            used.append(c.name)
            try:
                out = execute(c.name, c.arguments)
            except Exception:  # noqa: BLE001
                logger.exception("工具 %s 執行失敗", c.name)
                out = dict(TOOL_FAILURE)
            if out.get("ok") and out.get("booking"):
                booking = out["booking"]
            msgs.append({"role": "tool", "tool_call_id": c.id,
                         "content": json.dumps(out, ensure_ascii=False, default=str)})

    return finish(None, max_steps)  # 走不到:最後一步一定會 return
```

- [ ] **Step 4: 跑測試確認通過**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider tests/test_agent_loop.py`
Expected: 全部 passed。

- [ ] **Step 5: 變異檢查(手動,改完還原)**

先 `cp app/agent/loop.py "$TEMP/loop_orig.py"`;逐一套用、各跑一次 `tests/test_agent_loop.py`;最後 `cp` 回來、`cmp` 確認。

| 改動 | 應該抓到的測試 |
|---|---|
| `tools=None if last else tools` → `tools=tools` | `test_the_last_step_has_no_tools_so_the_model_must_answer` |
| `deadline=deadline` → `deadline=time.monotonic() + budget_seconds` | `test_every_step_shares_one_deadline` |
| 拿掉 `if handoff is not None: return …`(讓它被當一般工具執行) | `test_a_handoff_wins_and_other_tools_in_the_same_reply_are_not_run` |
| `if out.get("ok") and out.get("booking")` → `if out.get("booking") is not None or out.get("ok")` | `test_tool_results_go_back_…`(check 結果被當預約)或 `test_a_refused_booking_is_not_reported_as_booked` |
| `except LLMError` 那段改成 `raise` | `test_a_failure_after_booking_still_reports_the_booking` |
| `MIN_STEP_SECONDS = 1.0` → `0.0` | `test_the_loop_stops_when_the_budget_is_used_up` |

- [ ] **Step 6: 跑全套,Commit**

Run: 全套測試指令。Expected: 0 failed。

```bash
git add app/agent/loop.py tests/test_agent_loop.py
git commit -m "feat(agent): 自己寫的 tool-calling 迴圈

模型呼叫工具 → 執行 → 結果帶 tool_call_id 放回對話 → 再問,直到有文字。
最多 4 步、最後一步不帶工具;所有步驟共用一個截止時間;轉真人優先且
不執行同一回應的其他工具;工具例外變成錯誤結果還給模型。loop 不碰資料庫。

Co-Authored-By: <實際執行的模型>"
```

---

### Task 6: 接進 webhook

**Files:**
- Modify: `app/config.py`(`rate_limit_global_per_minute` 之後)
- Modify: `app/routers/webhook.py`
- Test: `tests/test_webhook.py`

**Interfaces:**
- Consumes: `app.booking` 全部(Task 3)、`run_agent`/`AgentResult`(Task 5)、`complete(..., deadline=)`(Task 4)、`Company.booking_config`/`Booking`(Task 2)
- Produces: 無(終端行為)

- [ ] **Step 1: 寫失敗的測試**

`tests/test_webhook.py` 檔尾加(`rules`、`line_out`、`_llm`、`_replies`、`_pushes`、`_mode`、`_body`、`_post`、
`SAFETY_SCRIPT`、`LLMResult`、`LLMError`、`ToolCall`、`select`、`datetime`、`timedelta`、`timezone`、
`ConversationMode` 都是檔案裡既有的):

```python
# --- 預約 agent ---------------------------------------------------------------

from datetime import date  # noqa: E402

from app import booking  # noqa: E402
from app.models import Booking  # noqa: E402

BOOKING = {"label": "訂位", "days": list("一二三四五六日"),
           "times": ["18:00", "19:00", "20:00"], "capacity": 5, "max_party": 6,
           "advance_days": 14, "fields": []}


@pytest.fixture
def bookable():
    """acme 開放預約,回傳三天後的日期(避開「今天的時段已經過了」)。"""
    with session_scope() as db:
        c = db.scalar(select(Company).where(Company.slug == "acme"))
        c.booking_config = BOOKING
        c.contact = "02-2345-6789"
    return (booking.today_taipei(datetime.now(timezone.utc)) + timedelta(days=3)).isoformat()


def _llm_seq(monkeypatch, *results):
    """依序回傳預先寫好的模型回應(可以是 LLMError);記下每次的 messages 與 tools。"""
    calls = []
    queue = list(results)

    def fake(messages, **kw):
        calls.append({"messages": list(messages), **kw})
        r = queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr("app.routers.webhook.complete", fake)
    return calls


def _tool_calls(*calls):
    return LLMResult("", token_count=1, latency_ms=1, tool_call=calls[0], tool_calls=tuple(calls))


def _args(day, **over):
    return {"date": day, "time": "20:00", "party_size": 4, "name": "王小明",
            "phone": "0912-345-678", **over}


def _expected_confirmation(day, time, n):
    d = date.fromisoformat(day)
    return f"已為您訂位 {d.month:02d}/{d.day:02d}({'一二三四五六日'[d.weekday()]}){time},{n} 位,訂位人王小明。"


def test_a_booking_conversation_saves_the_booking_and_sends_the_models_answer(
        bookable, line_out, monkeypatch):
    day = bookable
    calls = _llm_seq(monkeypatch,
                     _tool_calls(ToolCall("check_availability", {"date": day, "party_size": 4}, id="c1")),
                     _tool_calls(ToolCall("create_booking", _args(day), id="c2")),
                     LLMResult("已為您訂好", token_count=1, latency_ms=1))
    with TestClient(app) as client:
        _post(client, _body(text="週五晚上 8 點 4 位,王小明 0912-345-678"))
    assert _replies(line_out) == ["已為您訂好"]
    with session_scope() as db:
        (row,) = db.scalars(select(Booking)).all()
        assert (row.booking_time, row.party_size, row.phone) == ("20:00", 4, "0912345678")
        roles = [r.role.value for r in db.scalars(select(ChatHistory).order_by(ChatHistory.id))]
    assert roles == ["user", "assistant"]   # 中間的工具呼叫不進對話紀錄
    assert "日期對照" in calls[0]["messages"][0]["content"]
    assert {t["function"]["name"] for t in calls[0]["tools"]} >= {"check_availability",
                                                                   "create_booking"}


def test_when_the_model_fails_after_booking_only_the_confirmation_is_sent(
        bookable, line_out, monkeypatch):
    """客人要辦的事辦成了 —— 接一句「連線有點問題,可以再問一次嗎」只會讓人以為沒訂到。"""
    day = bookable
    _llm_seq(monkeypatch,
             _tool_calls(ToolCall("create_booking", _args(day, time="19:00", party_size=2), id="c1")),
             LLMError("上游掛了"))
    with TestClient(app) as client:
        _post(client, _body(text="訂位"))
    assert _replies(line_out) == [_expected_confirmation(day, "19:00", 2)]


def test_a_handoff_after_booking_still_tells_the_customer_it_is_booked(
        bookable, rules, line_out, monkeypatch):
    day = bookable
    _llm_seq(monkeypatch,
             _tool_calls(ToolCall("create_booking", _args(day), id="c1")),
             _tool_calls(ToolCall("transfer_to_human", {"category": "safety"}, id="c2")))
    with TestClient(app) as client:
        _post(client, _body(text="訂位,另外我小孩對花生很敏感"))
    assert _replies(line_out) == [_expected_confirmation(day, "20:00", 4) + SAFETY_SCRIPT]
    assert _mode()[0] == ConversationMode.HUMAN
    assert any("【新訂位】" in p[2] for p in _pushes(line_out))


def test_the_staff_is_told_about_a_new_booking(bookable, line_out, monkeypatch):
    day = bookable
    with session_scope() as db:
        db.scalar(select(Company).where(Company.slug == "acme")).staff_notify_to = "Ustaff"
    _llm_seq(monkeypatch,
             _tool_calls(ToolCall("create_booking", _args(day), id="c1")),
             LLMResult("好的", token_count=1, latency_ms=1))
    with TestClient(app) as client:
        _post(client, _body(text="訂位"))
    d = date.fromisoformat(day)
    assert _pushes(line_out) == [("push", "Ustaff",
                                  f"【新訂位】{d.month:02d}/{d.day:02d}({'一二三四五六日'[d.weekday()]})"
                                  f"20:00 4 位 王小明 0912345678")]


def test_an_unknown_tool_gets_an_error_result_and_the_loop_goes_on(
        bookable, line_out, monkeypatch):
    calls = _llm_seq(monkeypatch,
                     _tool_calls(ToolCall("cancel_booking", {}, id="c1")),
                     LLMResult("抱歉,取消請直接來電", token_count=1, latency_ms=1))
    with TestClient(app) as client:
        _post(client, _body(text="我要取消訂位"))
    assert json.loads(calls[1]["messages"][-1]["content"]) == {"ok": False, "error": "沒有這個工具"}
    assert _replies(line_out) == ["抱歉,取消請直接來電"]


def test_without_a_booking_config_there_are_no_booking_tools(rules, line_out, monkeypatch):
    calls = _llm(monkeypatch)
    with TestClient(app) as client:
        _post(client, _body(text="嗨"))
    assert [t["function"]["name"] for t in calls[0]["tools"]] == ["transfer_to_human"]


def test_the_typing_animation_lasts_as_long_as_the_agent_budget(line_out, monkeypatch):
    seconds = []
    monkeypatch.setattr("app.routers.webhook.LineClient.show_loading",
                        lambda self, uid, seconds_=None, **kw: seconds.append(kw.get("seconds", seconds_)) or True)
    _llm(monkeypatch)
    with TestClient(app) as client:
        _post(client, _body(text="嗨"))
    assert seconds == [45]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider tests/test_webhook.py -k "booking or booked or animation"`
Expected: 預約相關的 4 個與未知工具那個 failed(第二次模型回應沒被要求 / 沒有 `Booking` 列 / 回覆不是確認句 /
舊程式把任何工具呼叫都當成轉真人);
`test_the_typing_animation_lasts_as_long_as_the_agent_budget` failed(`[None]` 或 `[20]`,不是 `[45]`);
`test_without_a_booking_config_there_are_no_booking_tools` passed(守既有行為)。

- [ ] **Step 3: 設定**

`app/config.py`,在 `rate_limit_global_per_minute` 之後加:

```python
    # agent loop(設計見 docs/superpowers/specs/2026-10-06-booking-agent-design.md 第六節)。
    # 最後一步不帶工具,模型只能用文字回答。45 秒是整個 loop 合計:LINE reply token
    # 約一分鐘有效,留 15 秒給送出與備援。llm_total_budget_seconds 保留給單次呼叫。
    agent_max_steps: int = Field(4, ge=1)
    agent_total_budget_seconds: float = Field(45.0, gt=0)
```

- [ ] **Step 4: webhook**

`app/routers/webhook.py`:

1. import 區:`import json` 之後加 `import math`;`from app import ratelimit` 改成 `from app import booking, ratelimit`;
   在 `from app.agent.llm import LLMError, complete` 這一行改成 `from app.agent.llm import complete`,並加
   `from app.agent.loop import run_agent`(`LLMError` 這個檔案不再直接用到)。

2. `process_text_event` 的 session 區塊裡,把

```python
                system_prompt = company.system_prompt
                fallback = company.fallback_message
```

改成

```python
                system_prompt = company.system_prompt
                fallback = company.fallback_message
                booking_config = company.booking_config
                contact = company.contact
```

3. 「正在輸入」那一行 `client.show_loading(line_user_id)` 改成
   `client.show_loading(line_user_id, seconds=_loading_seconds(settings.agent_total_budget_seconds))`,
   並把它上面註解裡的「LLM 要跑 3-15 秒」改成「agent loop 可能跑好幾步、最多 45 秒」。

4. 從 `# LLM 呼叫放在 session 外面:不要抓著資料庫連線等十五秒` 開始,到
   `answer = expired_prefix + answer`(含)為止的整段,換成:

```python
        # 模型呼叫放在 session 外面:不要抓著資料庫連線等好幾十秒。預約工具執行時
        # 才各自開一個短的 session。
        tools = [t for t in (build_tool(rules),) if t]
        if booking_config:
            system_prompt += "\n\n" + booking.booking_prompt(
                booking_config, today=booking.today_taipei(now), contact=contact)
            tools += booking.build_tools(booking_config)

        def execute(name: str, arguments: dict) -> dict:
            if booking_config and name == booking.CHECK_TOOL:
                with session_scope() as tdb:
                    return booking.check_availability(
                        tdb, company_id=company_id, config=booking_config,
                        contact=contact, now=datetime.now(timezone.utc),
                        arguments=arguments)
            if booking_config and name == booking.CREATE_TOOL:
                with session_scope() as tdb:
                    return booking.create_booking(
                        tdb, company_id=company_id, user_pk=user_pk,
                        config=booking_config, contact=contact,
                        now=datetime.now(timezone.utc), arguments=arguments)
            return {"ok": False, "error": "沒有這個工具"}

        agent = run_agent(build_messages(system_prompt, history, text),
                          tools=tools or None, execute=execute,
                          max_steps=settings.agent_max_steps,
                          budget_seconds=settings.agent_total_budget_seconds,
                          complete_fn=complete)
        logger.info("agent 步數=%d 工具=%s", agent.steps, ",".join(agent.tools_used) or "無")

        # 預約成立了,但送出去的若不是模型自己的回答(模型失敗、轉真人、安全網),
        # 就由程式補確認句 —— 客人絕不能訂好了卻不知道。
        confirmation = ""
        if agent.booking:
            b = agent.booking
            logger.info("%s成立 客人 …%s %s %s %s 位", booking_config["label"],
                        line_user_id[-6:], b["date"], b["time"], b["party_size"])
            confirmation = booking.confirmation_text(booking_config, b)
            if staff_to:
                _push_staff(client, staff_to, booking.staff_message(booking_config, b))

        # agent 層:模型自己決定要轉。轉真人優先於同一回應裡的其他工具(loop 處理)。
        if agent.handoff is not None:
            _handoff(decision=decision_from_tool(rules, agent.handoff.arguments),
                     prefix=confirmation, **handoff_args)
            return

        if agent.text is None:
            # 模型失敗或超時。已經成立預約的話只送確認句 —— 客人要辦的事辦成了,
            # 再接「連線有點問題,可以再問一次嗎」只會讓人以為沒訂到。
            answer = confirmation or fallback
            tokens, latency = None, None
        else:
            # F1b 安全網:模型沒呼叫工具,卻照 prompt 把 script 整句念出來 ——
            # 客人聽起來像被轉了,實際上沒切 HUMAN、店員也沒收到通知。
            recited = decision_from_recited_script(rules, agent.text)
            if recited is not None:
                _handoff(decision=recited, prefix=confirmation, **handoff_args)
                return
            # 第二道:沒照念 script,而是自己改寫成「我將為您轉接」(2026-09-29 真機)。
            promised = decision_from_promised_transfer(agent.text)
            if promised is not None:
                _handoff(decision=promised, prefix=confirmation, **handoff_args)
                return
            # 只有模型真的回了答案才加:fallback 前面接「專員不在線上」沒有意義
            answer = expired_prefix + agent.text
            tokens, latency = agent.token_count, agent.latency_ms
```

   (原本那段裡寫到 `result` 的變數全部不再需要;後面 `ensure_traditional`、`truncate_for_line`、寫入紀錄、
   `client.send`、`_mark_read` 不變。)

5. `_handoff` 加最後一個參數 `prefix: str = ""`,docstring 加一句「prefix:本次成立的預約確認句,接在轉真人那句話前面。」,
   並把它的 session 區塊與送出改成:

```python
    with session_scope() as db:
        enter_human_mode(db.get(User, user_pk), timeout_minutes=timeout_minutes, now=now)
        if prefix:
            # 確認句自己一列:script 那一列要保持原樣,is_handoff_script 才認得出來
            # (F1a 的歷史標記靠它)
            db.add(ChatHistory(company_id=company_id, user_id=user_pk,
                               role=ChatRole.ASSISTANT, content=prefix))
        db.add(ChatHistory(company_id=company_id, user_id=user_pk,
                           role=ChatRole.ASSISTANT, content=decision.script))
```

   以及 `if not client.send(reply_token, line_user_id, decision.script):` 改成
   `if not client.send(reply_token, line_user_id, prefix + decision.script):`。

6. 在 `_mark_read` 之後加兩個小函式:

```python
def _loading_seconds(budget: float) -> int:
    """LINE 只接受 5 到 60 秒、5 的倍數,不合法直接回 400。"""
    return max(5, min(60, int(math.ceil(budget / 5) * 5)))


def _push_staff(client: LineClient, staff_to: str, message: str) -> None:
    """新預約通知店員。自己包一層:推播失敗不能影響已經成立的預約與給客人的回覆。"""
    try:
        if not client.push(staff_to, message):
            logger.error("新預約的店員通知送不出去(%s)", staff_to)
    except Exception:  # noqa: BLE001
        logger.exception("新預約的店員通知爆炸")
```

- [ ] **Step 5: 跑測試確認通過**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider tests/test_webhook.py`
Expected: 全部 passed —— 含既有的轉真人、安全網、限流、HUMAN、已讀測試(它們的假 `complete` 只填 `tool_call`,
loop 的相容處理會認得;`calls[0]["tools"] is None` 那個測試靠 `tools or None`)。

- [ ] **Step 6: 變異檢查(手動,改完還原)**

先 `cp app/routers/webhook.py "$TEMP/webhook_orig.py"`;逐一套用、各跑一次 `tests/test_webhook.py`;最後 `cp` 回來、`cmp` 確認。

| 改動 | 應該抓到的測試 |
|---|---|
| `answer = confirmation or fallback` → `answer = fallback` | `test_when_the_model_fails_after_booking_only_the_confirmation_is_sent` |
| `_handoff(... prefix=confirmation ...)`(agent.handoff 那個)拿掉 `prefix=` | `test_a_handoff_after_booking_still_tells_the_customer_it_is_booked` |
| 拿掉 `if staff_to: _push_staff(...)` | `test_the_staff_is_told_about_a_new_booking` |
| `if booking_config:`(加 prompt 與工具那段)→ `if False:` | `test_a_booking_conversation_saves_the_booking_and_sends_the_models_answer` |
| `seconds=_loading_seconds(...)` 拿掉 | `test_the_typing_animation_lasts_as_long_as_the_agent_budget` |

- [ ] **Step 7: 跑全套,Commit**

Run: 全套測試指令。Expected: 0 failed。

```bash
git add app/config.py app/routers/webhook.py tests/test_webhook.py
git commit -m "feat(webhook): 呼叫模型改成跑 agent loop,預約成立時保證客人收到確認

有 booking_config 的店才附加預約說明與兩個工具;工具執行時各自開短 session。
預約成立但送出的不是模型回答時由程式補確認句(模型失敗只送確認句;轉真人
與安全網則接在前面),有設定時推播店員。正在輸入動畫跟著 loop 的 45 秒。

Co-Authored-By: <實際執行的模型>"
```

---

### Task 7: 店家查預約的 CLI

**Files:**
- Modify: `app/cli.py`(`run_release` 之後加函式;`main` 加子指令)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `Booking`(Task 2)、`booking.today_taipei`、`booking.WEEKDAYS`(Task 3)
- Produces: `run_bookings(slug: str, *, on: str | None = None) -> list[dict]`;`python -m app.cli bookings --slug <slug> [--date YYYY-MM-DD]`

- [ ] **Step 1: 寫失敗的測試**

`tests/test_cli.py` 檔尾加:

```python
# --- bookings 指令 ------------------------------------------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

from app.booking import today_taipei  # noqa: E402
from app.cli import run_bookings  # noqa: E402
from app.models import Booking  # noqa: E402


@pytest.fixture
def shops():
    mine = run_seed("restaurant", slug="bistro", channel_secret=SECRET, channel_token=TOKEN)
    other = run_seed("clinic", slug="clinic", channel_secret=SECRET, channel_token=TOKEN)
    with session_scope() as db:
        me = User(company_id=mine, line_user_id="U1")
        them = User(company_id=other, line_user_id="U1")
        db.add_all([me, them])
        db.flush()
        return (mine, me.id), (other, them.id)


def _add(shop, days, time="19:00", status="confirmed", name="王小明"):
    company_id, user_id = shop
    with session_scope() as db:
        db.add(Booking(company_id=company_id, user_id=user_id,
                       booking_date=today_taipei(datetime.now(timezone.utc)) + timedelta(days=days),
                       booking_time=time, party_size=2, extra={}, name=name,
                       phone="0912345678", status=status))


def test_bookings_lists_upcoming_confirmed_bookings_in_order(shops):
    mine, other = shops
    _add(mine, -1, name="昨天")
    _add(mine, 0, "20:00", name="今晚八點")
    _add(mine, 0, "18:00", name="今晚六點")
    _add(mine, 3, name="三天後")
    _add(mine, 2, status="cancelled", name="取消的")
    _add(other, 1, name="別家店")
    assert [r["name"] for r in run_bookings("bistro")] == ["今晚六點", "今晚八點", "三天後"]


def test_bookings_on_a_given_date(shops):
    mine, _ = shops
    _add(mine, 0, name="今天")
    _add(mine, 3, name="三天後")
    day = (today_taipei(datetime.now(timezone.utc)) + timedelta(days=3)).isoformat()
    assert [r["name"] for r in run_bookings("bistro", on=day)] == ["三天後"]


def test_main_bookings_prints_each_booking(shops, capsys):
    mine, _ = shops
    _add(mine, 1, name="王小明")
    assert main(["bookings", "--slug", "bistro"]) == 0
    out = capsys.readouterr().out
    assert "王小明" in out and "0912345678" in out and "19:00" in out


def test_bookings_for_an_unknown_slug_exits(shops):
    with pytest.raises(SystemExit):
        run_bookings("nope")


def test_bookings_with_a_malformed_date_exits(shops):
    with pytest.raises(SystemExit):
        run_bookings("bistro", on="10/09")
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider tests/test_cli.py -k "bookings"`
Expected: 收集時 `ImportError: cannot import name 'run_bookings' from 'app.cli'`。

- [ ] **Step 3: 實作**

`app/cli.py`,在 `run_release` 之後加(資料庫相關的 import 照檔案慣例放在函式裡,理由見檔頭註解):

```python
def run_bookings(slug: str, *, on: str | None = None) -> list[dict]:
    """列出某家店的預約:預設今天(台灣)以後;有 on 就只列那一天。只列已成立的,依日期、時間排序。"""
    from datetime import date, datetime, timezone

    from sqlalchemy import select

    from app.booking import WEEKDAYS, today_taipei
    from app.database import session_scope
    from app.models import Booking, Company

    try:
        day = date.fromisoformat(on) if on else None
    except ValueError:
        raise SystemExit(f"日期格式要是 YYYY-MM-DD:「{on}」") from None

    with session_scope() as db:
        company = db.scalar(select(Company).where(Company.slug == slug))
        if company is None:
            raise SystemExit(
                f"找不到 slug「{slug}」。用 `python -m app.cli list` 看有哪些行業。")
        q = select(Booking).where(Booking.company_id == company.id,
                                  Booking.status == "confirmed")
        if day is not None:
            q = q.where(Booking.booking_date == day)
        else:
            q = q.where(Booking.booking_date >= today_taipei(datetime.now(timezone.utc)))
        rows = db.scalars(q.order_by(Booking.booking_date, Booking.booking_time,
                                     Booking.id)).all()
        return [{"date": r.booking_date.isoformat(),
                 "weekday": WEEKDAYS[r.booking_date.weekday()],
                 "time": r.booking_time, "party_size": r.party_size, "name": r.name,
                 "phone": r.phone, "extra": dict(r.extra or {})} for r in rows]


def _report_bookings(rows: list[dict]) -> None:
    if not rows:
        print("沒有預約")
        return
    print(f"共 {len(rows)} 筆")
    for r in rows:
        d = r["date"][5:].replace("-", "/")
        extra = "  ".join(str(v) for v in r["extra"].values())
        print(f"  {d}({r['weekday']}) {r['time']}  {r['party_size']} 位  "
              f"{r['name']}  {r['phone']}" + (f"  {extra}" if extra else ""))
```

`main` 裡,在 `r = sub.add_parser("release")` 那兩行之後加:

```python
    b = sub.add_parser("bookings")
    b.add_argument("--slug", required=True)
    b.add_argument("--date", help="只列這一天(YYYY-MM-DD);不給就列今天以後的全部")
```

並在 `if args.cmd == "release":` 那段之後加:

```python
    if args.cmd == "bookings":
        _report_bookings(run_bookings(args.slug, on=args.date))
        return 0
```

- [ ] **Step 4: 跑測試確認通過,跑全套,Commit**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider tests/test_cli.py`,再跑全套。Expected: 0 failed。

```bash
git add app/cli.py tests/test_cli.py
git commit -m "feat(cli): bookings 指令讓店家查預約

預設列出今天(台灣)以後已成立的預約,--date 只看某一天,依日期時間排序。

Co-Authored-By: <實際執行的模型>"
```

---

### Task 8: 文件

**Files:**
- Modify: `README.md`、`docs/report.md`、`industries/README.md`、`docs/demo-checklist.md`

**Interfaces:** 無

- [ ] **Step 1: 量出實際數字**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider --ignore=tests/test_eval_jev.py 2>&1 | tail -1
git ls-files 'tests/*.py' | xargs cat | wc -l
git ls-files 'app/*.py' 'app/**/*.py' | sort -u | xargs cat | wc -l
```

- [ ] **Step 2: README**

在 `## 三個示範行業` 這一行**之前**插入一節:

```markdown
## agent:模型自己決定下一步

餐廳與診所的資料夾多一份選填的 `booking.yaml`(時段、名額、要問的欄位),bot 就會幫客人預約。
模型手上有「查空位」「建立預約」與「轉真人」三個工具,**自己決定**呼叫哪一個、看了結果再決定下一步:

> **客人**:我要訂這週五晚上 8 點,4 位,王小明 0912345678
> **bot**:已為您保留 10/09(五)20:00,4 位,訂位人王小明。逾時 15 分鐘未到,座位將不予保留。

```
客人訊息 → 模型呼叫【查空位】→ 程式回傳各時段 → 模型看到 20:00 有位,呼叫【建立預約】
        → 程式逐項檢查並寫入 → 模型組出確認句
```

- 迴圈是自己寫的(`app/agent/loop.py`):最多 4 步、最後一步不帶工具;整個 loop 45 秒
- 所有檢查在程式裡(`app/booking.py`):日期範圍、公休、已過的時段、人數、電話、名額、防重複 —— 模型是會出錯的那一方
- 日期由程式給對照表:實測讓模型自己從「今天星期幾」推算「週五」會算錯
- 預約成立但送出的不是模型的回答(模型失敗、轉真人)時,程式補一句確認 —— 客人絕不會訂好了卻不知道
- 電商沒有 `booking.yaml`,就沒有預約工具 —— 一樣不改程式碼
- 店家查預約:`python -m app.cli bookings --slug bistro`
```

「狀態」一節:在速率限制那一段之後加一段:

```markdown
預約 agent:餐廳與診所可以在 LINE 上直接預約,模型自己跑「查空位 → 追問 → 建立預約」;
有名額就直接成立,名額以「組」計。實測(NVIDIA super):一句話講完 2/2、指定已滿的時段 3/3 都沒有硬訂。
```

同一節的測試數字換成 Step 1 量到的。

「下一步」表格刪掉 `**Tool Calling**` 那一列,改加一列:

```markdown
| **取消與修改預約** | 目前只能新增(請客人來電取消)。`bookings.status` 已預留;要先決定客人怎麼證明「這是我的預約」 |
```

- [ ] **Step 3: report**

第六節「自動測試」第一行的測試數與行數換成 Step 1 量到的(格式照舊)。

第七節在 `### 11. 本機測試全綠,正式映像跑的卻是另一組版本` 那一段之後加:

```markdown
### 12. 模型自己算日期會算錯

做預約 agent 前先拿 NVIDIA super 實測多步驟工具呼叫(假工具,13 段對話)。「一句話講完」2/2、
「指定已滿的時段」3/3 都正確,但 prompt 只寫「今天是 2026-10-06(星期二)」時,模型一度把 10/10 當成週五
(週五是 10/09)。另外 3 次卡在反覆追問「本週五還是下週五」—— 原因是 prompt 自己寫了「週五指的是本週
或下一個星期五」,等於製造歧義。

現在每次請求都由程式附上未來兩週的日期對照表(標出公休日),並明確規定「週五」就是從今天起算最近的那一天、
不要反問。**弱模型要靠程式兜底**,跟簡繁轉換是同一個道理。建立預約時程式也會再檢查一次日期範圍與公休,
模型算錯也訂不進去。
```

第八節限制 7(`7. **只有一個工具。**` 開頭那一項)整項換成:

```markdown
7. **預約只能新增。** 取消、修改要客人來電;名額以「組」計,不處理桌位。工具過程不存進對話紀錄 ——
   下一則訊息時模型看不到上一則的查詢結果(建立時程式會再查一次,所以不會超訂)。
```

並在第八節「會慢慢變成問題的」那一組的最後加一項(編號接續):

```markdown
- **客人的姓名、電話會隨對話送到模型供應商。** NVIDIA 試用條款寫明會收集輸入內容 —— demo 可以,
  接真實店家前要換成不收集資料的方案。
```

第九節表格第 4 列 `**更多工具**` 換成:

```markdown
| 4 | **更多工具** | 預約已完成(查空位、建立預約,agent loop 最多 4 步)。下一個是取消與修改預約,以及電商的查訂單 |
```

- [ ] **Step 4: `industries/README.md`**

「每個行業裡面是什麼」的表格最後加一列:

```markdown
| `booking.yaml`(選填) | 預約:時段、每個時段幾組、每筆最多幾人、最遠訂到幾天後、要問的額外欄位 | 格式錯誤 → 規則 11 擋下;時段落在營業時間外 → 規則 12 列為 BLOCK |
```

並把該節第一句「五份 YAML,結構完全一樣:」改成「五份必填的 YAML,結構完全一樣;`booking.yaml` 選填:」。

- [ ] **Step 5: `docs/demo-checklist.md`**

「現場要示範的五個步驟」標題改成「現場要示範的六個步驟」,表格最後加一列:

```markdown
| 6 | `release --slug bistro` 後傳「我要訂這週五晚上 8 點,4 位,王小明 0912345678」 | 模型先查空位再建立預約,回確認句;`docker compose exec app python -m app.cli bookings --slug bistro` 列得出那筆;log 有 `agent 步數=3` |
```

- [ ] **Step 6: Commit**

```bash
git add README.md docs/report.md industries/README.md docs/demo-checklist.md
git commit -m "docs: 預約 agent 寫進 README、報告與 demo 清單,數字同步

Co-Authored-By: <實際執行的模型>"
```

---

### Task 9: 真模型驗收、PR、部署、手機驗收(controller 執行)

**Files:** 無(操作步驟)

- [ ] **Step 1: 真模型驗收(不進 repo、不進 CI)**

在 scratchpad 寫一次性腳本:對一個暫存 SQLite(`DATABASE_URL=sqlite:///<scratch>/accept.db`、`create_all`)建一家餐廳公司
(`booking_config` = 真的 `industries/restaurant/booking.yaml`)與一位客人,用**正式的** `build_messages`、
`booking.booking_prompt`、`booking.build_tools`、`run_agent(complete_fn=complete)`,`execute` 綁 `booking.check_availability` /
`create_booking`,跑四個情境各 3 次:分四句(「我想訂位」→「週五晚上 4 個人」→「8 點」→「王小明 0912-345-678」,
每句之間把 user / 最後文字加進 history)、一句講完、指定已滿的時段(先塞滿該時段 5 筆)、診所(`booking_config` 換成
clinic 的,「我想預約週五早上看青春痘,王小明 0912345678」)。

通過標準:建立的預約日期全部是「從今天起算最近的週五」、已滿時段沒有建立、「分四句」至少 2/3 完成、診所的 `extra` 是 `{"service": "青春痘"}`。
不通過就回頭調 `booking_prompt` 的措辭(另開 fix commit,照 TDD 改 `test_booking.py` 的 prompt 斷言)。

- [ ] **Step 2: 推分支、開 PR**

`git push -u origin feat/booking-agent`,`gh pr create --base master`,描述照 #4、#5 的格式(摘要 / 改了什麼 / 驗證 / 已知限制),
附上真模型驗收的數字,結尾 `🤖 Generated with [Claude Code](https://claude.com/claude-code)`。用 ccd_pr `get_status` 確認綁定與 CI。

- [ ] **Step 3: 等使用者合併**

CI 三個 job 都綠後請使用者按 Merge。合併後本機:
`git stash push -- .env.example && git checkout master && git pull origin master && git stash pop`。

- [ ] **Step 4: 部署(先問過使用者)**

```bash
ssh -i ~/.ssh/helpdesk_gcp helpdesk@136.70.19.152 'cd ~/universal-ai-helpdesk && git pull && docker compose build app && docker compose up -d app'
ssh -i ~/.ssh/helpdesk_gcp helpdesk@136.70.19.152 'cd ~/universal-ai-helpdesk && docker compose exec -T app python -m app.cli seed --industry restaurant --slug bistro && docker compose exec -T app python -m app.cli release --slug bistro'
```

確認:app `healthy`、`alembic_version` = `c5e8a1f3d920`、`companies.booking_config` 有值、公開網址 `/health` 200。

- [ ] **Step 5: 手機驗收(請使用者操作)**

1. 傳「我要訂這週五晚上 8 點,4 位,王小明 0912345678」→ 收到確認句(日期是最近的週五)
2. `docker compose exec -T app python -m app.cli bookings --slug bistro` 列得出那筆
3. log 有 `agent 步數=` 與 `訂位成立 客人 …`:`docker compose logs app --since 5m | grep -E "agent 步數|成立"`
