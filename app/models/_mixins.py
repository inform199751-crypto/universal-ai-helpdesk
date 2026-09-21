"""四個 model 檔案共用的小工具:`_uuid()` 與 `TimestampMixin`。

拆成四個 model 檔案時,這兩段原本在 schema 文件裡只出現一次。若各自複製一份,
會變成三個一模一樣、卻是不同類別的 TimestampMixin —— 改時間策略要記得改三個
地方,改一半就會有兩張表悄悄留在舊行為,而且因為是三個不同的類別,也沒辦法用
`isinstance` 之類的方式一起處理。集中在這裡,`company.py`/`user.py`/
`knowledge.py` 都從這裡 import,只有一份真正的定義。

`chat_histories` 不用這裡的 TimestampMixin:訊息是不可變的紀錄,只有
`created_at`,沒有 `updated_at`(見 app/models/chat.py)。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column


def _uuid() -> str:
    return str(uuid.uuid4())


class TimestampMixin:
    # 決策 8:時間一律存 UTC。DateTime(timezone=True) 在 PostgreSQL 是
    # timestamptz(內部正規化成 UTC),SQLite 不真的保存時區,但 func.now()
    # 在兩邊都回傳 UTC(SQLite 的 CURRENT_TIMESTAMP 定義就是 UTC)—— 兩邊
    # 一致,雙資料庫相容。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
