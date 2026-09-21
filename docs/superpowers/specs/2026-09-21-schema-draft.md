# 資料庫 Schema(FastAPI + SQLAlchemy)

日期:2026-09-21
狀態:**已定案**(2026-09-21)。前提架構見本文第四節。

相容目標:PostgreSQL(正式)/ SQLite(本機開發與測試),同一份 model 兩邊都能跑。

---

## 一、八個不顯眼但會出事的決策

在看 model 之前先看這些。這些是整份 schema 真正的內容,欄位本身反而是次要的。

### 1. `line_user_id` 不能單獨設 unique

LINE 的 userId **只在同一個 channel 內唯一**。同一個人加了兩家公司的官方帳號,
會拿到兩個不同的 userId;而理論上不同 channel 也可能出現相同字串。

唯一鍵必須是 `(company_id, line_user_id)`。設成單欄 unique 的話,第二家公司的客人
會在插入時撞鍵,而錯誤訊息看起來會像是「這個人已經存在」—— 查很久都查不到真正原因。

### 2. webhook 要怎麼知道這則訊息是哪一家公司的

三種做法,只有一種是對的:

| 做法 | 評價 |
|---|---|
| 每家公司一個路徑 `/webhook/{slug}` | ✅ **採用**。LINE Console 那邊本來就要各自填 webhook URL,零額外成本 |
| 拿每家的 channel_secret 逐一試簽章 | ❌ O(n) 次 HMAC,而且驗簽失敗是正常情況,log 會被洗版 |
| 只靠 body 裡的 `destination` | ⚠️ 可以,但要先 parse 才知道用誰的 secret 驗簽 —— **等於在驗證前就信任了 body** |

採用路徑法,同時把 `destination`(bot 自己的 userId)存起來**交叉比對**:
路徑說是 A 公司、body 的 destination 卻是 B 公司的 bot,就拒絕。

### 3. LINE 憑證必須加密存,不能是明文欄位

`channel_secret` 和 `access_token` 等於別人官方帳號的控制權。資料庫備份、
`SELECT *` 的截圖、不小心 commit 的 dump —— 任何一次外洩都是別人家的帳號被接管。

用 `cryptography` 的 Fernet 對稱加密,主金鑰放環境變數。欄位型別是 `LargeBinary`。

> 這一條在面試時值得主動講。多租戶 SaaS 存客戶憑證是最基本的考題,
> 明文存會直接被判定沒有安全意識。

### 4. `HUMAN` 模式一定要能自動歸還

轉真人之後,如果客服下班忘記切回 AI,那位客人就**永遠等不到任何回覆** ——
AI 不理他,真人也不在。這是真實客服系統最常見的災難。

所以 `mode` 之外還要有 `mode_expires_at`:進 HUMAN 模式時設一個期限(例如 30 分鐘),
真人每回一句就延長;逾時自動回到 AI,並主動跟客人說一句「專員目前不在線上,
我先幫您處理」。

### 5. 真人講的話要存,但角色不同

進 HUMAN 模式時客服打的字也要寫進 `chat_histories`,否則切回 AI 之後,
AI 完全不知道剛剛真人跟客人講了什麼,會從頭問一次 —— 客人會爆炸。

所以 role 除了 `user` / `assistant` / `system`,要多一個 `human_agent`。
餵給 LLM 時把它當成 assistant,但資料上分得開(才統計得出真人介入率)。

### 6. 去重靠 `line_message_id` 的 unique 索引,不要自己寫判斷

LINE 沒收到 2xx 會重送。與其在程式裡查一次再寫一次(有 race condition),
不如讓資料庫的 unique 索引擋掉:插入時撞鍵就代表處理過了,直接跳過。

### 7. 向量庫不要存 point id 陣列

`knowledge_documents` 只存 `document_id`(就是主鍵)。寫進 Qdrant 時把它放進 payload,
要刪除整份文件就 **delete by filter**。存 point id 陣列的話,一份 200 chunk 的 PDF
會塞一個 200 元素的陣列進欄位,而且重新索引後全部作廢。

**租戶隔離用「一家公司一個 collection」,不是同一個 collection 用 company_id 過濾。**
理由是故障模式:過濾法只要有一次查詢忘記加 filter,就會把別家公司的文件內容
回給客人 —— 這種 bug 不會報錯,只會安靜地外洩。分 collection 讓這類 bug 不可能發生。

### 8. SQLite 預設不檢查外鍵

`PRAGMA foreign_keys=ON` 不開的話,SQLite 的 `ForeignKey` 宣告形同虛設 ——
本機測試全過、上了 PostgreSQL 才發現一堆孤兒資料。必須掛 event listener 強制開啟。

其餘可攜性處理:UUID 用 `String(36)` 不用 PG 原生型別、Enum 用 `native_enum=False`
(SQLite 沒有 ENUM,會落成 VARCHAR + CHECK)、時間一律存 UTC
(SQLite 不真的保存時區)。

---

## 二、ORM Models

