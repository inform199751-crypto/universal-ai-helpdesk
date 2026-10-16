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


# 「最近的週X」與「下週X」prompt 那兩行和星期不符的錯誤都用這兩個函式:兩邊各算一份的話,
# 哪天改了一邊,模型會在 prompt 看到一個日期、被工具指到另一個日期。

def _nearest(today: date, weekday: str) -> date:
    """從今天起(含今天)最近的週X。"""
    return today + timedelta(days=(WEEKDAYS.index(weekday) - today.weekday()) % 7)


def _next_week(today: date, weekday: str) -> date:
    """下週X = 這一週(週一到週日)之後那一週的週X,不是「最近的週X 再加 7 天」。
    今天 10/15(四)或 10/17(六),下週五都是 10/23。"""
    next_monday = today + timedelta(days=7 - today.weekday())
    return next_monday + timedelta(days=WEEKDAYS.index(weekday))


def _norm_weekday(raw) -> str | None:
    """把模型填的 weekday 收成 WEEKDAYS 裡的一個字,認不得就回 None。

    enum 只列「一」到「日」,但模型不一定照 enum 填(可能寫成「週五」「星期天」),
    拒絕這些只會多繞一圈;「天」就是「日」。長度一定要等於 1 —— 空字串與「一二」
    都是 WEEKDAYS 的子字串,不擋的話會被當成有效。
    """
    w = str(raw or "").strip()
    for prefix in ("星期", "禮拜", "週", "周"):
        if w.startswith(prefix):
            w = w[len(prefix):]
            break
    if w == "天":
        w = "日"
    return w if len(w) == 1 and w in WEEKDAYS else None


def _asks_party(config: dict) -> bool:
    return config["max_party"] > 1


def _fail(error: str) -> dict:
    return {"ok": False, "error": error}


# --- 工具定義與預約說明 ---------------------------------------------------------

def build_tools(config: dict) -> list[dict]:
    """由 booking_config 產生兩個工具:換行業時自動跟著變,跟轉真人工具同一套做法。"""
    label = config["label"]
    date_param = {"type": "string", "description": "YYYY-MM-DD,照日期對照表換算"}
    # 日期與星期是否一致由程式檢查:關掉思考後,模型不查表、自己心算,會把「週五」訂成週六。
    # 要它同時交代「那天是星期幾」,程式拿來跟日期對 —— 兩邊對不上就擋(_parse_date)。
    weekday_param = {"type": "string", "enum": list(WEEKDAYS),
                     "description": "那天是星期幾:客人說星期幾就填那個;客人只給日期就照日期對照表查"}
    party_param = {"type": "integer", "minimum": 1, "maximum": config["max_party"],
                   "description": "人數"}
    check_props: dict = {"date": date_param, "weekday": weekday_param}
    create_props: dict = {
        "date": date_param,
        "weekday": weekday_param,
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
    給它一張表照著查就不會。但關掉思考後它連表都不查,所以另外多一行「星期→日期」
    直接查(一個映射、不必在 15 天裡數),工具那邊再用 weekday 擋最後一道。
    「下週五」另給一行(下週一到下週日):只有「最近」那行可查的話,模型會被帶到這週。
    """
    label = config["label"]
    open_days = set(config["days"])
    last = today + timedelta(days=config["advance_days"])
    span = (today + timedelta(days=n) for n in range(config["advance_days"] + 1))
    table = "、".join(
        f"{d.isoformat()}({_weekday(d)}{'' if _weekday(d) in open_days else ',公休'})"
        for d in span)
    # 每個星期各取最近的那天、依日期排:今天排第一,跟對照表同一個方向
    nearest = "、".join(
        f"週{_weekday(d)}→{d.isoformat()}" + ("(今天)" if d == today else "")
        + ("" if _weekday(d) in open_days else "(公休)")
        for d in sorted(_nearest(today, w) for w in WEEKDAYS))
    # 下週可能超出可預約的天數:標出來,模型才不會照這行去查、再被工具擋一次
    next_week = "、".join(
        f"週{_weekday(d)}→{d.isoformat()}" + ("" if _weekday(d) in open_days else "(公休)")
        + (f"(超出可{label}範圍)" if d > last else "")
        for d in (_next_week(today, w) for w in WEEKDAYS))
    needs = ["日期", "時段"]
    if _asks_party(config):
        needs.append("人數")
    needs += [f["label"] for f in config.get("fields") or []]
    needs += ["姓名", "電話"]
    lines = [
        f"## {label}",
        f"今天是 {today.isoformat()}({_weekday(today)}),時區是台灣。",
        f"客人只說星期幾時,直接用這一行(從今天起最近的那一天):{nearest}",
        f"客人說「下週」「下禮拜」時,用這一行(下週一到下週日):{next_week}",
        f"可以{label}的日期對照(換算日期一律照這張表,不要自己推算):{table}",
        f"時段:{'、'.join(config['times'])}。",
        "- 客人只說星期幾(例如「週五」)而沒有特別說明時,就是從今天起算最近的那一天,"
        "不要反問是哪一週。",
        "- 客人說「下週X」「下禮拜X」時,用下週那一行的日期;確認時把日期講出來。",
        f"- 建立{label}前一定要先用 {CHECK_TOOL} 查空位,依查詢結果回答;不可以自己編造空位。",
        f"- 呼叫 {CHECK_TOOL}、{CREATE_TOOL} 時,weekday 填客人說的星期幾;"
        "客人只給日期時,照對照表查那天是星期幾再填。",
        "- 工具說日期和星期對不上時,回頭看客人說的是星期幾還是日期,"
        "照客人說的那個改正後再查;不要自己猜。",
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

def _parse_date(config: dict, raw, weekday, now: datetime) -> tuple[date | None, str | None]:
    label = config["label"]
    try:
        d = date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None, "日期格式要是 YYYY-MM-DD"
    given = _norm_weekday(weekday)
    if given is None:
        return None, "請一併提供那天是星期幾(一到日)"
    today = today_taipei(now)
    # 星期對不上要排在範圍與公休之前:日期本身就錯了,先講「公休」或「超出範圍」
    # 會把模型帶偏到那個日期上。錯誤每種讀法都講 —— 客人說的是星期,就改成最近的那天;
    # 說的是下週X,就是下週那天(只給最近的那天會把模型導到這週);說的是日期,只是
    # weekday 填錯。不能一律「往最近的星期靠」,那會訂到客人沒說的日子。
    if _weekday(d) != given:
        return None, (f"{d.isoformat()} 是週{_weekday(d)},不是週{given}。"
                      f"如果客人說的是週{given},"
                      f"從今天起最近的週{given}是 {_nearest(today, given).isoformat()};"
                      f"如果客人說的是下週{given},是 {_next_week(today, given).isoformat()};"
                      f"如果客人說的是 {_md(d)},weekday 請填「{_weekday(d)}」。")
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
    d, error = _parse_date(config, arguments.get("date"), arguments.get("weekday"), now)
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
    d, error = _parse_date(config, arguments.get("date"), arguments.get("weekday"), now)
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
