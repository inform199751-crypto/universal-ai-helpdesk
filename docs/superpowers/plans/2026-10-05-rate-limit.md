# 速率限制 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 webhook 呼叫模型之前加上三條速率限制(每人每分鐘、每人每天、全站每分鐘),計數直接從 `chat_histories` 算。

**Architecture:** 新模組 `app/ratelimit.py` 是一個純判定函式 `check()`,用同一個 session 對 `chat_histories` 下三個 COUNT;webhook 在關鍵字層之後、「正在輸入」之前呼叫它,依結果放行、回提醒、安靜或回忙碌訊息。`companies` 新增 `contact` 欄位供提醒文字使用,`chat_histories` 加 `created_at` 索引給全站計數。

**Tech Stack:** FastAPI、SQLAlchemy 2、Alembic、pydantic-settings、pytest(SQLite 記憶體庫;CI 另跑 PostgreSQL)

**Spec:** `docs/superpowers/specs/2026-10-05-rate-limit-design.md`

## Global Constraints

- 測試指令(Windows、專案根目錄 `C:\Users\User\Desktop\universal-ai-helpdesk`):
  `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider --ignore=tests/test_eval_jev.py`
- 程式碼要同時在 SQLite 與 PostgreSQL 上正確;CI 在 Python 3.11 與 3.13 各跑一輪。
- 預設上限:每人每分鐘 **5**、每人每天 **20**、全站每分鐘 **30**;`0` = 關閉該條;負數由設定驗證拒絕。
- 「一天」= 台灣時間午夜,固定 **UTC+8**(`timezone(timedelta(hours=8))`),不用 zoneinfo。
- 回覆文字逐字照抄(半形逗號與分號,跟專案其他回覆一致):
  - 每人每分鐘:`您傳得有點快,我跟不上了 🙏 麻煩等一分鐘,再把想問的事傳一次給我。`
  - 每人每天:`今天跟我聊的次數已經到上限了,明天再來問我;急的話可以直接聯繫 {contact}。`
  - 全站忙碌:`目前詢問的人比較多,請過幾分鐘再傳一次;急的話可以直接聯繫 {contact}。`
  - contact 為空時去掉「;急的話可以直接聯繫 {contact}」,句尾是「。」
- 關鍵字轉真人與非文字固定回覆**不限流**;限流檢查失敗時**放行**。
- **不要碰** `scripts/eval_jev*.py`、`scripts/eval_jev_cases.yaml`、`tests/test_eval_jev.py`,也不要 commit `.env.example` 裡的 Jev 段落(`TYPESAFE_API_KEY`)—— Task 3 有專用的暫存指令。
- commit 訊息用中文、`type(scope): 摘要` 格式,最後一行 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。
- 分支 `feat/rate-limit`。**合併 PR 由使用者按**,不要自己 merge。

## 檔案結構

| 檔案 | 責任 |
|---|---|
| `app/ratelimit.py`(新) | `Action`、`Limits`、`Verdict`、`check()`、`reply_text()`、`taipei_midnight_utc()` —— 判定與文字,不碰 LINE |
| `app/routers/webhook.py` | 呼叫 `check()`,依結果送出 / 寫紀錄 / 標已讀 |
| `app/config.py` | 三個上限設定 |
| `app/models/company.py`、`app/models/chat.py` | `contact` 欄位、`ix_chat_created` 索引 |
| `alembic/versions/7b1e4c2a9d30_contact_and_chat_created_index.py`(新) | 上面兩者的遷移 |
| `app/cli.py` | seed 寫入 contact |
| `compose.yaml`、`.env.example` | 把三個設定傳進容器 / 說明 |
| `tests/test_ratelimit.py`(新) | 判定規則 |

---

### Task 1: `companies.contact`、`ix_chat_created` 與 seed

**Files:**
- Modify: `app/models/company.py`(`fallback_message` 欄位之後)
- Modify: `app/models/chat.py`(`__table_args__`)
- Create: `alembic/versions/7b1e4c2a9d30_contact_and_chat_created_index.py`
- Modify: `app/cli.py`(`run_seed` 裡 `company.vector_collection = ...` 那一行之後)
- Test: `tests/test_alembic.py`、`tests/test_cli.py`

**Interfaces:**
- Produces: `Company.contact: str | None`(Task 3 讀它組文字);索引 `ix_chat_created`。

- [ ] **Step 1: 寫失敗的測試**

`tests/test_alembic.py` 檔尾加:

```python
ESCALATION = "3f2a9c1d7b4e"


def _indexes(url: str, table: str) -> set[str]:
    engine = create_engine(url)
    try:
        return {i["name"] for i in inspect(engine).get_indexes(table)}
    finally:
        engine.dispose()


def test_upgrade_adds_contact_and_the_created_at_index(alembic_sqlite):
    """全站每分鐘的 COUNT 只看 created_at;沒有索引就是每則訊息掃一次整張表。"""
    cfg, url = alembic_sqlite
    command.upgrade(cfg, "head")
    assert "contact" in _columns(url)
    assert "ix_chat_created" in _indexes(url, "chat_histories")


def test_downgrade_removes_contact_and_the_created_at_index(alembic_sqlite):
    cfg, url = alembic_sqlite
    command.upgrade(cfg, "head")
    command.downgrade(cfg, ESCALATION)
    assert "contact" not in _columns(url)
    assert "ix_chat_created" not in _indexes(url, "chat_histories")
```

`tests/test_cli.py` 檔尾加:

```python
def test_seed_stores_the_contact_from_company_yaml():
    """提醒文字的「急的話可以直接聯繫 …」靠它;沒存的話那半句永遠不會出現。"""
    cid = run_seed("restaurant", slug="bistro", channel_secret=SECRET, channel_token=TOKEN)
    with session_scope() as db:
        assert db.get(Company, cid).contact == "02-2345-6789"


def test_switching_industry_rewrites_the_contact():
    cid = run_seed("restaurant", slug="bistro", channel_secret=SECRET, channel_token=TOKEN)
    run_seed("ecommerce", slug="bistro")
    with session_scope() as db:
        assert db.get(Company, cid).contact == "service@example.com"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider tests/test_alembic.py tests/test_cli.py -k "contact"`
