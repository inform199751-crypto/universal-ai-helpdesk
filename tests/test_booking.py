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


def test_today_is_counted_in_taipei_time(ids):
    """UTC 10/06 17:00 已經是台灣 10/07 01:00 —— 用 UTC 日期的話,昨天還訂得到。"""
    late = datetime(2026, 10, 6, 17, 0, tzinfo=timezone.utc)
    assert "只能訂位 10/07~10/21" in _check(
        ids, {"date": "2026-10-06", "party_size": 2}, now=late)["error"]


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
