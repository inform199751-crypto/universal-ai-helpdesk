"""escalation_rules and staff_notify_to

Revision ID: 3f2a9c1d7b4e
Revises: 66766e088730
Create Date: 2026-09-24 16:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '3f2a9c1d7b4e'
down_revision: Union[str, Sequence[str], None] = '66766e088730'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default 是必要的:companies 已經有資料列,NOT NULL 欄位沒有
    # 預設值的話 PostgreSQL 直接拒絕 ALTER TABLE。'[]' = 沒有規則,
    # 規則層與工具都不啟用,行為跟遷移前一樣 —— 重跑一次 seed 才會打開。
    #
    # batch_alter_table:SQLite 的 ALTER TABLE 不支援 DROP COLUMN,
    # downgrade 要靠 batch 模式重建表。
    with op.batch_alter_table("companies") as batch:
        batch.add_column(sa.Column("escalation_rules", sa.JSON(), nullable=False,
                                   server_default=sa.text("'[]'")))
        batch.add_column(sa.Column("staff_notify_to", sa.String(length=64),
                                   nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("companies") as batch:
        batch.drop_column("staff_notify_to")
        batch.drop_column("escalation_rules")
