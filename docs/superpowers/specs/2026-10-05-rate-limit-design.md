# 速率限制 — 設計文件

## 一、目標與範圍

### 要解決的問題

`/webhook/{slug}` 是公開端點。簽章擋得住偽造,擋不住「有人拿真的 LINE 帳號狂傳」——
每則都會打 LLM。2026-10-05 起服務 24 小時跑在 GCP 上,這從理論變成實際的風險。

換成 NVIDIA 當主要模型之後,風險的形狀也變了:

| | 額度 | 被灌爆時會怎樣 |
|---|---|---|
| NVIDIA(主要) | 每分鐘約 40 次,帳號共用,**沒有每日上限** | 超過就 429 → 全部退到備援 |
| OpenRouter(備援) | **一天 50 次**,失敗的請求也扣 | 幾分鐘燒光 → 之後所有客人只收到 fallback |

所以要擋的是兩件事:**單一客人狂傳**,以及**同一分鐘湧進太多訊息**把 NVIDIA 推過上限、
連鎖燒掉 OpenRouter 的每日額度。

### 做

- 每人每分鐘、每人每天、全站每分鐘,三條上限
- 被擋時:個人上限第一次踩到回一句提醒、之後安靜;全站忙碌每則都回忙碌訊息
- 關鍵字轉真人不受限流影響
- `companies` 加 `contact` 欄位,提醒文字附上店家聯絡方式

### 不做

- autoheal(report 下一步 1 的另一半,另一個 PR)
- 各店家各自的上限(三個數字是全站設定)
- 被擋掉的訊息事後自動補答
- 限流的管理介面或統計報表

## 二、決策紀錄

| # | 決策 | 選擇 | 理由 |
|---|---|---|---|
| 1 | 擋哪一層 | 個人 + 全站兩層都做 | 只做個人:四五個人各傳十幾則照樣灌爆;只做全站:一個人就能讓所有客人都被擋 |
| 2 | 個人超量時 | 第一次回提醒,之後安靜(A1) | 觸發的多半是以為沒送出而重傳的正常客人(需要被告知一次),或故意灌爆的人(每則都回只會更亂) |
| 3 | 全站忙碌時 | 每則都回忙碌訊息(B1) | 走到這裡的多半是正常客人,他們沒做錯事 |
| 4 | 上限 | 每人每分鐘 5、每人每天 20、全站每分鐘 30 | 全站 30 留 10 的餘裕在 NVIDIA 的每分鐘 40 次以下 |
| 5 | 「一天」怎麼切 | 台灣時間午夜(UTC+8) | 全站已經沒有每日額度要對齊 OpenRouter;對客人與店家,「今天」就是台灣的今天 |
| 6 | 全站上限的範圍 | 跨所有租戶合算 | 額度綁的是同一把 key,分開算沒有意義 |
| 7 | 計數存在哪 | **直接從 `chat_histories` 算** | 不新增任何狀態,計數跟對話紀錄是同一份資料;部署與重啟不歸零;多 worker 也對 |
| 8 | 「每人每天」算什麼 | **AI 回了幾則**(assistant 列),不是客人傳了幾則 | 算客人傳的話,轉真人期間跟店員來回 20 則的客人,交還 AI 後問一句就被擋 —— 被擋的正好是剛剛不太高興的那位 |
| 9 | 「只提醒一次」怎麼做 | 由計數本身決定,不另外記狀態 | 每分鐘:剛好第 6 則提醒。每天:提醒本身也寫進對話紀錄,計數從 20 變 21,之後自然進入安靜 |
| 10 | 放在流程哪裡 | 關鍵字轉真人**之後**、呼叫模型**之前** | 只擋花錢的路徑;安全觸發(診所「胸痛」)不管傳了幾則都要轉 |
| 11 | 檢查失敗時 | 放行(fail-open) | 限流是保護機制,不是主流程。「沉默是唯一不被接受的失敗模式」 |

決策 7 的替代方案與不選的理由:

- **程式記憶體裡的計數**:最簡單,但每次部署歸零(2026-10-05 一天就部署兩次),
  多 worker 或多台機器時不準 —— 正是面試最常被追問的弱點。
- **專用計數表**:最精確,但多一張表、每則多一次寫入、要定期清舊資料,
  而 PostgreSQL 與 SQLite 的 upsert 語法不同要寫兩套。這個規模是過度設計。

## 三、整體流程

`process_text_event` 的順序,只在「關鍵字之後、模型之前」插入一步:

```
客人訊息 → 寫進 chat_histories(去重,不變)
  → 店員帳號?           → 結束(不變)
  → HUMAN 模式中?        → 結束(不變)
  → 非文字訊息?          → 固定回覆(不變,不限流)
  → 關鍵字命中 transfer? → 轉真人(不變,不限流)
  → 【限流檢查】
       ├─ 放行     → 正在輸入 → 呼叫模型(不變)
       ├─ 提醒     → 回提醒、寫進紀錄、標已讀
       ├─ 安靜     → 不回、不標已讀,寫 warning log
       └─ 全站忙碌 → 回忙碌訊息、寫進紀錄、標已讀
```

