"""bookings and companies.booking_config

Revision ID: c5e8a1f3d920
Revises: 7b1e4c2a9d30
Create Date: 2026-10-07 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c5e8a1f3d920'
down_revision: Union[str, Sequence[str], None] = '7b1e4c2a9d30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 可為空:既有的公司是 NULL(不提供預約),重跑 seed 才會填上。
    # batch_alter_table:SQLite 的 DROP COLUMN 要靠 batch 模式重建表(downgrade 用得到)。
    with op.batch_alter_table("companies") as batch:
        batch.add_column(sa.Column("booking_config", sa.JSON(), nullable=True))
    # 新表沒有既有資料列,所以 NOT NULL 欄位不需要 server_default
    op.create_table(
        "bookings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("company_id", sa.String(length=36),
                  sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(length=36),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("booking_date", sa.Date(), nullable=False),
        sa.Column("booking_time", sa.String(length=5), nullable=False),
        sa.Column("party_size", sa.Integer(), nullable=False),
        sa.Column("extra", sa.JSON(), nullable=False),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("phone", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_bookings_slot", "bookings",
                    ["company_id", "booking_date", "booking_time"])


def downgrade() -> None:
    op.drop_index("ix_bookings_slot", table_name="bookings")
    op.drop_table("bookings")
    with op.batch_alter_table("companies") as batch:
        batch.drop_column("booking_config")
