"""速率限制。設計見 docs/superpowers/specs/2026-10-05-rate-limit-design.md。

計數直接從 chat_histories 算,不另外存狀態:計數跟對話紀錄是同一份資料,
部署與重啟不歸零,多 worker 也對。三條規則依序檢查,第一條不放行的就是結果 ——
個人規則在前,狂傳的人被擋的是他自己;全站計數裡每人最多算 m 則,
超過的部分不佔全站的名額。

「只提醒一次」也不另外記:超過個人上限時,看這條規則的提醒在它的窗口內
(每分鐘:60 秒內;每天:台灣今天)有沒有寫進過 chat_histories —— 沒有就提醒,
有就安靜。不能靠「計數剛好等於門檻」:非文字訊息、轉真人的 script、還在等模型的
回答都不經過這裡卻會寫進紀錄,計數會跳過那個值,客人一句提醒都沒收到就被靜音。
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select
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
    NOTIFY = "notify"    # 超過個人上限、這個窗口還沒提醒過:回一次提醒
    SILENT = "silent"    # 這個窗口已經提醒過:不回
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


def _notified(db: Session, user_pk: str, content_matches, since: datetime) -> bool:
    """這條規則的提醒在窗口內寫進過紀錄沒 —— 「只提醒一次」就靠這個,不另外記。"""
    return _count(db, ChatHistory.user_id == user_pk,
                  ChatHistory.role == ChatRole.ASSISTANT,
                  content_matches,
                  ChatHistory.created_at >= since) > 0


def _global_count(db: Session, since: datetime, m: int) -> int:
    """全站這一分鐘的 user 列,每人最多算 m 則:超過的部分他自己的上限已經擋下,
    不該再佔全站的名額。m == 0(每人每分鐘關閉)時沒有個人上限可言,用原始計數。"""
    recent = (ChatHistory.role == ChatRole.USER, ChatHistory.created_at >= since)
    if not m:
        return _count(db, *recent)
    per_user = (select(func.count().label("n"))
                .select_from(ChatHistory)
                .where(*recent)
                .group_by(ChatHistory.user_id)
                .subquery())
    capped = case((per_user.c.n > m, m), else_=per_user.c.n)
    # int():PostgreSQL 的 SUM(bigint) 回傳 numeric,到 Python 是 Decimal
    return int(db.scalar(select(func.coalesce(func.sum(capped), 0))))


def check(db: Session, *, user_pk: str, now: datetime, limits: Limits) -> Verdict:
    """判定這一則要不要放行。「這一則」必須已經 flush 進 chat_histories(同一個
    session)—— SessionLocal 是 autoflush=False,只 db.add() 還不算寫進去。

    任何例外都放行:限流是保護機制,不是主流程 —— 為了它讓正常客人收不到回答,
    違反「沉默是唯一不被接受的失敗模式」。查詢包在 savepoint 裡:PostgreSQL 上
    SQL 一出錯整個交易就作廢,不退回 savepoint 的話,呼叫端的下一個查詢就爆,
    剛寫進去的這一則也跟著 rollback —— 「放行」就變成了 fallback。
    """
    try:
        with db.begin_nested():
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
            notified = _notified(db, user_pk, ChatHistory.content == MINUTE_NOTICE, since)
            return Verdict(Action.SILENT if notified else Action.NOTIFY, "user_minute", n)

    d = limits.user_per_day
    if d:
        # 算 AI 回了幾則,不是客人傳了幾則:轉真人期間跟店員來回很多則的客人,
        # 交還 AI 後不該被擋
        today = taipei_midnight_utc(now)
        n = _count(db, ChatHistory.user_id == user_pk,
                   ChatHistory.role == ChatRole.ASSISTANT,
                   ChatHistory.created_at >= today)
        if n >= d:
            # 每天的提醒後半段隨 contact 變,比對固定的開頭
            notified = _notified(db, user_pk,
                                 ChatHistory.content.startswith(_DAY_NOTICE, autoescape=True),
                                 today)
            return Verdict(Action.SILENT if notified else Action.NOTIFY, "user_day", n)

    g = limits.global_per_minute
    if g:
        # 跨所有公司:額度綁的是同一把 key
        n = _global_count(db, since, m)
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
