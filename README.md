# AI 智慧客服(萬用)

> Universal AI Helpdesk — 企業把自己的資料放進一個資料夾,不改任何一行程式碼,
> 就得到一個掛在 LINE 官方帳號上的 AI 客服。

```
industries/<行業>/*.yaml        ← 企業帶入的資料,唯一真相來源
        │
        │  python -m app.cli seed --industry <行業>
        │     ├─ validate  → ERROR 擋下 / BLOCK 列為導入議程
        │     └─ Jinja2    → system_prompt
        ▼
   PostgreSQL ── companies / users / chat_histories / knowledge_documents
        │
 LINE ──┼──► FastAPI ──► OpenRouter (LLM)
   ▲    │   /webhook/{slug}
   └─ reply / push
```

換行業 = 複製一個資料夾、改裡面的 YAML、重新 seed。**沒有任何一行 per-company 的程式碼。**

## 三個示範行業

| 行業 | 它壓測到什麼 |
|---|---|
| 餐飲 | 基準線 |
| 電商 | 退換貨、物流、付款這類規則密集的問答 |
| 診所 | 高風險場域 —— 不能給醫療建議、必須轉真人 |

## 這個專案真正的主張

不是「我用 AI 做了一個聊天機器人」,而是:

1. **換行業只換資料。** 行業是參數,不是寫死的邏輯。
2. **交付的不是機器人,是一份導入檢查清單。** `validate` 跳出來的每一條 BLOCK,
   都是導入會議上要跟客戶確認的問題 —— 不是 bug。
3. **沉默是唯一不被接受的失敗模式。** LLM 掛了、網路斷了、token 過期了,
   客人都要收到一句得體的話。

## 技術棧

FastAPI · SQLAlchemy · PostgreSQL / SQLite · Alembic · LINE Messaging API · OpenRouter

## 狀態

**設計完成,尚未實作。**

- 系統設計:[docs/superpowers/specs/2026-09-21-design.md](docs/superpowers/specs/2026-09-21-design.md)
- 資料庫設計:[docs/superpowers/specs/2026-09-21-schema-draft.md](docs/superpowers/specs/2026-09-21-schema-draft.md)

## 命名說明

顯示名稱是「AI 智慧客服(萬用)」。目錄、repo 與部署服務名使用 `universal-ai-helpdesk` ——
多數 PaaS 的服務名只允許小寫英數與連字號,而中文網址貼給別人時會變成一長串百分號編碼。
