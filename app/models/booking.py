"""bookings —— 預約。設計見 docs/superpowers/specs/2026-10-06-booking-agent-design.md 第四節。"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        # 查名額:某家公司、某天、某時段有幾筆
        Index("ix_bookings_slot", "company_id", "booking_date", "booking_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    # 哪位 LINE 客人訂的。同一人同一時段只會有一筆,但那是程式層的檢查(app/booking.py
    # 在公司列的鎖裡先查再寫),資料表沒有 unique 限制 —— 手動寫 SQL 不受保護。
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    booking_date: Mapped[date] = mapped_column(Date, nullable=False)
    booking_time: Mapped[str] = mapped_column(String(5), nullable=False)  # HH:MM
    party_size: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # booking.yaml 的 fields(例如診所的看診項目)
    extra: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)  # 去掉 - 與空白
    # 先只有 confirmed;欄位預留給之後的取消
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="confirmed")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