- 檢查在「正在輸入」動畫**之前**:被擋的訊息不會讓客人看到「輸入中」卻什麼都沒收到。
- 非文字訊息不限流:固定回覆不打模型,LINE 的 reply 也不收費。
- 安靜的訊息保持**未讀**,跟「AI 沒回的留給真人看」一致 —— 店家在官方帳號後台看得到有人在狂傳。

## 四、計數規則

模組 `app/ratelimit.py`,一個純粹的判定函式:

```python
def check(db, *, user_pk: str, now: datetime, limits: Limits) -> Verdict
```

不帶 `company_id`:`users.id` 本身就屬於某一家公司,每人的兩條用它就夠;全站那條本來就跨所有公司。

`Verdict` 帶三個欄位:`action`(`ALLOW` / `NOTIFY` / `SILENT` / `BUSY`)、
`rule`(`user_minute` / `user_day` / `global_minute`,放行時為 `None`)、`count`(觸發時的計數)。

「這一則」在檢查前已經寫進 `chat_histories`(去重那一步),所以計數**包含這一則**。

| 順序 | 規則 | 計數 | 判定 |
|---|---|---|---|
| 1 | 每人每分鐘(上限 m) | 這位客人 `created_at >= now - 60 秒` 的 **user** 列(所有訊息型別) | `count == m + 1` → NOTIFY;`count > m + 1` → SILENT |
| 2 | 每人每天(上限 d) | 這位客人 `created_at >=` 台灣今天 00:00 的 **assistant** 列 | `count == d` → NOTIFY;`count > d` → SILENT |
| 3 | 全站每分鐘(上限 g) | **所有公司** `created_at >= now - 60 秒` 的 **user** 列 | `count > g` → BUSY |

- 依序檢查,第一條不放行的就是結果。個人規則先於全站:狂傳的人被擋的是他自己,不佔全站名額。
- 上限設 `0` = 關閉那一條。
- 「台灣今天 00:00」= 把 `now` 換到 UTC+8、取當天 00:00、再換回 UTC 比較(資料庫存的是 UTC)。
  台灣沒有日光節約,用固定 UTC+8,不依賴時區資料庫。
- 每人每天的 assistant 列包含 AI 回答、非文字的固定回覆、轉真人的 script,以及本設計的提醒與忙碌訊息。
  `human_agent` 列不算(目前也不會寫入,真人在官方帳號後台回覆)。

## 五、回覆文字與紀錄

`{contact}` 換成 `companies.contact`:

| 情況 | 文字 |
|---|---|
| 每人每分鐘的提醒 | 您傳得有點快,我跟不上了 🙏 麻煩等一分鐘,再把想問的事傳一次給我。 |
| 每人每天的提醒 | 今天跟我聊的次數已經到上限了,明天再來問我;急的話可以直接聯繫 {contact}。 |
| 全站忙碌 | 目前詢問的人比較多,請過幾分鐘再傳一次;急的話可以直接聯繫 {contact}。 |

- `contact` 為空時省略「;急的話可以直接聯繫 {contact}」整段,句尾補「。」。不會出現空白或 `None`。
- **不寫「我稍等一下再回您」**:bot 不會事後補答被擋掉的訊息,那是做不到的承諾 ——
  跟 report 第七節第 10 點「嘴上說要轉接卻沒轉」同一類錯誤。
- **全站忙碌不用 `fallback_message`**:那句是「連線有點問題,可以再問一次嗎?」—— 叫客人立刻重傳,
  正好讓全站更塞;而且 seed 從來不設它,三個行業都是同一句預設值。
- 用「聯繫」不用「來電」:電商的 contact 是 email。
- 三段文字都**寫進 `chat_histories`(assistant)**:跟非文字固定回覆一致,模型下一輪看得懂上下文;
  決策 9 的「每天只提醒一次」靠它;之後要統計觸發次數有資料可查。
- 每次不放行都寫一行 warning,例如 `限流 客人 …abc123 規則=user_minute 計數=6 動作=NOTIFY`。

## 六、資料與設定

### 設定(`app/config.py`)

| 欄位 | 環境變數 | 預設 |
|---|---|---|
| `rate_limit_user_per_minute` | `RATE_LIMIT_USER_PER_MINUTE` | 5 |
| `rate_limit_user_per_day` | `RATE_LIMIT_USER_PER_DAY` | 20 |
| `rate_limit_global_per_minute` | `RATE_LIMIT_GLOBAL_PER_MINUTE` | 30 |

負數視為設定錯誤(Pydantic 驗證 `>= 0`)。`compose.yaml` 把三個變數傳進 app 容器,
`.env.example` 加說明。

### Alembic 遷移(一個 revision,`down_revision = 3f2a9c1d7b4e`)

- `companies.contact`:`String(200)`、可為空。既有資料列是 NULL,重跑 seed 才會填上。
- `chat_histories` 加索引 `ix_chat_created`(`created_at`):給全站每分鐘的 COUNT 用。
  每人那兩條用既有的 `ix_chat_user_created`(`user_id, created_at`)。
