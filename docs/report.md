# AI 智慧客服(萬用)—— 專題報告

> 企業把自己的資料放進一個資料夾,不改任何一行程式碼,
> 就得到一個掛在 LINE 官方帳號上的 AI 客服。

本文是專題的總報告。設計細節不在這裡重寫,而是指向原始文件:
[系統設計](superpowers/specs/2026-09-21-design.md)、
[資料庫設計](superpowers/specs/2026-09-21-schema-draft.md)、
[實作計畫](superpowers/plans/2026-09-21-helpdesk-v1.md)。

---

## 一、問題

中小企業導入 AI 客服時,真正的成本不在模型,在**每一家都要重做一次**:
問答要重寫、話術要重調、什麼時候該轉真人要重新定義。市面上的作法通常是
「幫你設定一套」,於是每多一個客戶就多一份要維護的設定,而那些設定散落在
某個平台的後台裡,沒有版本、沒有檢查、離職就失傳。

這個專題想回答的是:**能不能讓「行業」變成參數,而不是寫死的邏輯?**

判準很明確 —— 換一個行業,如果需要改任何一行程式碼,就算失敗。

## 二、主張與範圍

主張有三個,對應到系統裡具體的東西:

| 主張 | 體現在哪裡 |
|---|---|
| **換行業只換資料** | `industries/<行業>/` 五份 YAML。三個示範行業共用同一套程式碼、同一個 LINE 帳號、同一個 webhook 路徑 |
| **交付的不是機器人,是一份導入檢查清單** | `validate` 的八條規則。BLOCK 不是 bug,是導入會議上要跟客戶確認的問題 |
| **沉默是唯一不被接受的失敗模式** | LLM 掛了、上游滿載、程式爆炸,客人都要收到一句得體的話 |

v1 **明確不做**的三樣(schema 已預留欄位,之後開通不必資料遷移):
多租戶管理介面、真人接手(HUMAN 模式)介面、RAG。

## 三、關鍵設計決策

完整的決策紀錄見設計文件第二節(12 條)與資料庫文件第一節(8 條)。
這裡挑出最能代表取捨的四條:

**1. Webhook 收到就回 200,真正的工作丟背景**

LINE 沒收到 2xx 會重送,而 LLM 要跑三到十五秒。同步等必定超時,
客人會收到兩次一樣的答案。代價寫在決策 3:程序重啟會掉未完成的任務 ——
可接受,最壞情況是漏一則回覆,而 LINE 的重送機制會補上。

**2. 簽章用收到的原始位元組計算**

在 FastAPI 裡這是個陷阱:一旦宣告 Pydantic body model,框架已經 parse 過,
再序列化回去位元組就變了。而自己寫測試時兩邊用同一個序列化器,所以一定會過
—— 只有 LINE 真的傳過來才會壞。測試裡因此有一條專門證明
「重新序列化後簽章就不同」。

**3. 去重交給資料庫的 unique 索引**

自己寫「先查再寫」有 race condition。`chat_histories.line_message_id`
設 unique,插入時撞鍵就代表處理過了,直接跳過。

**4. LINE 憑證加密存,不是明文欄位**

明文等於把別人官方帳號的控制權放在 `SELECT *` 裡。資料庫備份、一次截圖、
不小心 commit 的 dump,任何一次外洩都是別人家的帳號被接管。

## 四、架構

兩張圖見 [README](../README.md):導入時(YAML → validate → Jinja2 → 資料庫)
與執行時(LINE → 驗簽 → 立刻 200 → 背景 → LLM → 回覆)。

```
industries/<行業>/*.yaml     企業帶入的資料,唯一真相來源
        │  validate 八條規則 → ERROR 擋下 / BLOCK 列為導入議程
        │  Jinja2 → system_prompt
        ▼
   companies / users / chat_histories / knowledge_documents
        │
 LINE ──┴──► FastAPI /webhook/{slug} ──► OpenRouter
```

同一份 ORM model 必須能在 PostgreSQL 與 SQLite 上跑:UUID 存 `String(36)`、
Enum 用 `native_enum=False`、時間欄位一律存 UTC。SQLite 還要用 connect event
掛上 `PRAGMA foreign_keys=ON` —— 不開的話 `ForeignKey` 宣告形同虛設,
本機測試全過,上了 PostgreSQL 才發現一堆孤兒資料。

## 五、資料層:五份 YAML 與八條檢查

每個行業就是五份 YAML:`company`(人設、營業時間、**禁語清單**)、
`faq`、`policies`、`escalation`(什麼時候轉真人)、`glossary`。

`validate` 的八條規則分兩個等級,意思不同:

| # | 檢查 | 等級 |
|---|---|---|
| 1 | 五份 YAML 都存在且可 parse | ERROR |
| 2 | 必填欄位齊全 | ERROR |
| 3 | **答案本身不可命中自己的禁語清單** | ERROR |
| 4 | 任一答案長度超過 LINE 上限 | ERROR |
| 5 | `escalation` 涵蓋四類高風險:法律 / 醫療人身安全 / 金錢爭議 / 個資 | BLOCK |
| 6 | 殘留 placeholder(`請填入` / `TODO` / `XXX`) | BLOCK |
| 7 | 術語不可與禁語清單衝突 | ERROR |
| 8 | 營業時間格式可解析 | ERROR |

