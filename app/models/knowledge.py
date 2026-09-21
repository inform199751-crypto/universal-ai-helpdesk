"""knowledge_documents —— 知識庫文件的 metadata。實際的向量在 Qdrant,這裡只記關聯。

決策依據見 docs/superpowers/specs/2026-09-21-schema-draft.md 第一節與第三節。
"""

from __future__ import annotations

import enum
import uuid
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


class DocumentStatus(str, enum.Enum):
    PENDING = "pending"
    INDEXING = "indexing"
    READY = "ready"
    FAILED = "failed"


class KnowledgeDocument(Base, TimestampMixin):
    """知識庫文件的 metadata。實際的向量在 Qdrant,這裡只記關聯。"""

    __tablename__ = "knowledge_documents"
    __table_args__ = (
        # 同一份文件重傳不要重複建索引
        UniqueConstraint("company_id", "checksum", name="uq_doc_company_checksum"),
        Index("ix_doc_company_status", "company_id", "status"),
    )

    # 這個 id 會寫進 Qdrant 每個 point 的 payload。刪除整份文件靠它
    # delete by filter(決策 7)。不存 point id 陣列:一份 200 chunk 的
    # PDF 會塞一個 200 元素的陣列進欄位,而且重新索引後全部作廢。
    # 決策 8:存 String(36) 不用 PG 原生 UUID 型別 —— 兩邊都要能跑。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )

    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)  # file / url / manual
    source_uri: Mapped[str | None] = mapped_column(String(1000))
    # sha256,用來擋重複上傳
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)

    vector_collection: Mapped[str] = mapped_column(String(128), nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 換嵌入模型(例如從 bge-m3 換成 text-embedding-3-small)維度會不同,
    # 舊向量全部作廢。不記錄當初用哪個模型建的,之後只會得到「查詢結果
    # 很怪但不報錯」這種最難查的故障(見 schema 文件第三節)。
    embedding_model: Mapped[str | None] = mapped_column(String(128))

    # 決策 8:native_enum=False —— SQLite 沒有 ENUM 型別,兩邊都落成 VARCHAR。
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, native_enum=False, length=16),
        nullable=False,
        default=DocumentStatus.PENDING,
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    company: Mapped["Company"] = relationship(back_populates="documents")
