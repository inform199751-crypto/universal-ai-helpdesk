"""轉真人:規則層與 agent 層共用的判斷與狀態變更。

兩層走同一套函式,行為才保證一致 —— 不會出現「關鍵字轉的會通知、
模型轉的不會」。這個模組不開 session、不送訊息,只做決定與改 User
物件;送出去是 webhook 的事。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.models import ConversationMode, User

TOOL_NAME = "transfer_to_human"
# 模型呼叫了工具、類別卻不認得時回的話。不能沿用任何一類的 script ——
# 猜錯類別的話,客人會聽到跟他的狀況無關的承諾。
GENERIC_SCRIPT = "這部分我請專人與您聯繫,請稍候。"
EXPIRED_PREFIX = "專員目前不在線上,我先幫您處理。"
CATEGORY_LABELS = {"safety": "安全", "legal": "法律", "money": "帳務", "privacy": "個資"}


@dataclass(frozen=True)
class Decision:
    category: str | None   # None:模型給了不認得的類別
    script: str            # 回給客人的話
    basis: str             # 給店員看的判斷依據 —— 規則轉的還是模型轉的


def _transfer_rules(rules: list[dict]) -> list[dict]:
    return [r for r in rules if r.get("action") == "transfer"]


def _words(trigger: str) -> list[str]:
    # 空字串要濾掉:trigger 寫成「過敏、」時會切出 "",而 "" in 任何字串
    # 都是 True —— 每一則訊息都會被轉真人。
    return [w.strip() for w in (trigger or "").split("、") if w.strip()]


def match_keyword(rules: list[dict], text: str) -> Decision | None:
    """第一條命中的 transfer 規則。yaml 裡 L3 排在前面,所以安全優先。"""
    for rule in _transfer_rules(rules):
        for word in _words(rule.get("trigger", "")):
            if word in text:
                return Decision(rule.get("category"), rule["script"], f"關鍵字「{word}」")
    return None


def build_tool(rules: list[dict]) -> dict | None:
    """從升級規則產生工具定義。換行業時自動跟著變。"""
    transfer = _transfer_rules(rules)
    if not transfer:
        return None
    hints = ";".join(f"{r['category']}({r.get('trigger', '')})" for r in transfer)
    return {"type": "function", "function": {
        "name": TOOL_NAME,
        "description": ("客人的狀況需要真人處理時呼叫,不要自己回答。"
                        f"類別:{hints}。意思相近、換句話說的也算。"),
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string",
                             "enum": [r["category"] for r in transfer]},
                "reason": {"type": "string", "description": "一句話說明為什麼要轉"},
            },
            "required": ["category"],
        },
    }}


def decision_from_tool(rules: list[dict], arguments: dict) -> Decision:
    """模型呼叫了工具。類別不認得也照樣轉 —— 模型已經表達「需要真人」。"""
    reason = str(arguments.get("reason") or "").strip() or "(模型沒有說明)"
    category = arguments.get("category")
    for rule in _transfer_rules(rules):
        if rule.get("category") == category:
            return Decision(category, rule["script"], f"AI —— {reason}")
    return Decision(None, GENERIC_SCRIPT, f"AI —— {reason}")


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite 讀回來的 datetime 沒有時區(實測 tzinfo=None),直接跟
    datetime.now(timezone.utc) 比會 TypeError。PostgreSQL 讀回來有時區,
    所以只在 PG 上測會漏掉這一條。"""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def is_still_human(user: User, now: datetime) -> bool:
    if user.mode != ConversationMode.HUMAN:
        return False
    expires = as_utc(user.mode_expires_at)
    # NULL = 不會自動切換(schema 決策 4)。這裡的 handoff 一定會設期限,
    # NULL 只會出現在有人手動改資料庫的時候。
    return expires is None or now < expires


def enter_human_mode(user: User, *, timeout_minutes: int, now: datetime) -> None:
    user.mode = ConversationMode.HUMAN
    user.mode_changed_at = now
    user.mode_expires_at = now + timedelta(minutes=timeout_minutes)


def return_to_ai(user: User, now: datetime) -> None:
    user.mode = ConversationMode.AI
    user.mode_changed_at = now
    user.mode_expires_at = None


def customer_label(user: User) -> str:
    return user.display_name or f"客人 …{user.line_user_id[-6:]}"


def staff_message(decision: Decision, customer: str, text: str) -> str:
    label = CATEGORY_LABELS.get(decision.category or "", "轉真人")
    return f"【{label}】{customer}\n{text[:100]}\n判斷依據:{decision.basis}"
