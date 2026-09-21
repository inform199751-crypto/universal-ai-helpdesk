"""chat_histories —— 對話紀錄。供 LLM 短期記憶,也是之後做分析報表的資料來源。

決策依據見 docs/superpowers/specs/2026-09-21-schema-draft.md 第一節。
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.user import User


class ChatRole(str, enum.Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    HUMAN_AGENT = "human_agent"  # 決策 5:真人講的,餵給 LLM 時當 assistant


class ChatHistory(Base):
    """對話紀錄。供 LLM 短期記憶,也是之後做分析報表的資料來源。"""

    __tablename__ = "chat_histories"
    __table_args__ = (
        # 唯一的高頻查詢:取這個人最近 N 則
        Index("ix_chat_user_created", "user_id", "created_at"),
        # 決策 6:去重靠這個 unique 索引,不要自己寫「先查再寫」(有 race
        # condition)。LINE 沒收到 2xx 會重送,插入時撞鍵就代表處理過了,
        # 直接跳過即可。
        UniqueConstraint("line_message_id", name="uq_chat_line_message_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 冗餘欄位,但刻意保留:跨租戶統計不必 join users
    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    role: Mapped[ChatRole] = mapped_column(
        Enum(ChatRole, native_enum=False, length=16), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # 只有 role=user 的列會有值(assistant 的訊息沒有 LINE message id)。
    # 刻意允許 NULL:PostgreSQL 與 SQLite 的 unique 約束都允許多個 NULL 共存,
    # 不要為了「乾淨」把這欄改成 NOT NULL,那會讓第二則 assistant 回覆就撞鍵。
    line_message_id: Mapped[str | None] = mapped_column(String(64))

    # 觀測用:回應延遲與 token 數。沒有這兩欄就不知道系統慢在哪、花了多少錢。
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    token_count: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="messages")