Expected: 4 failed —— alembic 的兩個是 `assert 'contact' in {...}` 失敗;cli 的兩個是 `AttributeError: 'Company' object has no attribute 'contact'`。

- [ ] **Step 3: 實作**

`app/models/company.py`,在 `fallback_message` 欄位定義之後加:

```python
    # 店家聯絡方式(company.yaml 的 contact,必填)。原本只被組進 system_prompt;
    # 限流的提醒文字要單獨用它 ——「急的話可以直接聯繫 …」。電商的是 email,
    # 所以文字寫「聯繫」不寫「來電」。可為空:遷移前的資料列要重跑 seed 才有值。
    contact: Mapped[str | None] = mapped_column(String(200))
```

`app/models/chat.py` 的 `__table_args__`,在 `Index("ix_chat_user_created", ...)` 之後加一行:

```python
        # 限流的全站每分鐘計數:只看 created_at、跨所有公司
        Index("ix_chat_created", "created_at"),
```

新檔 `alembic/versions/7b1e4c2a9d30_contact_and_chat_created_index.py`:

```python
"""contact and chat_histories created_at index

Revision ID: 7b1e4c2a9d30
Revises: 3f2a9c1d7b4e
Create Date: 2026-10-05 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7b1e4c2a9d30'
down_revision: Union[str, Sequence[str], None] = '3f2a9c1d7b4e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 可為空,不需要 server_default:既有資料列就是 NULL,重跑 seed 才填上。
    # batch_alter_table:SQLite 的 DROP COLUMN 要靠 batch 模式重建表(downgrade 用得到)。
    with op.batch_alter_table("companies") as batch:
        batch.add_column(sa.Column("contact", sa.String(length=200), nullable=True))
    op.create_index("ix_chat_created", "chat_histories", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_chat_created", table_name="chat_histories")
    with op.batch_alter_table("companies") as batch:
        batch.drop_column("contact")
```

`app/cli.py` 的 `run_seed`,在 `company.vector_collection = f"kb_{slug}"` 之後加:

```python
        company.contact = data["company"]["contact"]
```

- [ ] **Step 4: 跑測試確認通過**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider tests/test_alembic.py tests/test_cli.py`
Expected: 全部 passed(含既有的 `test_upgrade_keeps_existing_companies_and_defaults_the_rules_to_empty` —— 它現在也會跑過新遷移,證明對「已有資料列」的表加欄位沒問題)。

- [ ] **Step 5: 跑全套確認沒有打壞別的**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider --ignore=tests/test_eval_jev.py`
Expected: `287 passed, 1 skipped`(原本 283 + 新的 4 個)。

- [ ] **Step 6: Commit**

```bash
git add app/models/company.py app/models/chat.py app/cli.py alembic/versions/7b1e4c2a9d30_contact_and_chat_created_index.py tests/test_alembic.py tests/test_cli.py
git commit -m "feat(db): companies.contact 與 chat_histories 的 created_at 索引

限流的提醒文字要附店家聯絡方式,而 contact 原本只被組進 system_prompt;
全站每分鐘的計數只看 created_at,需要自己的索引。seed 寫入 contact。

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: 判定模組 `app/ratelimit.py` 與設定

**Files:**
- Create: `app/ratelimit.py`
- Modify: `app/config.py`(`history_limit` 那一段附近)
- Create: `tests/test_ratelimit.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Consumes: `ChatHistory`、`ChatRole`、`Company`、`User`(既有);`ix_chat_created`(Task 1)
- Produces(Task 3 使用):
  - `class Action(enum.Enum)`:`ALLOW`、`NOTIFY`、`SILENT`、`BUSY`
  - `@dataclass(frozen=True) class Limits(user_per_minute: int, user_per_day: int, global_per_minute: int)`,含 `Limits.from_settings(s) -> Limits`
  - `@dataclass(frozen=True) class Verdict(action: Action, rule: str | None = None, count: int = 0)`;`rule` 是 `"user_minute"` / `"user_day"` / `"global_minute"`
  - `check(db: Session, *, user_pk: str, now: datetime, limits: Limits) -> Verdict`
  - `reply_text(verdict: Verdict, contact: str | None) -> str | None`(`None` = 不回覆)
  - `MINUTE_NOTICE: str`
  - Settings 欄位:`rate_limit_user_per_minute`、`rate_limit_user_per_day`、`rate_limit_global_per_minute`

- [ ] **Step 1: 寫失敗的測試**

新檔 `tests/test_ratelimit.py`:

