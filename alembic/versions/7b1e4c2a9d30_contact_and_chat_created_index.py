"""contact and chat_histories created_at index

Revision ID: 7b1e4c2a9d30
Revises: 3f2a9c1d7b4e
Create Date: 2026-10-05 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7b1e4c2a9d30'
down_revision: Union[str, Sequence[str], None] = '3f2a9c1d7b4e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 可為空,不需要 server_default:既有資料列就是 NULL,重跑 seed 才填上。
    # batch_alter_table:SQLite 的 DROP COLUMN 要靠 batch 模式重建表(downgrade 用得到)。
    with op.batch_alter_table("companies") as batch:
        batch.add_column(sa.Column("contact", sa.String(length=200), nullable=True))
    op.create_index("ix_chat_created", "chat_histories", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_chat_created", table_name="chat_histories")
    with op.batch_alter_table("companies") as batch:
        batch.drop_column("contact")