- downgrade 刪掉兩者;`companies` 用 `batch_alter_table`(SQLite 的 DROP COLUMN 要重建表)。

### seed

`company.contact = data["company"]["contact"]`。`contact` 已經是 `validate` 的必填欄位,三個行業都有。

## 七、錯誤處理與已知取捨

| 情況 | 處理 |
|---|---|
| COUNT 查詢丟例外 | 寫 warning(含 exc_info),**放行** |
| 同一人同一瞬間兩則、剛好卡在門檻 | 可能兩次提醒或零次提醒 —— 兩邊都先寫入再計數,沒有鎖。後果輕微,不加鎖 |
| 送提醒失敗 | 跟現有回覆一樣由 `LineClient.send` 處理(reply 失敗改 push);提醒已寫進紀錄,不重試 |
| 提醒與忙碌訊息算進當天 20 則 | 接受。全站忙碌本來就罕見 |
| SQLite 的時間是字串比較 | 精度到秒(`CURRENT_TIMESTAMP` 沒有微秒),門檻附近可能差一秒。CI 兩種資料庫都跑 |
| 全站計數包含不會打模型的訊息 | 接受,是往保守那邊偏(早一點進入忙碌) |
| HUMAN 逾時的那一則剛好被限流 | 客人在 HUMAN 期間一分鐘內傳超過 5 則、逾時後再傳一句,那句會被擋,連「專員目前不在線上」的前綴都收不到。機率低(要在逾時前一分鐘內密集傳),接受;寫進 report 的限制 |

## 八、檔案清單

### 新增

- `app/ratelimit.py` —— `Limits`、`Verdict`、`check()`、`reply_text()`
- `alembic/versions/7b1e4c2a9d30_contact_and_chat_created_index.py`
- `tests/test_ratelimit.py`

### 修改

- `app/routers/webhook.py` —— 關鍵字之後插入限流分支
- `app/models/company.py`(`contact`)、`app/models/chat.py`(索引)
- `app/config.py`、`app/cli.py`(seed 寫 contact)
- `compose.yaml`、`.env.example`
- `tests/test_webhook.py`、`tests/test_cli.py`、`tests/test_alembic.py`、`tests/test_config.py`
- `README.md`(下一步表格)、`docs/report.md`(限制 2、下一步 1)

## 九、測試策略

先寫測試、看它失敗再實作。

**`tests/test_ratelimit.py`(真的資料庫,插入指定 `created_at` 的列)**

- 1 分鐘內第 5 則放行、第 6 則 NOTIFY、第 7 則 SILENT
- 61 秒前的列不算
- 每天:assistant 19 列放行、20 列 NOTIFY、21 列 SILENT
- **台灣午夜邊界**:台灣前一天 23:59(UTC 15:59)的 assistant 列不算,當天 00:01(UTC 16:01)的算
- **每天只算 assistant**:同一位客人今天有 30 列 user(轉真人期間),仍然放行
- 另一家公司、另一位客人的列不算進這位客人
- 全站:兩家公司合計 30 列放行、31 列 BUSY
- 個人優先:同時超過個人與全站時,結果是個人的 NOTIFY/SILENT
- 上限 0 關閉該條
- COUNT 丟例外時放行

**`tests/test_webhook.py`(沿用既有的 monkeypatch 寫法)**

- 被擋時 `complete` 沒被呼叫、`show_loading` 沒被呼叫
- NOTIFY:送出提醒、寫進 `chat_histories`、標已讀
- SILENT:什麼都不送、不標已讀
- BUSY:送出忙碌文字(含 contact)
- **超過上限時,關鍵字轉真人照樣轉**
- contact 為空時,文字沒有「聯繫」那半句

**其他**:`test_alembic.py` 驗證 upgrade/downgrade 兩個方向;`test_cli.py` 驗證 seed 寫入 contact;
`test_config.py` 驗證預設值與負數被拒。

## 十、驗收標準

- [ ] 全套測試通過,SQLite 與 PostgreSQL 兩個 CI job 都綠
- [ ] 部署後 `alembic upgrade head` 自動完成,重跑 seed(不加 `--reset-history`)後 `companies.contact` 有值
- [ ] 手機在一分鐘內連傳 6 則:前 5 則正常回答,第 6 則收到提醒,第 7 則沒有回應
- [ ] log 有 `限流 … 規則=user_minute … 動作=NOTIFY` 與 `動作=SILENT`
- [ ] 傳一句關鍵字(例如「吃了不舒服」),照樣轉真人

## 十一、部署步驟

1. 主機 `git pull` → `docker compose build app` → `docker compose up -d app`(分開跑,build 失敗不動到舊容器)
2. `docker compose exec app python -m app.cli seed --industry restaurant --slug bistro`(**不加** `--reset-history`)
3. 手機驗收;結束後 `app.cli release --slug bistro`(若驗收過程中轉了真人)

## 十二、開放問題

- 全站每分鐘 30 是依「NVIDIA 每分鐘約 40 次」推算的,那個數字是社群實測、沒有公開保證。
  上線後若在 log 看到 NVIDIA 429,再往下調。