```python
"""速率限制的判定規則。設計見 docs/superpowers/specs/2026-10-05-rate-limit-design.md。

用真的資料庫、插入指定 created_at 的列 —— 規則本身就是三個 COUNT 查詢,
mock 掉資料庫就什麼都沒驗到。
"""

import logging
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.database import Base, engine, session_scope
from app.models import ChatHistory, ChatRole, Company, User
from app.ratelimit import (MINUTE_NOTICE, Action, Limits, Verdict, check,
                           reply_text)

NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)  # 台灣 20:00
LIMITS = Limits(user_per_minute=5, user_per_day=20, global_per_minute=30)


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


def _company(slug="acme"):
    with session_scope() as db:
        c = Company(slug=slug, name=slug, industry="restaurant",
                    vector_collection=f"kb_{slug}",
                    line_channel_secret_enc=b"x", line_channel_token_enc=b"x")
        db.add(c)
        db.flush()
        return c.id


def _user(company_id, line_user_id="U1"):
    with session_scope() as db:
        u = User(company_id=company_id, line_user_id=line_user_id)
        db.add(u)
        db.flush()
        return u.id


def _rows(company_id, user_pk, n, *, role=ChatRole.USER, at=NOW):
    with session_scope() as db:
        for _ in range(n):
            db.add(ChatHistory(company_id=company_id, user_id=user_pk, role=role,
                               content="x", created_at=at))


def _check(user_pk, *, now=NOW, limits=LIMITS):
    with session_scope() as db:
        return check(db, user_pk=user_pk, now=now, limits=limits)


@pytest.fixture
def me():
    cid = _company()
    return cid, _user(cid)


# --- 每人每分鐘 ---------------------------------------------------------------

@pytest.mark.parametrize("n, expected", [
    (5, Verdict(Action.ALLOW)),
    (6, Verdict(Action.NOTIFY, "user_minute", 6)),
    (7, Verdict(Action.SILENT, "user_minute", 7)),
])
def test_the_minute_limit_notifies_once_then_goes_silent(me, n, expected):
    cid, uid = me
    _rows(cid, uid, n, at=NOW - timedelta(seconds=10))
    assert _check(uid) == expected


def test_messages_older_than_a_minute_do_not_count(me):
    cid, uid = me
    _rows(cid, uid, 10, at=NOW - timedelta(seconds=61))
    _rows(cid, uid, 1)
    assert _check(uid) == Verdict(Action.ALLOW)


def test_non_text_messages_count_toward_the_minute_limit(me):
    """貼圖洗版也是洗版:user 列不分訊息型別,內容是「[貼圖]」照樣算。"""
    cid, uid = me
    with session_scope() as db:
        for _ in range(6):
            db.add(ChatHistory(company_id=cid, user_id=uid, role=ChatRole.USER,
                               content="[貼圖]", created_at=NOW))
    assert _check(uid).action is Action.NOTIFY


# --- 每人每天 -----------------------------------------------------------------

@pytest.mark.parametrize("n, expected", [
    (19, Verdict(Action.ALLOW)),
    (20, Verdict(Action.NOTIFY, "user_day", 20)),
    (21, Verdict(Action.SILENT, "user_day", 21)),
])
def test_the_daily_limit_counts_ai_replies(me, n, expected):
    cid, uid = me
    _rows(cid, uid, n, role=ChatRole.ASSISTANT, at=NOW - timedelta(hours=1))
    assert _check(uid) == expected


def test_the_day_starts_at_taipei_midnight(me):
    """台灣 10/6 00:30 = UTC 10/5 16:30。UTC 15:59 是台灣前一天 23:59,不算;
    UTC 16:01 是台灣當天 00:01,算。用 UTC 午夜切的話兩批都會算進去。"""
    cid, uid = me
    now = datetime(2026, 10, 5, 16, 30, tzinfo=timezone.utc)
    _rows(cid, uid, 20, role=ChatRole.ASSISTANT,
          at=datetime(2026, 10, 5, 15, 59, tzinfo=timezone.utc))
    assert _check(uid, now=now) == Verdict(Action.ALLOW)
    _rows(cid, uid, 20, role=ChatRole.ASSISTANT,
          at=datetime(2026, 10, 5, 16, 1, tzinfo=timezone.utc))
    assert _check(uid, now=now) == Verdict(Action.NOTIFY, "user_day", 20)


def test_customer_messages_do_not_count_toward_the_daily_limit(me):
    """轉真人期間跟店員來回 30 則的客人,交還 AI 後不該被擋。"""
    cid, uid = me
    _rows(cid, uid, 30, role=ChatRole.USER, at=NOW - timedelta(hours=2))
    assert _check(uid) == Verdict(Action.ALLOW)


# --- 誰的列算誰的 -------------------------------------------------------------

def test_other_customers_rows_do_not_count(me):
    cid, uid = me
    other_company = _company("other")
    for owner_cid, owner in ((cid, _user(cid, "U2")),
                             (other_company, _user(other_company, "U1"))):
        _rows(owner_cid, owner, 4)
        _rows(owner_cid, owner, 25, role=ChatRole.ASSISTANT)
    assert _check(uid) == Verdict(Action.ALLOW)


# --- 全站每分鐘 ---------------------------------------------------------------

def _crowd(n_users, each, company_ids):
    """n_users 位客人(輪流分到各公司),每人 each 則 —— 每人都在個人上限以內。"""
    for i in range(n_users):
        cid = company_ids[i % len(company_ids)]
        _rows(cid, _user(cid, f"F{i}"), each)


def test_the_global_limit_counts_every_company(me):
    cid, uid = me
    other = _company("other")
    _crowd(10, 3, [cid, other])                       # 30 則
    assert _check(uid) == Verdict(Action.ALLOW)
    _rows(cid, uid, 1)                                # 第 31 則
    assert _check(uid) == Verdict(Action.BUSY, "global_minute", 31)


def test_a_customer_over_their_own_limit_is_not_reported_as_global_busy(me):
    cid, uid = me
    _crowd(10, 3, [cid])
    _rows(cid, uid, 6)
    assert _check(uid) == Verdict(Action.NOTIFY, "user_minute", 6)


# --- 關閉與失敗 ---------------------------------------------------------------

def test_a_zero_limit_turns_that_rule_off(me):
    """三條同時超過;任何一條沒把 0 當成關閉,這裡就不會是 ALLOW。"""
    cid, uid = me
    _crowd(10, 4, [cid])
    _rows(cid, uid, 10)
    _rows(cid, uid, 30, role=ChatRole.ASSISTANT)
    assert _check(uid, limits=Limits(0, 0, 0)) == Verdict(Action.ALLOW)


def test_a_failing_count_lets_the_message_through(me, monkeypatch, caplog):
    """限流是保護,不是主流程:查不了就放行,不讓正常客人收不到回答。"""
    def boom(*a, **kw):
        raise RuntimeError("db down")
    monkeypatch.setattr("app.ratelimit._count", boom)
    caplog.set_level(logging.WARNING, logger="app.ratelimit")
    assert _check(me[1]) == Verdict(Action.ALLOW)
    assert any("限流檢查失敗" in r.message for r in caplog.records)


# --- 設定 → Limits ------------------------------------------------------------

def test_limits_are_read_from_the_matching_settings():
    s = Settings(_env_file=None, fernet_key="k", openrouter_api_key="k",
                 rate_limit_user_per_minute=1, rate_limit_user_per_day=2,
                 rate_limit_global_per_minute=3)
    assert Limits.from_settings(s) == Limits(user_per_minute=1, user_per_day=2,
                                             global_per_minute=3)


# --- 文字 ---------------------------------------------------------------------

def test_the_minute_notice_asks_to_resend_and_has_no_contact():
    """不寫「我稍等一下再回您」:bot 不會事後補答,那是做不到的承諾。"""
    v = Verdict(Action.NOTIFY, "user_minute", 6)
    assert reply_text(v, "02-2345-6789") == MINUTE_NOTICE
    assert MINUTE_NOTICE == "您傳得有點快,我跟不上了 🙏 麻煩等一分鐘,再把想問的事傳一次給我。"


def test_the_daily_notice_includes_the_contact():
    v = Verdict(Action.NOTIFY, "user_day", 20)
    assert reply_text(v, "02-2345-6789") == (
        "今天跟我聊的次數已經到上限了,明天再來問我;急的話可以直接聯繫 02-2345-6789。")


def test_busy_includes_the_contact():
    v = Verdict(Action.BUSY, "global_minute", 31)
    assert reply_text(v, "service@example.com") == (
        "目前詢問的人比較多,請過幾分鐘再傳一次;急的話可以直接聯繫 service@example.com。")


@pytest.mark.parametrize("contact", [None, "", "   "])
def test_without_a_contact_the_clause_is_dropped(contact):
    assert reply_text(Verdict(Action.BUSY, "global_minute", 31), contact) == (
        "目前詢問的人比較多,請過幾分鐘再傳一次。")
    assert reply_text(Verdict(Action.NOTIFY, "user_day", 20), contact) == (
        "今天跟我聊的次數已經到上限了,明天再來問我。")


@pytest.mark.parametrize("v", [Verdict(Action.SILENT, "user_minute", 7),
                               Verdict(Action.ALLOW)])
def test_silent_and_allow_have_no_reply(v):
    assert reply_text(v, "02-2345-6789") is None
```

