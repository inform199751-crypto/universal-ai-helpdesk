# 行業資料夾對照表

**資料夾名稱就是 `--industry` 要填的值。** 用英文是刻意的:它會出現在指令列,
而中文參數在 Windows 主控台容易踩到編碼與貼上的問題。

| 資料夾 | 行業 | 公司名稱 | 它壓測到什麼 |
|---|---|---|---|
| `restaurant/` | 餐飲 | 微醺之夜 Bistro | 基準線 |
| `ecommerce/` | 電商 | 好日子生活選物 | 退換貨、物流、付款這類規則密集的問答 |
| `clinic/` | 診所 | 晴日皮膚科診所 | 高風險場域 —— 不給醫療建議、必須轉真人 |

不想翻這份表的話,直接問程式:

```bash
python -m app.cli list
```

它會掃這個資料夾,印出每個行業的公司名稱、FAQ 筆數與 ERROR 數。
**那道指令沒有任何硬編碼的行業清單** —— 加第四個行業不用改程式碼,
這裡自然會多一列。

## 每個行業裡面是什麼

五份 YAML,結構完全一樣:

| 檔案 | 內容 | 壞掉會怎樣 |
|---|---|---|
| `company.yaml` | 店名、營業時間、聯絡方式、語氣、**禁語清單** | 缺必填欄位 → 規則 2 擋下 |
| `faq.yaml` | 常見問答 | 答案命中自己的禁語 → 規則 3 擋下 |
| `policies.yaml` | 規則與政策 | 同上 |
| `escalation.yaml` | 什麼時候轉真人(L1/L2/L3) | 沒涵蓋 legal/safety/money/privacy → 規則 5 列為 BLOCK |
| `glossary.yaml` | 術語解釋 | 術語同時在禁語清單裡 → 規則 7 擋下 |

## 加一個新行業

1. 複製任何一個現有資料夾,改名(英文、小寫、連字號)
2. 改裡面五份 YAML 的內容
3. `python -m app.cli validate --industry <新名字>` → 要零 ERROR
4. `python -m app.cli seed --industry <新名字> --slug <你的 slug> --reset-history`

**沒有第 5 步。不用改任何一行程式碼。**

## 改了 YAML 之後要重新 seed

人設是在 seed 時用 Jinja2 組成 `system_prompt` 存進資料庫的,不是每次對話
即時讀 YAML。改完檔案要跑一次 seed 才會生效 —— 已存在的公司不必再給憑證:

```bash
python -m app.cli seed --industry restaurant --slug bistro
```