**ERROR 擋 seed,BLOCK 不擋但列在報告最上方。** 這個分級是整個專題的核心主張:
交付的不是機器人,是一份導入檢查清單 —— BLOCK 跳出來的每一條,都是要跟客戶
確認的問題,不是要修的 bug。

### 診所版為什麼是壓力測試

三個示範行業裡,診所是刻意選的高風險場域。它的禁語清單包含「診斷」,
**所以連 `escalation` 的話術本身都不能出現那兩個字** —— 規則 3 在這裡才看得出
價值。實測驗證過咬合:把 safety 話術裡的「判斷」改成「診斷」,
`validate` 立刻報規則 3 並以離開碼 1 拒絕。

沒有診所這個行業,`escalation.yaml` 和規則 5 看起來只是多餘的規則。

## 六、驗證

### 自動測試

142 個測試,在 Python 3.11 與 3.13 上由 GitHub Actions 每次 push 執行。
**測試碼 1,687 行,比正式碼的 1,439 行還多。**

測試不碰網路也不碰檔案系統上的資料庫:`conftest.py` 把 `DATABASE_URL`
設成記憶體 SQLite,對外的 HTTP 一律用 `httpx.MockTransport` 或 monkeypatch
擋掉。所以 CI 不需要任何 secret,也不會因為外部服務不穩而變紅。

幾條值得一提的:

- `test_reserialized_body_produces_different_signature` —— 證明 router 不能宣告
  Pydantic body model
- `test_http_200_wrapping_an_error_object_is_treated_as_failure` —— OpenRouter
  會用 200 包著 error 回來
- `test_duplicate_image_only_replies_once` —— 非文字訊息也要走去重
- `test_validate_and_list_run_without_any_credentials` —— 用子行程 + 洗掉環境變數
  來驗,因為行程內的 conftest 早就把變數設好了

### 真機端對端

接上真的 LINE 官方帳號,用手機驗過:

| 驗證項目 | 結果 |
|---|---|
| 三個行業各自答出自己的內容 | 同一帳號、同一 webhook,只換資料夾 |
| 短期記憶 | 第二句沒提店名,它知道在講同一家店 |
| L3 升級 | 逐字照著 `escalation.yaml` 的話術,沒有自己發揮 |
| 診所拒答病情 | 回覆裡沒有出現自己的禁語「診斷」 |
| 非文字訊息 | 回固定話術,不浪費一次 LLM 呼叫 |
| 上游滿載 | 自動換模型重試,客人沒看到錯誤訊息 |

實測延遲 1,514 ms 到 14,543 ms,每則 1,588 到 2,263 tokens。
延遲的變異幾乎全部來自上游模型,不是自己這邊 —— 最快與最慢差十倍,
而程式路徑完全相同。

## 七、真實環境踩到的坑

這一節是寫測試想不出來的部分。**每一條的共同點都是:症狀指向錯的地方。**

### 1. 上游用 HTTP 200 包著錯誤回來

```
HTTP 200,body: {"error": {"message": "Upstream error from Nvidia:
Service temporarily overloaded", "code": 503}}
```

只看 `status_code` 會判成成功,然後把空字串當答案送給客人。判斷標準必須是
**「有沒有真的拿到 message 內容」**。免費模型的供應商滿載是常態不是例外 ——
實測單獨打一次就踩到。現在偏好模型失敗會自動換 `openrouter/free` 自動路由
重試一次,客人不會看到錯誤訊息。

### 2. 在 PowerShell 主控台按 Ctrl+V 不是貼上

它會塞進一個字面上的控制字元 `^V`,於是 channel secret 變成 1 個字元。
而 seed 照單全收、加密、寫進資料庫,**完全不報錯**。症狀要到 LINE Console
按 Verify 回 401 才出現,那時人會去查 webhook 網址和 tunnel。

現在 seed 會擋:secret 必須是 32 位十六進位、token 至少 100 字元,
不合就拒絕,而且錯誤訊息直接把 Ctrl+V 這個坑和修法寫出來。

### 3. winget 裝完,已經在跑的行程還是找不到它

winget 只更新登錄檔裡的 PATH。**已經在執行的行程,以及它們之後開出來的
子行程,拿到的都還是舊的環境變數。** 於是出現「明明裝好了,腳本卻說找不到
cloudflared,叫你去安裝」—— 那句話會把人導去重裝。

現在 `scripts/dev.py` 先查 PATH,再查 winget 的預設安裝位置。
使用者不該需要理解 Windows 的 PATH 傳播規則才能跑起自己的 demo。

### 4. Quick Tunnel 網址一換,LINE 回 530