`tests/test_config.py` 檔尾加(檔頭補 `import pytest` 與 `from pydantic import ValidationError`):

```python
@pytest.mark.parametrize("field", ["rate_limit_user_per_minute", "rate_limit_user_per_day",
                                   "rate_limit_global_per_minute"])
def test_a_negative_rate_limit_is_rejected(field):
    """0 是「關閉」,負數沒有意義 —— 寫錯的設定要在開機時就爆,不是默默變成關閉。"""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, fernet_key="k", openrouter_api_key="k", **{field: -1})
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider tests/test_ratelimit.py tests/test_config.py`
Expected: `tests/test_ratelimit.py` 收集時 `ModuleNotFoundError: No module named 'app.ratelimit'`;`test_a_negative_rate_limit_is_rejected` 三個 failed(`DID NOT RAISE`)。

- [ ] **Step 3: 實作設定**

`app/config.py`:檔頭 import 改成 `from pydantic import Field, field_validator`,並在 `history_limit` / `line_max_text_length` 那段之後加:

```python
    # 速率限制(設計見 docs/superpowers/specs/2026-10-05-rate-limit-design.md)。
    # 0 = 關閉那一條。全站每分鐘 30 留 10 的餘裕在 NVIDIA 免費層的每分鐘約 40 次以下 ——
    # 超過的話 NVIDIA 回 429,全部退到 OpenRouter,一天 50 次幾分鐘就燒光。
    rate_limit_user_per_minute: int = Field(5, ge=0)
    rate_limit_user_per_day: int = Field(20, ge=0)
    rate_limit_global_per_minute: int = Field(30, ge=0)
```

- [ ] **Step 4: 實作 `app/ratelimit.py`**

