"""預約的業務邏輯。設計見 docs/superpowers/specs/2026-10-06-booking-agent-design.md 第五節。

用真的資料庫:名額、防重複都是查詢,mock 掉就什麼都沒驗到。
「現在」固定在台灣 2026-10-06(二)12:00 —— 週一公休、14 天範圍的邊界都看得到。
"""

import re
import threading
import time
from datetime import date, datetime, timedelta, timezone

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
OK_ARGS = {"date": "2026-10-09", "weekday": "五", "time": "20:00", "party_size": 4,
           "name": "王小明", "phone": "0912-345-678"}
CLINIC_ARGS = {"date": "2026-10-09", "weekday": "五", "time": "09:30", "name": "王小明",
               "phone": "0912345678", "service": "青春痘"}


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def ids():
    with session_scope() as db:
        c = Company(slug="bistro", name="青木", industry="restaurant",
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
    assert set(params["required"]) == {"date", "weekday", "time", "party_size", "name", "phone"}
    assert set(check["function"]["parameters"]["required"]) == {"date", "weekday", "party_size"}


def test_both_tools_require_a_weekday_the_program_can_check_against_the_date():
    """日期正確不能靠模型心算:關掉思考後它會把「週五」訂成週六。weekday 是必填的 enum,
    程式拿它跟日期對 —— 對不上就擋。"""
    for config in (RESTAURANT, CLINIC):
        for tool in booking.build_tools(config):
            params = tool["function"]["parameters"]
            assert params["properties"]["weekday"]["enum"] == list("一二三四五六日")
            assert "weekday" in params["required"]


def test_clinic_tools_ask_for_the_service_instead_of_party_size():
    check, create = booking.build_tools(CLINIC)
    props = create["function"]["parameters"]["properties"]
    assert "party_size" not in props
    assert "party_size" not in check["function"]["parameters"]["properties"]
    assert props["service"]["enum"] == ["一般皮膚", "青春痘", "醫美諮詢"]
    assert "service" in create["function"]["parameters"]["required"]
    assert "weekday" in create["function"]["parameters"]["required"]
    assert "weekday" in check["function"]["parameters"]["required"]


# --- 預約說明(日期對照表) ----------------------------------------------------

def test_the_prompt_gives_today_and_a_date_table_with_closed_days():
    p = booking.booking_prompt(RESTAURANT, today=date(2026, 10, 6), contact=CONTACT)
    assert "今天是 2026-10-06(二)" in p
    assert "2026-10-09(五)" in p
    assert "2026-10-12(一,公休)" in p
    assert "2026-10-20(二)" in p and "2026-10-21" not in p   # 今天起算 14 天
    assert "不要反問" in p
    assert "最多 6 位" in p and CONTACT in p
    # 改、取消不在範圍內(spec §一):沒講的話,模型會把重複呼叫的 ok 當成「已幫您改好」
    assert f"已成立的訂位不能在這裡修改或取消;客人要改人數、時間或取消,請他直接聯繫 {CONTACT}。" in p


def test_the_prompt_maps_each_weekday_straight_to_its_nearest_date():
    """客人只說「週五」時,模型直接查這一行 —— 不必在 15 天的表裡自己數、也不必心算。
    從今天起連續 7 天、依日期順序:今天就是週二,所以週二排第一、是今天。"""
    p = booking.booking_prompt(RESTAURANT, today=date(2026, 10, 6), contact=CONTACT)
    line = ("客人只說星期幾時,直接用這一行(從今天起最近的那一天):"
            "週二→2026-10-06(今天)、週三→2026-10-07、週四→2026-10-08、"
            "週五→2026-10-09、週六→2026-10-10、週日→2026-10-11、週一→2026-10-12(公休)")
    assert line in p
    assert "週五→2026-10-09" in p and "週二→2026-10-06(今天)" in p
    assert "週一→2026-10-12(公休)" in p
    assert p.index(line) < p.index("可以訂位的日期對照")   # 先看直接查的那行
    assert "不要反問" in p                                  # 既有規則照舊


def test_the_weekday_line_marks_today_even_when_it_is_closed():
    p = booking.booking_prompt(RESTAURANT, today=date(2026, 10, 12), contact=CONTACT)   # 週一公休
    assert "週一→2026-10-12(今天)(公休)、週二→2026-10-13" in p


def test_the_clinic_weekday_line_marks_its_own_closed_day():
    p = booking.booking_prompt(CLINIC, today=date(2026, 10, 6), contact=CONTACT)
    assert "週日→2026-10-11(公休)" in p and "週一→2026-10-12(公休)" not in p


NEXT_WEEK_HEAD = "客人說「下週」「下禮拜」時,用這一行(下週一到下週日):"


def _line(p, head):
    (line,) = [ln for ln in p.splitlines() if ln.startswith(head)]
    return line


def test_the_prompt_lists_next_week_monday_to_sunday_on_its_own_line():
    """「下週五」= 這一週(週一到週日)之後那一週的週五。今天 10/15(四):下週是 10/19~10/25,
    下週五是 10/23 —— 不是最近的週五 10/16。沒有這一行,模型只看得到「最近的那一天」。"""
    p = booking.booking_prompt(RESTAURANT, today=date(2026, 10, 15), contact=CONTACT)
    line = (NEXT_WEEK_HEAD + "週一→2026-10-19(公休)、週二→2026-10-20、週三→2026-10-21、"
            "週四→2026-10-22、週五→2026-10-23、週六→2026-10-24、週日→2026-10-25")
    assert line in p.splitlines()
    assert "週五→2026-10-16" in _line(p, "客人只說星期幾時")   # 最近的那行照舊
    assert p.index("客人只說星期幾時") < p.index(NEXT_WEEK_HEAD) < p.index("可以訂位的日期對照")
    assert "- 客人說「下週X」「下禮拜X」時,用下週那一行的日期;確認時把日期講出來。" in p


def test_next_week_is_the_same_week_on_a_saturday():
    """今天 10/17(六):這一週是 10/12~10/18,下週一樣是 10/19~10/25,下週五還是 10/23
    (剛好也是最近的週五)。"""
    p = booking.booking_prompt(RESTAURANT, today=date(2026, 10, 17), contact=CONTACT)
    assert "週五→2026-10-23" in _line(p, NEXT_WEEK_HEAD)
    assert _line(p, NEXT_WEEK_HEAD).startswith(NEXT_WEEK_HEAD + "週一→2026-10-19(公休)、")


def test_next_week_days_past_the_booking_range_are_marked():
    short = {**RESTAURANT, "advance_days": 5}   # 10/15 起 5 天:最後一天是 10/20(二)
    line = _line(booking.booking_prompt(short, today=date(2026, 10, 15), contact=CONTACT),
                 NEXT_WEEK_HEAD)
    assert "週一→2026-10-19(公休)、週二→2026-10-20、週三→2026-10-21(超出可訂位範圍)" in line
    clinic = {**CLINIC, "advance_days": 7}      # 診所週日公休,10/25 也超出範圍:兩個都標
    line = _line(booking.booking_prompt(clinic, today=date(2026, 10, 15), contact=CONTACT),
                 NEXT_WEEK_HEAD)
    assert line.endswith("週日→2026-10-25(公休)(超出可預約範圍)")
    assert "週四→2026-10-22、" in line   # 10/22 是最後一天,還在範圍內


def test_the_prompt_lines_and_the_mismatch_error_name_the_same_dates():
    """「最近的那天」「下週那天」prompt 與錯誤訊息各用一次:兩邊各算一份的話,哪天改了一邊,
    模型會在 prompt 看到一個日期、被工具指到另一個日期。一週七天每一天都對一次。"""
    for offset in range(7):
        today = date(2026, 10, 12 + offset)
        now = datetime(today.year, today.month, today.day, 4, 0, tzinfo=timezone.utc)
        p = booking.booking_prompt(RESTAURANT, today=today, contact=CONTACT)
        pattern = r"週(.)→(\d{4}-\d{2}-\d{2})"
        nearest = dict(re.findall(pattern, _line(p, "客人只說星期幾時")))
        next_week = dict(re.findall(pattern, _line(p, NEXT_WEEK_HEAD)))
        for w in booking.WEEKDAYS:
            wrong_day = date.fromisoformat(nearest[w]) + timedelta(days=1)
            _, error = booking._parse_date(RESTAURANT, wrong_day.isoformat(), w, now)
            assert f"最近的週{w}是 {nearest[w]};" in error
            assert f"如果客人說的是下週{w},是 {next_week[w]};" in error


def test_the_prompt_tells_the_model_to_fill_the_weekday_and_to_fix_a_mismatch_from_what_was_said():
    p = booking.booking_prompt(RESTAURANT, today=date(2026, 10, 6), contact=CONTACT)
    assert ("- 呼叫 check_availability、create_booking 時,weekday 填客人說的星期幾;"
            "客人只給日期時,照對照表查那天是星期幾再填。") in p
    assert ("- 工具說日期和星期對不上時,回頭看客人說的是星期幾還是日期,"
            "照客人說的那個改正後再查;不要自己猜。") in p


def test_the_clinic_prompt_asks_for_the_service_not_the_party_size():
    p = booking.booking_prompt(CLINIC, today=date(2026, 10, 6), contact=CONTACT)
    assert "看診項目" in p
    assert "人數" not in p and "最多" not in p
    assert f"已成立的預約不能在這裡修改或取消;客人要改時間或取消,請他直接聯繫 {CONTACT}。" in p


# --- 查空位 -------------------------------------------------------------------

def test_an_open_day_lists_every_slot(ids):
    assert _check(ids, {"date": "2026-10-09", "weekday": "五", "party_size": 4}) == {
        "date": "2026-10-09", "weekday": "五", "open": True,
        "slots": [{"time": "18:00", "available": True},
                  {"time": "19:00", "available": True},
                  {"time": "20:00", "available": True}]}


@pytest.mark.parametrize("args, fragment", [
    ({"date": "2026-10-12", "weekday": "一", "party_size": 2}, "10/12(一)沒有開放訂位"),
    ({"date": "2026-10-21", "weekday": "三", "party_size": 2}, "只能訂位 10/06~10/20"),
    ({"date": "2026-10-05", "weekday": "一", "party_size": 2}, "只能訂位 10/06~10/20"),
    ({"date": "10月9日", "weekday": "五", "party_size": 2}, "YYYY-MM-DD"),
    ({"date": "2026-10-09", "weekday": "五", "party_size": 7}, "超過 6 位請直接聯繫 02-2345-6789"),
    ({"date": "2026-10-09", "weekday": "五", "party_size": 0}, "人數至少 1 位"),
    ({"date": "2026-10-09", "weekday": "五"}, "請先問客人幾位"),
])
def test_check_explains_why_it_cannot_answer(ids, args, fragment):
    assert fragment in _check(ids, args)["error"]


def test_a_full_slot_is_reported_unavailable(ids):
    company_id, _ = ids
    for i in range(5):
        assert _create(ids, {**OK_ARGS, "time": "19:00"},
                       user_pk=_user(company_id, f"F{i}"))["ok"]
    slots = _check(ids, {"date": "2026-10-09", "weekday": "五", "party_size": 2})["slots"]
    assert [s["available"] for s in slots] == [True, False, True]


def test_slots_already_past_today_are_unavailable(ids):
    slots = _check(ids, {"date": "2026-10-07", "weekday": "三", "party_size": 2},
                   now=EVENING)["slots"]
    assert [s["available"] for s in slots] == [False, True, True]


def test_today_is_counted_in_taipei_time(ids):
    """UTC 10/06 17:00 已經是台灣 10/07 01:00 —— 用 UTC 日期的話,昨天還訂得到。"""
    late = datetime(2026, 10, 6, 17, 0, tzinfo=timezone.utc)
    assert "只能訂位 10/07~10/21" in _check(
        ids, {"date": "2026-10-06", "weekday": "二", "party_size": 2}, now=late)["error"]


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
    ({"date": "2026-10-12", "weekday": "一"}, "沒有開放訂位"),
    ({"date": "2026-10-21", "weekday": "三"}, "只能訂位 10/06~10/20"),
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
    result = _create(ids, {**OK_ARGS, "date": "2026-10-07", "weekday": "三", "time": "18:00"},
                     now=EVENING)
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
    # 客人可能是想改人數:結果要明講沒有改,模型才不會回「已幫您改好」
    assert "沒有更改" in again["note"] and CONTACT in again["note"]


@pytest.mark.skipif(engine.dialect.name == "sqlite",
                    reason="要驗的是 PostgreSQL 的列鎖;SQLite 只用在開發與測試,不保證這件事")
def test_two_simultaneous_calls_from_the_same_customer_book_once(ids, monkeypatch):
    """同一位客人的兩個請求同時到(LINE 重送、連按兩次):第二個要等第一個 commit 完才做
    防重複檢查。防重複排在鎖之前的話,兩個都看不到對方,兩筆都成立。

    數名額那一步放慢一點,讓先拿到鎖的那個抓久一點 —— 不靠排程運氣,每次都看得到競態。"""
    real_taken = booking._taken

    def slow_taken(*args, **kwargs):
        time.sleep(0.3)
        return real_taken(*args, **kwargs)

    monkeypatch.setattr(booking, "_taken", slow_taken)
    barrier = threading.Barrier(2)
    results, errors = [], []

    def book():
        try:
            barrier.wait(timeout=10)
            results.append(_create(ids))   # 每個執行緒自己的 session_scope()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=book) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    assert len(_rows()) == 1
    assert sorted(bool(r.get("duplicate")) for r in results) == [False, True]


# --- 星期防線:日期與星期是否一致由程式檢查,不靠模型 ----------------------------

MISMATCH = ("2026-10-10 是週六,不是週五。如果客人說的是週五,從今天起最近的週五是 2026-10-09;"
            "如果客人說的是下週五,是 2026-10-16;"
            "如果客人說的是 10/10,weekday 請填「六」。")


def test_a_booking_whose_weekday_does_not_match_the_date_is_refused_and_saves_nothing(ids):
    """2026-10-08 真模型驗收:客人說週五,模型自己心算成 10/10(週六)就訂了。"""
    result = _create(ids, {**OK_ARGS, "date": "2026-10-10", "weekday": "五"})
    assert result == {"ok": False, "error": MISMATCH}
    # 兩種讀法都要講:客人說的是星期(改成最近的那天),或是日期(只是 weekday 填錯)
    assert "2026-10-10 是週六" in result["error"] and "2026-10-09" in result["error"]
    assert "weekday 請填「六」" in result["error"]
    assert _rows() == []


def test_check_refuses_a_mismatched_weekday_instead_of_listing_slots(ids):
    result = _check(ids, {"date": "2026-10-10", "weekday": "五", "party_size": 4})
    assert result == {"error": MISMATCH}
    assert "slots" not in result


@pytest.mark.parametrize("weekday", [None, "", "八", "星期", "五六", 5])
def test_a_missing_or_invalid_weekday_is_asked_for(ids, weekday):
    msg = "請一併提供那天是星期幾(一到日)"
    args = {k: v for k, v in OK_ARGS.items() if k != "weekday"}
    check_args = {"date": "2026-10-09", "party_size": 4}
    if weekday is not None:
        args["weekday"] = check_args["weekday"] = weekday
    assert _create(ids, args) == {"ok": False, "error": msg}
    assert _check(ids, check_args) == {"error": msg}
    assert _rows() == []


@pytest.mark.parametrize("weekday", ["五", "週五", "星期五", "禮拜五", "周五", " 週五 "])
def test_the_usual_ways_of_writing_a_weekday_are_accepted(ids, weekday):
    assert _create(ids, {**OK_ARGS, "weekday": weekday})["ok"] is True
    assert "error" not in _check(ids, {"date": "2026-10-09", "weekday": weekday, "party_size": 4})


@pytest.mark.parametrize("weekday", ["日", "天", "週日", "週天", "星期天", "禮拜天", "周日"])
def test_tian_means_sunday(ids, weekday):
    sunday = {"date": "2026-10-11", "weekday": weekday}
    assert _create(ids, {**OK_ARGS, **sunday})["booking"]["weekday"] == "日"
    assert _check(ids, {**sunday, "party_size": 2})["weekday"] == "日"


@pytest.mark.parametrize("args", [
    {"date": "2026-10-12", "weekday": "五"},   # 10/12 是公休的週一 —— 先講星期不符
    {"date": "2026-10-21", "weekday": "五"},   # 10/21 超出 14 天範圍 —— 先講星期不符
])
def test_a_weekday_mismatch_is_reported_before_closed_days_and_range(ids, args):
    """日期本身就錯了,先說「公休」或「超出範圍」會把模型帶偏到那個日期上。"""
    for error in (_check(ids, {**args, "party_size": 2})["error"],
                  _create(ids, {**OK_ARGS, **args})["error"]):
        assert "不是週五" in error
        assert "沒有開放" not in error and "只能訂位" not in error


@pytest.mark.parametrize("date_str, weekday, nearest", [
    ("2026-10-07", "二", "2026-10-06"),   # 最近的那天含今天
    ("2026-10-13", "一", "2026-10-12"),   # 今天是週二:週一要等到下週
    ("2026-10-09", "六", "2026-10-10"),
])
def test_the_nearest_weekday_counts_from_today_inclusive(ids, date_str, weekday, nearest):
    error = _check(ids, {"date": date_str, "weekday": weekday, "party_size": 2})["error"]
    assert f"最近的週{weekday}是 {nearest};" in error


def test_the_nearest_weekday_is_counted_from_the_taipei_date(ids):
    """UTC 10/06 17:00 是台灣 10/07(三) —— 用 UTC 日期的話,「最近的週二」會是已經過去的昨天。"""
    late = datetime(2026, 10, 6, 17, 0, tzinfo=timezone.utc)
    error = _check(ids, {"date": "2026-10-09", "weekday": "二", "party_size": 2},
                   now=late)["error"]
    assert "最近的週二是 2026-10-13;" in error


THURSDAY = datetime(2026, 10, 15, 4, 0, tzinfo=timezone.utc)   # 台灣 10/15(四)12:00


def test_the_mismatch_error_also_names_next_weeks_date(ids):
    """客人說「下週五」、模型卻填了別天:錯誤只給最近的週五(10/16)的話,會把模型導到這週。
    下週 = 這一週(週一到週日)之後那一週,今天 10/15(四)時下週五是 10/23。"""
    error = _check(ids, {"date": "2026-10-24", "weekday": "五", "party_size": 2},
                   now=THURSDAY)["error"]
    assert error == ("2026-10-24 是週六,不是週五。如果客人說的是週五,從今天起最近的週五是 2026-10-16;"
                     "如果客人說的是下週五,是 2026-10-23;"
                     "如果客人說的是 10/24,weekday 請填「六」。")


def test_next_week_counts_from_the_calendar_week_not_from_seven_days_later(ids):
    """今天 10/17(六):最近的週五與下週五都是 10/23 —— 下週是 10/19~10/25 那一週。"""
    saturday = datetime(2026, 10, 17, 4, 0, tzinfo=timezone.utc)
    error = _create(ids, {**OK_ARGS, "date": "2026-10-24"}, now=saturday)["error"]
    assert "最近的週五是 2026-10-23;" in error and "下週五,是 2026-10-23;" in error


def test_next_week_is_counted_from_the_taipei_date(ids):
    """UTC 10/18(日)17:00 是台灣 10/19(一):這一週已經換成 10/19~10/25,下週一是 10/26。
    用 UTC 日期算的話會說成 10/19。"""
    late = datetime(2026, 10, 18, 17, 0, tzinfo=timezone.utc)
    error = _check(ids, {"date": "2026-10-20", "weekday": "一", "party_size": 2},
                   now=late)["error"]
    assert "下週一,是 2026-10-26;" in error


def test_the_clinic_checks_the_weekday_too(ids):
    result = _create(ids, {**CLINIC_ARGS, "date": "2026-10-10"}, config=CLINIC)   # 說週五、日期是週六
    assert result["ok"] is False and "2026-10-10 是週六,不是週五" in result["error"]
    assert _rows() == []


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
