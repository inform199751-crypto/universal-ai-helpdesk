"""users —— LINE 使用者。同一個人在不同公司的官方帳號下是兩列。

決策依據見 docs/superpowers/specs/2026-09-21-schema-draft.md 第一節。
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.chat import ChatHistory
    from app.models.company import Company


def _uuid() -> str:
    return str(uuid.uuid4())


class TimestampMixin:
    # 決策 8:時間一律存 UTC(理由見 app/models/company.py 的 TimestampMixin)。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


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
    mode: Mapped[ConversationMode] = mapped_column(
        Enum(ConversationMode, native_enum=False, length=8),
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