```python
"""速率限制。設計見 docs/superpowers/specs/2026-10-05-rate-limit-design.md。

計數直接從 chat_histories 算,不另外存狀態:計數跟對話紀錄是同一份資料,
部署與重啟不歸零,多 worker 也對。三條規則依序檢查,第一條不放行的就是結果 ——
個人規則在前,狂傳的人被擋的是他自己,不佔全站的名額。

「只提醒一次」也不另外記:每分鐘是「剛好第 m+1 則」才提醒;每天是提醒本身也
寫進 chat_histories(assistant),計數從 d 變 d+1,之後自然進入安靜。
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ChatHistory, ChatRole

logger = logging.getLogger(__name__)

# 台灣沒有日光節約,固定 UTC+8 就對,不必依賴容器裡有沒有時區資料庫
TAIPEI = timezone(timedelta(hours=8))
WINDOW = timedelta(seconds=60)

# 不寫「我稍等一下再回您」:被擋掉的訊息不會被補答,那是做不到的承諾
MINUTE_NOTICE = "您傳得有點快,我跟不上了 🙏 麻煩等一分鐘,再把想問的事傳一次給我。"
_DAY_NOTICE = "今天跟我聊的次數已經到上限了,明天再來問我"
_BUSY = "目前詢問的人比較多,請過幾分鐘再傳一次"


class Action(enum.Enum):
    ALLOW = "allow"
    NOTIFY = "notify"    # 剛好踩到個人上限:回一次提醒
    SILENT = "silent"    # 已經提醒過:不回
    BUSY = "busy"        # 全站這一分鐘太多:每則都回忙碌訊息


@dataclass(frozen=True)
class Limits:
    user_per_minute: int
    user_per_day: int
    global_per_minute: int

    @classmethod
    def from_settings(cls, s) -> Limits:
        return cls(user_per_minute=s.rate_limit_user_per_minute,
                   user_per_day=s.rate_limit_user_per_day,
                   global_per_minute=s.rate_limit_global_per_minute)


@dataclass(frozen=True)
class Verdict:
    action: Action
    rule: str | None = None   # "user_minute" / "user_day" / "global_minute"
    count: int = 0


_ALLOW = Verdict(Action.ALLOW)


def taipei_midnight_utc(now: datetime) -> datetime:
    """台灣今天 00:00,換回 UTC(資料庫存的是 UTC)。"""
    local = now.astimezone(TAIPEI)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def _count(db: Session, *conditions) -> int:
    return db.scalar(select(func.count()).select_from(ChatHistory).where(*conditions))


def check(db: Session, *, user_pk: str, now: datetime, limits: Limits) -> Verdict:
    """判定這一則要不要放行。「這一則」必須已經寫進 chat_histories(同一個 session)。

    任何例外都放行:限流是保護機制,不是主流程 —— 為了它讓正常客人收不到回答,
    違反「沉默是唯一不被接受的失敗模式」。
    """
    try:
        return _check(db, user_pk=user_pk, now=now, limits=limits)
    except Exception:  # noqa: BLE001
        logger.warning("限流檢查失敗,放行", exc_info=True)
        return _ALLOW


def _check(db: Session, *, user_pk: str, now: datetime, limits: Limits) -> Verdict:
    since = now - WINDOW

    m = limits.user_per_minute
    if m:
        # 所有訊息型別都算 —— 貼圖洗版也是洗版
        n = _count(db, ChatHistory.user_id == user_pk,
                   ChatHistory.role == ChatRole.USER,
                   ChatHistory.created_at >= since)
        if n > m:
            return Verdict(Action.NOTIFY if n == m + 1 else Action.SILENT, "user_minute", n)

    d = limits.user_per_day
    if d:
        # 算 AI 回了幾則,不是客人傳了幾則:轉真人期間跟店員來回很多則的客人,
        # 交還 AI 後不該被擋
        n = _count(db, ChatHistory.user_id == user_pk,
                   ChatHistory.role == ChatRole.ASSISTANT,
                   ChatHistory.created_at >= taipei_midnight_utc(now))
        if n >= d:
            return Verdict(Action.NOTIFY if n == d else Action.SILENT, "user_day", n)

    g = limits.global_per_minute
    if g:
        # 跨所有公司:額度綁的是同一把 key
        n = _count(db, ChatHistory.role == ChatRole.USER,
                   ChatHistory.created_at >= since)
        if n > g:
            return Verdict(Action.BUSY, "global_minute", n)

    return _ALLOW


def _with_contact(head: str, contact: str | None) -> str:
    contact = (contact or "").strip()
    return f"{head};急的話可以直接聯繫 {contact}。" if contact else f"{head}。"


def reply_text(verdict: Verdict, contact: str | None) -> str | None:
    """要回給客人的字;None = 不回(SILENT 與 ALLOW)。"""
    if verdict.action is Action.BUSY:
        return _with_contact(_BUSY, contact)
    if verdict.action is Action.NOTIFY:
        if verdict.rule == "user_minute":
            return MINUTE_NOTICE
        return _with_contact(_DAY_NOTICE, contact)
    return None
```

- [ ] **Step 5: 跑測試確認通過**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider tests/test_ratelimit.py tests/test_config.py`
Expected: 全部 passed。

- [ ] **Step 6: 變異檢查(手動,改完一定要還原)**

逐一把下列改動套到 `app/ratelimit.py`,各跑一次 `tests/test_ratelimit.py`,每一個都必須至少有一個測試失敗;跑完用 `git checkout app/ratelimit.py` 還原(檔案還沒 commit 的話,先 `cp app/ratelimit.py "$TEMP/ratelimit_orig.py"`,最後 `cp` 回來):

| 改動 | 應該抓到的測試 |
|---|---|
| `n == m + 1` → `n == m` | `test_the_minute_limit_notifies_once_then_goes_silent` |
| 每天那條的 `ChatRole.ASSISTANT` → `ChatRole.USER` | `test_customer_messages_do_not_count_toward_the_daily_limit` |
| `taipei_midnight_utc(now)` → `now.replace(hour=0, minute=0, second=0, microsecond=0)` | `test_the_day_starts_at_taipei_midnight` |
| 全站那條加上 `ChatHistory.user_id == user_pk` | `test_the_global_limit_counts_every_company` |
| 把全站那段移到最前面 | `test_a_customer_over_their_own_limit_is_not_reported_as_global_busy` |
| `if m:` → `if m is not None:` | `test_a_zero_limit_turns_that_rule_off` |
| `check()` 裡拿掉 try/except | `test_a_failing_count_lets_the_message_through` |

確認 `git diff app/ratelimit.py` 為空(或與備份一致)再往下。

- [ ] **Step 7: 跑全套**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider --ignore=tests/test_eval_jev.py`
Expected: `314 passed, 1 skipped`(287 + `test_ratelimit.py` 的 24 個 + `test_config.py` 的 3 個)。數字不同時,以「0 failed」為準,把實際數字記下來給 Task 4 用。

- [ ] **Step 8: Commit**

