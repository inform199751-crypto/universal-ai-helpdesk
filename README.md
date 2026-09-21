# AI 智慧客服(萬用)

> Universal AI Helpdesk — 企業把自己的資料放進一個資料夾,不改任何一行程式碼,
> 就得到一個掛在 LINE 官方帳號上的 AI 客服。

```
industries/<行業>/  ←  五份 YAML,企業帶入的資料
        │
        │  python build.py --industry <行業>
        ▼
   dist/<行業>_客服.yml  →  匯入 Dify
        │
  LINE 官方帳號 ◄── Cloudflare Worker ──► Dify Cloud
```

換行業 = 複製一個資料夾、改裡面的 YAML、重跑 build。**沒有任何一行 per-company 的程式碼。**

## 三個示範行業

| 行業 | 它壓測到什麼 |
|---|---|
| 餐飲 | 基準線 |
| 電商 | 退換貨、物流、付款這類規則密集的問答 |
| 診所 | 高風險場域 —— 不能給醫療建議、必須轉真人 |

## 狀態

**設計完成,尚未實作。** 設計文件:[docs/superpowers/specs/2026-09-21-design.md](docs/superpowers/specs/2026-09-21-design.md)

## 命名說明

顯示名稱是「AI 智慧客服(萬用)」。目錄、repo 與 Cloudflare Worker 使用
`universal-ai-helpdesk` —— Worker 名稱只允許小寫英數與連字號,而中文網址貼給別人時
會變成一長串百分號編碼。