```python
"""app/models.py —— PostgreSQL / SQLite 雙相容。"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


# 決策 8:SQLite 預設不檢查外鍵,不開這個等於沒宣告過 ForeignKey
@event.listens_for(Engine, "connect")
def _enable_sqlite_fk(dbapi_connection, _record):
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# --- 1. companies -----------------------------------------------------------

class Company(Base, TimestampMixin):
    """租戶。一列 = 一家企業 = 一個 LINE 官方帳號。"""

    __tablename__ = "companies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)

    # webhook 路徑用:/webhook/{slug}。決策 2。
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    industry: Mapped[str] = mapped_column(String(64), nullable=False)

    # --- LINE 憑證(決策 3:密文,不是明文)---
    line_channel_id: Mapped[str | None] = mapped_column(String(64))
    line_channel_secret_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    line_channel_token_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # bot 自己的 userId。webhook body 的 destination 要跟這個對得上,否則拒絕。
    line_destination: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)

    # --- 客服人設 ---
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tone: Mapped[str] = mapped_column(String(64), nullable=False, default="專業親切")
    # 禁語清單。JSON 在 PG 與 SQLite(3.9+)都支援。
    forbidden_phrases: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    fallback_message: Mapped[str] = mapped_column(
        Text, nullable=False, default="我這邊剛剛連線有點問題,可以再問一次嗎?"
    )

    # 轉真人後多久自動回到 AI(決策 4)
    human_mode_timeout_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)

    # 向量庫:一家一個 collection(決策 7)
    vector_collection: Mapped[str] = mapped_column(String(128), nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    users: Mapped[list["User"]] = relationship(back_populates="company", cascade="all, delete-orphan")
    documents: Mapped[list["KnowledgeDocument"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )


# --- 2. users ---------------------------------------------------------------

class ConversationMode(str, enum.Enum):
    AI = "AI"
    HUMAN = "HUMAN"


class User(Base, TimestampMixin):
    """LINE 使用者。同一個人在不同公司的官方帳號下是兩列。"""

    __tablename__ = "users"
    __table_args__ = (
        # 決策 1:LINE userId 只在同一個 channel 內唯一
        UniqueConstraint("company_id", "line_user_id", name="uq_users_company_line_user"),
        # 「這家公司現在有誰在等真人」—— 客服後台的主要查詢
        Index("ix_users_company_mode", "company_id", "mode"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    line_user_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # 來自 LINE profile API,可能取不到(客人封鎖過官方帳號時)
    display_name: Mapped[str | None] = mapped_column(String(200))
    picture_url: Mapped[str | None] = mapped_column(String(500))

    mode: Mapped[ConversationMode] = mapped_column(
        Enum(ConversationMode, native_enum=False, length=8),
        nullable=False,
        default=ConversationMode.AI,
    )
    mode_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 決策 4:逾時自動回到 AI。NULL = 不會自動切換。
    mode_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    company: Mapped["Company"] = relationship(back_populates="users")
    messages: Mapped[list["ChatHistory"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


# --- 3. chat_histories ------------------------------------------------------

class ChatRole(str, enum.Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    HUMAN_AGENT = "human_agent"  # 決策 5:真人講的,餵 LLM 時當 assistant


class ChatHistory(Base):
    """對話紀錄。供 LLM 短期記憶,也是之後做分析報表的資料來源。"""

    __tablename__ = "chat_histories"
    __table_args__ = (
        # 唯一的高頻查詢:取這個人最近 N 則
        Index("ix_chat_user_created", "user_id", "created_at"),
        # 決策 6:靠資料庫擋 LINE 重送,不要自己寫「先查再寫」
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

    # 只有 role=user 的列會有值(assistant 的訊息沒有 LINE message id)
    line_message_id: Mapped[str | None] = mapped_column(String(64))

    # 觀測用:回應延遲與 token 數。沒有這兩欄就不知道系統慢在哪、花了多少錢。
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    token_count: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="messages")


# --- 4. knowledge_documents -------------------------------------------------

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

    # 這個 id 會寫進 Qdrant 每個 point 的 payload。刪除整份文件靠它 delete by filter。
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
    # 換嵌入模型必須重建索引,所以要記住當初是用哪一個建的
    embedding_model: Mapped[str | None] = mapped_column(String(128))

    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, native_enum=False, length=16),
        nullable=False,
        default=DocumentStatus.PENDING,
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    company: Mapped["Company"] = relationship(back_populates="documents")
```

---

## 三、`embedding_model` 為什麼要存

換嵌入模型(例如從 `bge-m3` 換成 `text-embedding-3-small`)**維度會不同,舊向量全部作廢**。
不記錄當初用哪個模型建的,之後只會得到「查詢結果很怪但不報錯」這種最難查的故障。
既有 RAG 專案已經踩過這個坑(換嵌入模型必須同時換 collection 名稱)。

---

## 四、前提(已定案)

本文假設的架構已於 2026-09-21 定案:**FastAPI + 自建 Agent + 資料庫**,
取代原先的 Cloudflare Worker + Dify。決策理由見
[2026-09-21-design.md](2026-09-21-design.md) 的改版紀錄與決策 1。

但 **v1 只服務一家企業**,不是完整多租戶:

| | v1 實作 | schema 是否已支援 |
|---|---|---|
| 租戶 | `companies` 只有一列 | ✅ 不用改表就能開多家 |
| 真人接手 | 不做介面,欄位不寫入 | ✅ `mode` / `mode_expires_at` 已備妥 |
| 知識庫 | 不用 RAG,YAML 內容直接進 system prompt | ✅ `knowledge_documents` 已備妥,v1 空表 |

換句話說,**這份 schema 是照終局設計的,v1 只是還沒用滿。**
這樣做的代價是三張欄位暫時用不到,換來的是之後開多租戶與 RAG 時不用做資料遷移。

**仍待決:**

1. 託管平台(Render / Railway / Fly.io)與是否付費 —— 免費方案休眠會讓面試第一則訊息沒反應
2. 嵌入模型(影響 `embedding_model` 與 collection 命名),v1 用不到,做 RAG 時才需要