```bash
git add app/ratelimit.py app/config.py tests/test_ratelimit.py tests/test_config.py
git commit -m "feat(ratelimit): 三條上限的判定模組,計數直接從 chat_histories 算

每人每分鐘 5、每人每天 20(算 AI 回覆,不算客人訊息)、全站每分鐘 30。
個人規則在前;剛好踩到上限回一次提醒、之後安靜,靠計數本身決定、不另外
記狀態。一天照台灣午夜切(固定 UTC+8)。查詢失敗放行。0 = 關閉該條。

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: 接進 webhook,設定傳進容器

**Files:**
- Modify: `app/routers/webhook.py`(import 區;`process_text_event` 裡 `decision = match_keyword(rules, text)` / `if decision is None:` 之後)
- Modify: `compose.yaml`(app 服務的 `environment`,`NVIDIA_MODEL` 之後)
- Modify: `.env.example`(NVIDIA 段落之後、Jev 段落之前)
- Test: `tests/test_webhook.py`

**Interfaces:**
- Consumes: `ratelimit.check`、`ratelimit.reply_text`、`ratelimit.Limits.from_settings`、`ratelimit.Action`、`ratelimit.MINUTE_NOTICE`(Task 2);`Company.contact`(Task 1)
- Produces: 無(終端行為)

- [ ] **Step 1: 寫失敗的測試**

`tests/test_webhook.py` 檔尾加(這些 fixture / helper 都是檔案裡既有的:`rules`、`line_out`、`_llm`、`_replies`、`_mode`、`_body`、`_post`、`loading_calls`、`read_calls`、`SAFETY_SCRIPT`):

```python
# --- 速率限制 -----------------------------------------------------------------

from app.models import ChatRole  # noqa: E402
from app.ratelimit import MINUTE_NOTICE  # noqa: E402


def _flood(n, *, user="U1", role=ChatRole.USER):
    """先替這位客人塞 n 則「剛剛」的紀錄。"""
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        c = db.scalar(select(Company).where(Company.slug == "acme"))
        u = db.scalar(select(User).where(User.company_id == c.id,
                                         User.line_user_id == user))
        if u is None:
            u = User(company_id=c.id, line_user_id=user)
            db.add(u)
            db.flush()
        for _ in range(n):
            db.add(ChatHistory(company_id=c.id, user_id=u.id, role=role,
                               content="x", created_at=now))


def _set_contact(value):
    with session_scope() as db:
        db.scalar(select(Company).where(Company.slug == "acme")).contact = value


def _last_row():
    with session_scope() as db:
        return db.scalars(select(ChatHistory).order_by(ChatHistory.id.desc())).first()


def test_over_the_minute_limit_the_model_is_not_called_and_a_notice_is_sent(
        line_out, monkeypatch, loading_calls, read_calls):
    calls = _llm(monkeypatch)
    _flood(5)
    with TestClient(app) as client:
        _post(client, _body(msg_id="M6"))
    assert calls == []
    assert loading_calls == []
    assert _replies(line_out) == [MINUTE_NOTICE]
    assert read_calls == ["read-M6"]
    row = _last_row()
    assert row.role == ChatRole.ASSISTANT and row.content == MINUTE_NOTICE


def test_a_silent_message_sends_nothing_and_stays_unread(line_out, monkeypatch, read_calls):
    calls = _llm(monkeypatch)
    _flood(6)
    with TestClient(app) as client:
        _post(client, _body(msg_id="M7"))
    assert calls == []
    assert line_out == []
    assert read_calls == []


def test_global_busy_replies_with_the_contact(line_out, monkeypatch):
    calls = _llm(monkeypatch)
    _set_contact("02-2345-6789")
    for i in range(10):
        _flood(3, user=f"F{i}")                       # 30 則,每人都在個人上限以內
    with TestClient(app) as client:
        _post(client, _body(msg_id="M31"))           # 第 31 則
    assert calls == []
    assert _replies(line_out) == [
        "目前詢問的人比較多,請過幾分鐘再傳一次;急的話可以直接聯繫 02-2345-6789。"]


def test_the_daily_notice_without_a_contact_has_no_contact_clause(line_out, monkeypatch):
    _llm(monkeypatch)
    _flood(20, role=ChatRole.ASSISTANT)
    with TestClient(app) as client:
        _post(client, _body(msg_id="M21"))
    assert _replies(line_out) == ["今天跟我聊的次數已經到上限了,明天再來問我。"]


def test_a_keyword_transfer_still_happens_over_the_limit(rules, line_out, monkeypatch):
    """安全觸發不管傳了幾則都要轉 —— 限流只擋要打模型的路徑。"""
    calls = _llm(monkeypatch)
    _flood(10)
    with TestClient(app) as client:
        _post(client, _body(text="我朋友吃完過敏送醫了", msg_id="M11"))
    assert calls == []
    assert _replies(line_out) == [SAFETY_SCRIPT]
    assert _mode()[0] == ConversationMode.HUMAN


def test_under_the_limits_the_model_still_answers(line_out, monkeypatch):
    calls = _llm(monkeypatch)
    _flood(4)
    with TestClient(app) as client:
        _post(client, _body(msg_id="M5"))
    assert len(calls) == 1
    assert _replies(line_out) == ["您好"]


def test_a_non_text_message_still_gets_the_canned_reply_over_the_limit(line_out, monkeypatch):
    """固定回覆不打模型、LINE 的 reply 也不收費 —— 不限流。"""
    from app.routers.webhook import NON_TEXT_REPLY
    _llm(monkeypatch)
    _flood(10)
    with TestClient(app) as client:
        _post(client, _image_body(msg_id="M11"))
    assert _replies(line_out) == [NON_TEXT_REPLY]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider tests/test_webhook.py -k "limit or silent or busy or daily_notice"`
Expected: 前四個 failed(模型照樣被呼叫、`calls == []` 不成立);`test_a_keyword_transfer_still_happens_over_the_limit`、`test_under_the_limits_the_model_still_answers`、`test_a_non_text_message_still_gets_the_canned_reply_over_the_limit` passed(它們守的是既有行為,Step 5 的變異檢查會證明它們擋得住)。

- [ ] **Step 3: 實作**

`app/routers/webhook.py` 的 import 區,在 `from app.agent.zh import ensure_traditional` 之後加:

```python
from app import ratelimit
```

`process_text_event` 裡,把

```python
            decision = match_keyword(rules, text)
            if decision is None:
                # 取最近 N 則,但排除剛剛寫進去的這一句 —— 它會由 build_messages