530 是 Cloudflare 的錯誤,**長得完全不像「網址過期」** —— 當下第一個念頭是
去查剛才改了什麼程式。這是「不買網域、走 Quick Tunnel」那個決定必然的代價,
但代價可以自動化掉:網址已經在腳本手上,寫回去只是一行 PUT。
`python scripts/dev.py --slug bistro` 現在會自己把當次網址寫回 LINE Console。

### 5. 太早寫回網址,LINE 回 400 而網址是對的

承上。第一版一放到真實環境就壞:`{"message": "Invalid webhook endpoint URL"}`。
原因是 cloudflared 在 stderr 印出網址的那一刻,Cloudflare 的邊緣還沒開始路由,
而 LINE 的 PUT 會**實際去打那個網址驗證**。實測同一個網址:剛起時 400,
幾秒後通了再 PUT 就是 200。現在會先輪詢 `/health` 到 200 才註冊。

### 6. 非文字訊息繞過了去重

原本的流程是「不是文字就送罐頭回覆然後 return」—— 而那個 return 在去重那一步
**之前**。於是 LINE 重送一張圖,客人會連收兩次「我只看得懂文字訊息」。
文字訊息沒有這個問題,所以整套測試一直沒抓到。

### 7. `pip install -e .` 在乾淨的 clone 上失敗

```
error: Multiple top-level packages discovered in a flat-layout:
       ['app', 'alembic', 'prompts', 'industries'].
```

那是 README「怎麼跑起來」的**第一道指令**。開發機完全看不出來:editable
安裝是專案還只有 `app/` 的時候做的,egg-info 一直在,後來才長出
`prompts/`、`industries/`,而沒有人重跑過安裝。

**CI 第一次執行就抓到。** 這個問題本來會等到有人想跑這個專案那天才爆。

### 8. `validate` 需要加密金鑰與 LLM 帳號才跑得起來

`app/cli.py` 在模組層 import 了 `app.database`,而它在 import 的當下就
`get_settings()`。於是只想檢查自己 YAML 的人,得先產一把 Fernet 金鑰、
先辦一個 OpenRouter 帳號 —— **順序完全是反的**,validate 是導入現場第一個
會跑的東西,那時候還沒有任何憑證。CI 抓到的第二個問題。

## 八、限制

誠實列出來,不粉飾。

**現在會直接出事的**

1. **沒有部署。** 服務跑在開發者的筆電上,靠 Quick Tunnel 對外。電腦關了、
   網路換了、程式當了都沒人重啟。
2. **沒有速率限制。** `/webhook/{slug}` 是公開端點。簽章擋得住偽造,
   擋不住「有人拿真的帳號狂傳」—— 每則都會打 LLM,免費額度一分鐘燒光,
   之後所有客人都收到 fallback。
3. **答案品質沒有評測機制。** 免費模型的品質是浮動的(見第七節第 1 點),
   壞掉了不會有人知道。

**會慢慢變成問題的**

4. **知識庫塞在 prompt 裡。** 每則請求 1,500–2,300 tokens,而那只有 12 筆 FAQ。
   真實店家的問答不會只有 12 筆。
5. **只能回答,不能做事。** 沒有 tool calling。客人說「我要訂位」,
   它只能念電話號碼。
6. **多租戶只存在於 schema。** 資料表設計是對的(`company_id` 隔離、
   一家一個 vector collection),但實際只有一列公司。
7. **真人接管沒有實作。** 欄位都在,邏輯一行沒寫。

**工程面**

8. `LineClient` 每則訊息開一個新的 httpx client 且從不關閉。
9. `scripts/dev.py` 的埠寫死 8000(已經撞過一次埠衝突)。
10. log 只印到終端機,關掉就沒了。

## 九、下一步

| 優先 | 做什麼 | 為什麼是這個順序 |
|---|---|---|
| 1 | **部署 + 速率限制** | 限制 1、2 是分水嶺。沒有這兩個,它只能是「會動的原型」;有了才談得上「能放著跑」 |
| 2 | **RAG 與知識庫** | 限制 4 是功能天花板。`knowledge_documents` 資料表與 `companies.vector_collection` 欄位在設計階段就預留了 —— 做這段等於把設計文件裡已經想好的東西兌現 |
| 3 | **Tool calling** | 訂位(寫資料庫)、查訂單(讀資料庫)、多輪補問 |
| 4 | **真人接管的機制** | `users.mode` / `mode_expires_at` / `human_mode_timeout_minutes` 已預留,含「客服下班忘記切回 AI,客人就永遠等不到回覆」的逾時自動歸還。**先做機制,後台介面之後再說** —— 後台是大量 UI 工時、很低的技術密度 |

## 十、一句話總結

這不是一個「做到一半」的系統,是一個**範圍畫得很小但做完整了**的系統。

它在意的事情從頭到尾一致:不沉默、憑證不落地、壞資料不准進資料庫、
錯誤訊息要指得到真正原因。第七節那八個坑,每一個都有對應的修正與回歸測試 ——
**那些是真的接上去跑過才會有的東西。**
