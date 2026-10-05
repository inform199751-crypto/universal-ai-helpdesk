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