```

改成

```python
            decision = match_keyword(rules, text)
            if decision is None:
                # 限流只擋要打模型的這條路:關鍵字轉真人(上一行)與非文字的固定
                # 回覆都不花模型額度,而安全觸發不管傳了幾則都要轉。放在「正在
                # 輸入」之前 —— 被擋的訊息不該讓客人看到輸入中卻什麼都沒收到。
                verdict = ratelimit.check(db, user_pk=user_pk, now=now,
                                          limits=ratelimit.Limits.from_settings(settings))
                if verdict.action is not ratelimit.Action.ALLOW:
                    logger.warning("限流 客人 …%s 規則=%s 計數=%d 動作=%s",
                                   line_user_id[-6:], verdict.rule, verdict.count,
                                   verdict.action.name)
                    reply = ratelimit.reply_text(verdict, company.contact)
                    if reply is None:
                        # 安靜:不回、不標已讀 —— 店家在官方帳號後台看得到有人在狂傳
                        return
                    # 寫進紀錄:模型下一輪看得懂上下文,「每天只提醒一次」也靠它
                    db.add(ChatHistory(company_id=company_id, user_id=user_pk,
                                       role=ChatRole.ASSISTANT, content=reply))
                    client.send(reply_token, line_user_id, reply)
                    _mark_read(client, mark_as_read_token)
                    return

                # 取最近 N 則,但排除剛剛寫進去的這一句 —— 它會由 build_messages
```

(`now`、`user_pk`、`settings`、`company`、`client` 都是 `process_text_event` 裡既有的區域變數。)

`compose.yaml`,app 服務 `environment` 裡 `NVIDIA_MODEL: ...` 那一行之後加:

```yaml
      # 速率限制。0 = 關閉那一條(跑評測或 demo 前想暫時關掉時)
      RATE_LIMIT_USER_PER_MINUTE: ${RATE_LIMIT_USER_PER_MINUTE:-5}
      RATE_LIMIT_USER_PER_DAY: ${RATE_LIMIT_USER_PER_DAY:-20}
      RATE_LIMIT_GLOBAL_PER_MINUTE: ${RATE_LIMIT_GLOBAL_PER_MINUTE:-30}
```

`.env.example`,在 `NVIDIA_MODEL=nvidia/nemotron-3-super-120b-a12b` 之後、`# Jev` 之前加(前後各留一個空行):

```
# 速率限制(選填,不填就用預設)。0 = 關閉那一條。
# 每人每天算的是 AI 回了幾則,一天照台灣午夜切。全站每分鐘 30 是為了留在
# NVIDIA 免費層每分鐘約 40 次以下。
RATE_LIMIT_USER_PER_MINUTE=5
RATE_LIMIT_USER_PER_DAY=20
RATE_LIMIT_GLOBAL_PER_MINUTE=30
```

- [ ] **Step 4: 跑測試確認通過**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider tests/test_webhook.py`
Expected: 全部 passed。

- [ ] **Step 5: 變異檢查(手動,改完還原)**

先 `cp app/routers/webhook.py "$TEMP/webhook_orig.py"`,逐一套用、各跑一次 `tests/test_webhook.py`,最後 `cp "$TEMP/webhook_orig.py" app/routers/webhook.py`:

| 改動 | 應該抓到的測試 |
|---|---|
| 把整段限流移到 `decision = match_keyword(...)` 之前 | `test_a_keyword_transfer_still_happens_over_the_limit` |
| 把整段限流移到非文字分支(`if message_type != "text":`)之前 | `test_a_non_text_message_still_gets_the_canned_reply_over_the_limit` |
| `if verdict.action is not ratelimit.Action.ALLOW:` → `if False:` | 前四個限流測試 |
| 拿掉 `_mark_read(client, mark_as_read_token)` | `test_over_the_minute_limit_...` |
| 拿掉 `db.add(ChatHistory(...))` | `test_over_the_minute_limit_...` |
| `return`(安靜那一行)→ `pass` | `test_a_silent_message_sends_nothing_and_stays_unread` |

- [ ] **Step 6: 驗證 compose 設定**

Run: `docker compose config 2>&1 | grep -E "RATE_LIMIT|error"`
Expected: 三行 `RATE_LIMIT_...: "5"` / `"20"` / `"30"`,沒有 error。(本機 Docker 沒開的話跳過這步,在 PR 描述註明。)

- [ ] **Step 7: 跑全套**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider --ignore=tests/test_eval_jev.py`
Expected: 0 failed;passed 比 Task 2 多 7。記下實際數字給 Task 4。

- [ ] **Step 8: Commit(`.env.example` 只暫存限流段落)**

`.env.example` 的工作區裡還有一段**不能 commit** 的 Jev 設定,所以它不能用 `git add`。用下面的指令把「HEAD 版本 + 限流段落」直接寫進暫存區:

```bash
git add app/routers/webhook.py compose.yaml tests/test_webhook.py
.venv/Scripts/python - <<'EOF'
import subprocess
head = subprocess.run(["git", "show", "HEAD:.env.example"], capture_output=True, check=True).stdout.decode("utf-8")
work = open(".env.example", encoding="utf-8").read().replace("\r\n", "\n")
start = work.index("# 速率限制(選填")
end = work.index("RATE_LIMIT_GLOBAL_PER_MINUTE=30\n") + len("RATE_LIMIT_GLOBAL_PER_MINUTE=30\n")
anchor = "NVIDIA_MODEL=nvidia/nemotron-3-super-120b-a12b\n"
assert head.count(anchor) == 1
staged = head.replace(anchor, anchor + "\n" + work[start:end])
h = subprocess.run(["git", "hash-object", "-w", "--stdin"], input=staged.encode("utf-8"), capture_output=True, check=True).stdout.decode().strip()
subprocess.run(["git", "update-index", "--cacheinfo", f"100644,{h},.env.example"], check=True)
EOF
git diff --cached .env.example | grep -c TYPESAFE   # 必須印出 0
git commit -m "feat(webhook): 呼叫模型前先過速率限制

放在關鍵字層之後、正在輸入之前:關鍵字轉真人與非文字固定回覆不受限;
提醒與忙碌訊息寫進 chat_histories 並標已讀,安靜的保持未讀。三個上限
透過 compose 傳進容器,.env 可調、0 = 關閉。

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status --short   # 只應剩 .env.example(Jev 段落)與三個 eval_jev 檔
```

