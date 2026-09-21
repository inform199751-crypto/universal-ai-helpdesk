"""companies —— 租戶。一列 = 一家企業 = 一個 LINE 官方帳號。

決策依據見 docs/superpowers/specs/2026-09-21-schema-draft.md 第一節。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, DateTime, Integer, LargeBinary, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.knowledge import KnowledgeDocument
    from app.models.user import User


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


class Company(Base, TimestampMixin):
    """租戶。一列 = 一家企業 = 一個 LINE 官方帳號。"""

    __tablename__ = "companies"

    # 決策 8:UUID 存 String(36),不用 PostgreSQL 原生 UUID 型別 ——
    # SQLite 沒有這個型別,同一份 model 兩邊都要能跑。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)

    # webhook 路徑用:/webhook/{slug}。決策 2 —— LINE Console 本來就要各自填
    # webhook URL,零額外成本;不用逐一試簽章(O(n) 次 HMAC,而且驗簽失敗是
    # 正常情況,log 會被洗版),也不能只信 body 裡的 destination
    # (那等於在驗證前就信任了 body)。
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    industry: Mapped[str] = mapped_column(String(64), nullable=False)

    # --- LINE 憑證(決策 3:密文,不是明文)---
    # channel_secret / access_token 等於別人官方帳號的控制權。資料庫備份、
    # SELECT * 的截圖、不小心 commit 的 dump —— 任何一次外洩都是別人家的
    # 帳號被接管。存 app.crypto.encrypt() 的密文,型別是 LargeBinary。
    line_channel_id: Mapped[str | None] = mapped_column(String(64))
    line_channel_secret_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    line_channel_token_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # bot 自己的 userId。決策 2:webhook body 的 destination 要跟這個對得上,
    # 路徑說是 A 公司、body 的 destination 卻是 B 公司的 bot,就拒絕。
    line_destination: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)

    # --- 客服人設 ---
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tone: Mapped[str] = mapped_column(String(64), nullable=False, default="專業親切")
    # 禁語清單。JSON 在 PG 與 SQLite(3.9+)都支援,雙資料庫相容。
    forbidden_phrases: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    fallback_message: Mapped[str] = mapped_column(
        Text, nullable=False, default="我這邊剛剛連線有點問題,可以再問一次嗎?"
    )

    # 決策 4:HUMAN 模式一定要能自動歸還。轉真人之後如果客服下班忘記切回
    # AI,那位客人就永遠等不到任何回覆(AI 不理他,真人也不在)。這個欄位
    # 搭配 users.mode_expires_at 一起用:進 HUMAN 模式時設一個期限,逾時
    # 自動回到 AI。
    human_mode_timeout_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)

    # 決策 7:向量庫「一家一個 collection」,不是同一個 collection 用
    # company_id 過濾 —— 過濾法只要有一次查詢忘記加 filter,就會把別家
    # 公司的文件內容回給客人,而且這種 bug 不會報錯,只會安靜地外洩。
    # 分 collection 讓這類 bug 不可能發生。
    vector_collection: Mapped[str] = mapped_column(String(128), nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    users: Mapped[list["User"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    documents: Mapped[list["KnowledgeDocument"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
