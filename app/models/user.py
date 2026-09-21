"""users —— LINE 使用者。同一個人在不同公司的官方帳號下是兩列。

決策依據見 docs/superpowers/specs/2026-09-21-schema-draft.md 第一節。
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models._mixins import TimestampMixin, _uuid

if TYPE_CHECKING:
    from app.models.chat import ChatHistory
    from app.models.company import Company


class ConversationMode(str, enum.Enum):
    AI = "AI"
    HUMAN = "HUMAN"


class User(Base, TimestampMixin):
    """LINE 使用者。同一個人在不同公司的官方帳號下是兩列。"""

    __tablename__ = "users"
    __table_args__ = (
        # 決策 1:LINE 的 userId 只在同一個 channel 內唯一。同一個人加了
        # 兩家公司的官方帳號,會拿到兩個不同的 userId;不同 channel 也可能
        # 出現相同字串。唯一鍵必須是 (company_id, line_user_id) 這組合,
        # 設成單欄 unique 的話,第二家公司的客人會在插入時撞鍵,錯誤訊息
        # 看起來會像是「這個人已經存在」—— 查很久都查不到真正原因。
        UniqueConstraint("company_id", "line_user_id", name="uq_users_company_line_user"),
        # 「這家公司現在有誰在等真人」—— 客服後台的主要查詢
        Index("ix_users_company_mode", "company_id", "mode"),
    )

    # 決策 8:UUID 存 String(36),不用 PG 原生型別 —— 兩邊都要能跑。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    line_user_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # 來自 LINE profile API,可能取不到(客人封鎖過官方帳號時)
    display_name: Mapped[str | None] = mapped_column(String(200))
    picture_url: Mapped[str | None] = mapped_column(String(500))

    # 決策 8:native_enum=False —— SQLite 沒有 ENUM 型別,兩邊都落成
    # VARCHAR(length),同一份 model 兩邊都要能跑。
    # values_callable:存 enum 的 value(小寫/原字串),不要存 name。沒有這個,
    # SQLAlchemy 預設存 .name(例如 "AI"),日後任何用小寫值查資料庫的 raw SQL
    # 都會安靜地撞到 0 筆,而且不會報錯。
    mode: Mapped[ConversationMode] = mapped_column(
        Enum(ConversationMode, native_enum=False, length=8,
             values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=ConversationMode.AI,
    )
    mode_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 決策 4:HUMAN 模式一定要能自動歸還 —— 進 HUMAN 模式時設一個期限
    # (例如 30 分鐘),真人每回一句就延長;逾時自動回到 AI,並主動跟客人
    # 說一句「專員目前不在線上,我先幫您處理」。NULL = 不會自動切換。
    mode_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    company: Mapped["Company"] = relationship(back_populates="users")
    messages: Mapped[list["ChatHistory"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