---

### Task 4: 文件

**Files:**
- Modify: `README.md`(「狀態」段落、「下一步」表格)
- Modify: `docs/report.md`(第六節自動測試的數字、第八節限制 2、第九節下一步 1)

**Interfaces:** 無

- [ ] **Step 1: 量出實際數字**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q -p no:cacheprovider --ignore=tests/test_eval_jev.py 2>&1 | tail -1
git ls-files 'tests/*.py' | xargs cat | wc -l
git ls-files 'app/*.py' 'app/**/*.py' | sort -u | xargs cat | wc -l
```

(行數的算法跟 report 既有的「測試碼 3,634 行 / 正式碼 2,131 行」相同;新檔要先 `git add` 才會被 `git ls-files` 算到 —— Task 1–3 已經 commit,所以會。)

- [ ] **Step 2: 改 README**

「狀態」段落,在「主要模型可以改用 NVIDIA…」那一段之後加一段:

```markdown
公開的 webhook 有速率限制:每人每分鐘 5 則、每人每天 20 則(算 AI 回了幾則)、全站每分鐘 30 則,
`.env` 可調、設 0 關閉。個人超量第一次回提醒、之後安靜;全站忙碌每則都回忙碌訊息並附上店家聯絡方式。
關鍵字轉真人不受限流影響。
```

同一節的「283 個自動測試全過、1 個跳過」改成 Step 1 量到的數字。

「下一步」表格刪掉 `**速率限制**` 那一列。

- [ ] **Step 3: 改 report**

第六節「自動測試」第一行的測試數與行數,換成 Step 1 量到的數字(格式照舊:`N 個測試(N-1 通過、1 跳過)`、`測試碼 X 行,比正式碼的 Y 行還多`)。

第八節限制 2,整段換成:

```markdown
2. **速率限制只擋得住「太多」,擋不住「太巧」。** 每人每分鐘 5、每天 20(算 AI 回覆)、
   全站每分鐘 30,計數直接從 `chat_histories` 算(設計見
   `docs/superpowers/specs/2026-10-05-rate-limit-design.md`)。已知的取捨:同一人同一瞬間
   兩則剛好卡在門檻,可能收到兩次或零次提醒;轉真人逾時的那一則若剛好被限流,客人連
   「專員目前不在線上」都收不到;全站 30 是依 NVIDIA 免費層「每分鐘約 40 次」推算的,
   那個數字沒有公開保證。
```

第九節表格第 1 列 `**速率限制 + autoheal**` 改成:

```markdown
| 1 | **autoheal** | 速率限制已完成(限制 2)。「能放著跑」剩下限制 1 留下的「卡住但沒退出」 |
```

- [ ] **Step 4: Commit**

```bash
git add README.md docs/report.md
git commit -m "docs: 速率限制寫進 README 與報告,數字同步

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: PR、部署、手機驗收

**Files:** 無(操作步驟)

- [ ] **Step 1: 推分支、開 PR**

```bash
git push -u origin feat/rate-limit
gh pr create --base master --head feat/rate-limit --title "速率限制:每人每分鐘 5、每天 20、全站每分鐘 30"
```

PR 描述照 #4 的格式(摘要 / 改了什麼 / 驗證 / 已知限制),結尾 `🤖 Generated with [Claude Code](https://claude.com/claude-code)`。開完用 ccd_pr 的 `get_status` 確認綁定、看 CI。

- [ ] **Step 2: 等使用者合併**

CI 三個 job(Python 3.11、3.13、PostgreSQL)都綠之後,請使用者在 GitHub 按 Merge。**不要自己合併**(auto mode 會擋,這也是刻意的審查關卡)。合併後本機:

```bash
git stash push -- .env.example && git checkout master && git pull origin master && git stash pop
```

- [ ] **Step 3: 部署到 GCP 主機(建置與啟動分開跑)**

```bash
ssh -i ~/.ssh/helpdesk_gcp helpdesk@136.70.19.152 'cd ~/universal-ai-helpdesk && git pull && docker compose build app && docker compose up -d app'
```

build 約 2–6 分鐘;失敗時舊容器不受影響,可直接重跑。等 `docker compose ps` 顯示 app `healthy`,並用 `curl https://unsaid-expend-eagle.ngrok-free.dev/health` 確認 200。

- [ ] **Step 4: 補上 contact**

```bash
ssh -i ~/.ssh/helpdesk_gcp helpdesk@136.70.19.152 'cd ~/universal-ai-helpdesk && docker compose exec -T app python -m app.cli seed --industry restaurant --slug bistro && docker compose exec -T app python -m app.cli release --slug bistro'
```

**不加 `--reset-history`**。`release` 是為了確保手機帳號在 AI 模式。確認:`docker compose exec -T db sh -c 'psql -U $POSTGRES_USER -d $POSTGRES_DB -tAc "select contact from companies"'` 印出 `02-2345-6789`。

- [ ] **Step 5: 手機驗收(請使用者操作)**

1. 一分鐘內連傳 6 則一般問題(例如「營業時間」):前 5 則正常回答,第 6 則收到「您傳得有點快…」,第 7 則沒有回應。
2. 主機 log 要有 `限流 客人 … 規則=user_minute 計數=6 動作=NOTIFY` 與 `動作=SILENT`:
   `docker compose logs app --since 5m | grep 限流`
3. 等一分鐘後傳「吃了不舒服」:照樣轉真人。驗收完 `app.cli release --slug bistro`。
