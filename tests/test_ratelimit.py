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


def _rows(company_id, user_pk, n, *, role=ChatRole.USER, at=NOW, content="x"):
    with session_scope() as db:
        for _ in range(n):
            db.add(ChatHistory(company_id=company_id, user_id=user_pk, role=role,
                               content=content, created_at=at))


# 提醒列的內容用 reply_text 產生,跟 webhook 實際寫進去的一字不差
DAY_NOTICE = reply_text(Verdict(Action.NOTIFY, "user_day", 20), "02-2345-6789")


def _notice(company_id, user_pk, content, *, at):
    _rows(company_id, user_pk, 1, role=ChatRole.ASSISTANT, at=at, content=content)


def _check(user_pk, *, now=NOW, limits=LIMITS):
    with session_scope() as db:
        return check(db, user_pk=user_pk, now=now, limits=limits)


@pytest.fixture
def me():
    cid = _company()
    return cid, _user(cid)


# --- 每人每分鐘 ---------------------------------------------------------------

@pytest.mark.parametrize("n, notified, expected", [
    (5, False, Verdict(Action.ALLOW)),
    (6, False, Verdict(Action.NOTIFY, "user_minute", 6)),
    (7, True, Verdict(Action.SILENT, "user_minute", 7)),   # 第 6 則已經回過提醒
])
def test_the_minute_limit_notifies_once_then_goes_silent(me, n, notified, expected):
    cid, uid = me
    _rows(cid, uid, n, at=NOW - timedelta(seconds=10))
    if notified:
        _notice(cid, uid, MINUTE_NOTICE, at=NOW - timedelta(seconds=5))
    assert _check(uid) == expected


def test_over_the_minute_limit_without_a_notice_it_still_notifies(me):
    """貼圖、轉真人這些路徑不經過檢查卻會寫 user 列,計數可能直接跳過 6 ——
    「剛好第 6 則才提醒」的話,這位客人一句提醒都沒收到就被靜音。"""
    cid, uid = me
    _rows(cid, uid, 8, at=NOW - timedelta(seconds=10))
    assert _check(uid) == Verdict(Action.NOTIFY, "user_minute", 8)


@pytest.mark.parametrize("seconds_ago, expected", [
    (61, Verdict(Action.NOTIFY, "user_minute", 8)),   # 上一分鐘的提醒,這一分鐘要再提醒
    (30, Verdict(Action.SILENT, "user_minute", 8)),
])
def test_only_a_minute_notice_inside_the_window_counts(me, seconds_ago, expected):
    cid, uid = me
    _rows(cid, uid, 8, at=NOW - timedelta(seconds=10))
    _notice(cid, uid, MINUTE_NOTICE, at=NOW - timedelta(seconds=seconds_ago))
    assert _check(uid) == expected


def test_the_minute_limit_counts_only_customer_messages(me):
    """5 則客人 + 5 則 AI 回覆:算進 assistant 的話就是 10 則,會被擋。"""
    cid, uid = me
    _rows(cid, uid, 5, at=NOW - timedelta(seconds=10))
    _rows(cid, uid, 5, role=ChatRole.ASSISTANT, at=NOW - timedelta(seconds=10))
    assert _check(uid) == Verdict(Action.ALLOW)


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

@pytest.mark.parametrize("replies, notified, expected", [
    (19, False, Verdict(Action.ALLOW)),
    (20, False, Verdict(Action.NOTIFY, "user_day", 20)),
    (20, True, Verdict(Action.SILENT, "user_day", 21)),   # 20 則回答 + 今天的提醒
])
def test_the_daily_limit_counts_ai_replies(me, replies, notified, expected):
    cid, uid = me
    _rows(cid, uid, replies, role=ChatRole.ASSISTANT, at=NOW - timedelta(hours=1))
    if notified:
        _notice(cid, uid, DAY_NOTICE, at=NOW - timedelta(minutes=30))
    assert _check(uid) == expected


def test_over_the_daily_limit_without_a_notice_today_it_still_notifies(me):
    """19 則回答後客人傳兩張貼圖:固定回覆也是 assistant 列,計數直接從 19 跳到 21。"""
    cid, uid = me
    _rows(cid, uid, 22, role=ChatRole.ASSISTANT, at=NOW - timedelta(hours=1))
    assert _check(uid) == Verdict(Action.NOTIFY, "user_day", 22)


def test_a_daily_notice_from_before_taipei_midnight_does_not_count(me):
    """NOW 是台灣 10/5 20:00;台灣 10/4 23:59(UTC 15:59)的提醒是昨天的。"""
    cid, uid = me
    _notice(cid, uid, DAY_NOTICE, at=datetime(2026, 10, 4, 15, 59, tzinfo=timezone.utc))
    _rows(cid, uid, 21, role=ChatRole.ASSISTANT, at=NOW - timedelta(hours=1))
    assert _check(uid) == Verdict(Action.NOTIFY, "user_day", 21)


def test_another_customers_notice_does_not_silence_this_one(me):
    cid, uid = me
    other = _user(cid, "U2")
    _notice(cid, other, DAY_NOTICE, at=NOW - timedelta(minutes=30))
    _notice(cid, other, MINUTE_NOTICE, at=NOW - timedelta(seconds=5))
    _rows(cid, uid, 21, role=ChatRole.ASSISTANT, at=NOW - timedelta(hours=1))
    assert _check(uid) == Verdict(Action.NOTIFY, "user_day", 21)
    _rows(cid, uid, 7, at=NOW - timedelta(seconds=10))
    assert _check(uid) == Verdict(Action.NOTIFY, "user_minute", 7)


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


def test_a_blocked_spammer_does_not_fill_the_global_quota(me):
    """一個帳號一分鐘狂傳 40 則:超過 5 則的部分他自己的上限已經擋下,
    全站只算他 5 則 —— 否則一個人就能讓所有客人收到「詢問的人比較多」。"""
    cid, uid = me
    _rows(cid, _user(cid, "SPAM"), 40)
    _rows(cid, uid, 1)
    assert _check(uid) == Verdict(Action.ALLOW)                 # 5 + 1 = 6


def test_without_a_minute_limit_the_global_count_is_raw(me):
    """每人每分鐘關閉(0)時沒有「每人最多算幾則」可言,用原始計數。"""
    cid, uid = me
    _rows(cid, _user(cid, "SPAM"), 31)
    _rows(cid, uid, 1)
    assert _check(uid, limits=Limits(0, 20, 30)) == Verdict(Action.BUSY, "global_minute", 32)


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
