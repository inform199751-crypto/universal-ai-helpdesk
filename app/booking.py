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
    # 改、取消不在這次範圍內(spec §一)。不講的話,客人說「改成 4 位」時模型會再呼叫一次
    # create_booking,拿到重複的 ok 就回「已幫您改好」—— 資料其實沒動。
    changes = "、".join((["人數"] if _asks_party(config) else []) + ["時間"])
    lines.append(f"- 已成立的{label}不能在這裡修改或取消;客人要改{changes}或取消,"
                 f"請他直接聯繫 {contact or '店家'}。")
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

    # 先鎖住這家公司的列,再做防重複與名額 —— 同一位客人同時送兩次、或兩個人搶最後一個名額,
    # 第二個請求都要等第一個 commit 完才看得到結果(PostgreSQL)。FOR NO KEY UPDATE 就夠了,
    # 不會擋住其他資料表對 companies 的外鍵寫入。SQLite 只用在開發與測試,不保證這件事。
    db.execute(select(Company.id).where(Company.id == company_id).with_for_update(key_share=True))

    # 防重複:模型偶爾會重複呼叫 —— 同一位客人同一時段只會有一筆
    existing = db.scalar(select(Booking).where(
        Booking.company_id == company_id, Booking.user_id == user_pk,
        Booking.booking_date == d, Booking.booking_time == slot,
        Booking.status == CONFIRMED))
    if existing is not None:
        # 客人可能是想改人數:明講沒有改,模型才不會回「已幫您改好」
        return {"ok": True, "duplicate": True, "booking": _public(existing),
                "note": f"這位客人這個時段已經有{label},內容沒有更改;"
                        f"要修改或取消請客人直接聯繫 {contact or '店家'}"}

    # 名額:在上面那個鎖裡數 —— 搶最後一個名額的第二個人看到的是已滿
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
