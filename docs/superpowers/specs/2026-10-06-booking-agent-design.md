# 預約 agent — 設計文件

## 一、目標與範圍

### 要解決的問題

到目前為止,模型只會做一件事:判斷要不要轉真人(單一工具、呼叫一次就結束)。這是「用 tool calling
做決策的 LLM 客服」,還不是 agent —— 工具結果從來不回到模型手上,模型也沒有機會看了結果再決定下一步。

客人說「我要訂位」,bot 只能念電話號碼(餐廳、診所的 FAQ 都寫「請來電」)。

### 做

- 通用的**預約**:每個行業資料夾多一份選填的 `booking.yaml`(時段、名額、要問的欄位),
  餐廳與診所共用同一段程式,電商沒有這份檔案就不提供預約
- **agent loop**:模型呼叫工具 → 程式執行 → 結果放回對話 → 模型再決定,直到給出文字回答
- 兩個工具:**查空位**、**建立預約**(加上既有的轉真人)
- 有名額就**直接成立**(像 inline 的線上訂位),名額以「組」計算
- 店家用 CLI 查預約;有設定 `staff_notify_to` 時推播新預約

### 不做

- 取消、修改預約(請客人來電;`status` 欄位先預留)
- 訂金、網頁後台、接外部日曆或訂位系統
- 預約前提醒、候補名單、各醫師分開排班
- 桌位排列(以「組」計名額,不以座位數)

## 二、決策紀錄

| # | 決策 | 選擇 | 理由 |
|---|---|---|---|
| 1 | 範圍 | 通用預約工具,由 `booking.yaml` 驅動 | 只做餐廳的話等於寫了一段 per-industry 程式碼,違反專題「不改程式碼」的主張;跟轉真人工具由 `escalation.yaml` 產生是同一套做法 |
| 2 | 成立方式 | 有名額就直接成立 | 客人當下得到結果才是 bot 預約的價值;「店家確認」需要後台介面,範圍約兩倍,且店員端無法實測(只有一個 LINE 帳號) |
| 3 | 名額 | 以「組」計,每筆另有人數上限 | 餐廳與診所都適用;以座位計會牽涉桌位排列,與 agent 無關 |
| 4 | loop 實作 | 自己寫 tool-calling 迴圈 | 面試要講得出迴圈怎麼運作;沿用既有的時間上限、備援、savepoint。只讓模型抽欄位(不跑迴圈)就不是 agent;框架會把迴圈藏起來,也跟「證明會做系統」的初衷相反 |
| 5 | 步數上限 | 4 步,最後一步不給工具;模型仍回工具呼叫時照樣執行(轉真人優先),不再問模型,回覆由程式補 | 實測最多用 3 步;最後一步拿掉工具,不會繞圈到什麼都沒回。2026-10-08 實測 NVIDIA 在最後一步不帶工具、`tool_choice: none`、關思考三種做法下都照樣回 `create_booking`(各 3/3)—— 丟掉的話資料齊全的訂位不會成立,所以照樣執行,結果不再塞回對話 |
| 6 | 時間上限 | 整個 loop 45 秒(原本每則 25 秒) | 實測每步 1–13 秒,3 步最長超過 22 秒;LINE reply token 約一分鐘有效,留 15 秒給送出與備援 |
| 7 | 日期 | **程式提供未來 N 天的日期對照表**,並規定「週五」= 從今天起算最近的那一天(今天就是週五則指今天) | 實測:沒有對照表時模型把 10/10 當週五;有對照表後全對。模型反覆追問「本週五還是下週五」,是 prompt 本身寫了歧義 |
| 8 | 把關 | 所有檢查在程式裡(`app/booking.py`),模型只負責對話 | 模型是會出錯的那一方;不通過就回傳中文錯誤,讓模型向客人解釋 |
| 9 | 全站上限 | 維持每分鐘 30 則 | 多數訊息仍是 FAQ(1 步);平均約 1.2–1.5 次呼叫/則 ≈ 每分鐘 40 次。最壞情況 NVIDIA 429 → OpenRouter 接手,失敗形狀與以前相同。log 記步數,實際偏高再調 |
| 10 | 存什麼進對話紀錄 | 只存客人訊息與最後的文字回答,不存中間的工具呼叫 | 建立預約時程式會再查一次名額,模型不需要看到上一則的工具結果 |
| 11 | PR 規模 | 一個 PR | loop 單獨上線看不出任何差異,沒辦法獨立驗收 |

### 2026-10-06 實測(NVIDIA `nemotron-3-super-120b-a12b`,假工具,共 13 段對話)

| 情境 | 結果 |
|---|---|
| 一句講完(日期時間人數姓名電話) | 2/2:查空位 → 建立 → 確認,3 步 |
| 指定已滿時段 | 3/3:先查、老實說滿了、建議其他時段,沒有硬訂 |
| 分四句慢慢講 | 2/5 完成;另 3 次卡在反覆追問「本週五還是下週五」(prompt 歧義 + 腳本客人不回答) |
| 日期換算 | 沒有對照表時錯一次;有對照表後全對 |

### 2026-10-08 真模型驗收(NVIDIA `nemotron-3-super-120b-a12b`,正式的預約說明與工具)

| 情境 | 結果 | 處置 |
|---|---|---|
| 客人只說「我想訂位」(開放式問題),開著思考 | 5 次裡 3 次把 `max_tokens`(1200)全拿去思考、`finish_reason=length`、content 是空的(11~37 秒)→ `LLMError("拿不到內容")` → 客人收到 fallback;沒有預約說明的線上版 5/5 約 1.5 秒 | 送 `chat_template_kwargs.enable_thinking=false` 後 5/5 在 0.9~3 秒內正常回答 → `nvidia_enable_thinking = False` |
| 最後一步,對話裡已有 assistant `tool_calls` 與 `role: tool` | 不帶 `tools`:3/3 照樣回 `create_booking`;帶 `tools` + `tool_choice: none`:3/3 一樣;關思考:3/3 一樣 | loop 最後一步仍不給工具,但回來的工具呼叫照樣執行、不再問模型 |

## 三、整體流程

```
客人訊息 → (既有)寫入、去重、店員帳號、HUMAN、非文字、關鍵字轉真人、限流
  → 【agent loop】
       第 1–3 步:帶工具問模型
         ├ 文字           → 結束
         ├ transfer_to_human → 結束,走既有轉真人流程(優先;同一回應的其他工具不執行)
         └ 預約工具        → 執行 → 結果放回對話 → 下一步
       第 4 步(最後一步):不給工具
         ├ 文字           → 結束
         ├ transfer_to_human → 結束,走既有轉真人流程(優先)
         └ 仍回預約工具    → 照樣執行,不再問模型;回覆由程式補(成立 → 確認句,否則 fallback)
  → (既有)安全網、簡繁、寫入、送出、標已讀
```

webhook 的改動:原本「呼叫一次 `complete()`」的那段,改成呼叫 agent loop;loop 回傳「最後的文字」或
「轉真人決定」,其餘流程不變。

## 四、資料與設定

### `industries/<行業>/booking.yaml`(選填)

```yaml
label: 訂位                          # 這個行業怎麼稱呼(訂位 / 預約)
days: [二, 三, 四, 五, 六, 日]        # 可預約的星期
times: ["18:00", "19:00", "20:00"]   # 時段
capacity: 5                          # 每個時段幾組
max_party: 6                         # 每筆最多幾人;1 = 不問人數
advance_days: 14                     # 最遠可以訂到幾天後
fields: []                           # 姓名、電話以外要問的欄位
```

診所:

```yaml
label: 預約
days: [一, 二, 三, 四, 五, 六]
times: ["09:30", "10:30", "14:30", "15:30"]
capacity: 3
max_party: 1
advance_days: 14
fields:
  - key: service
    label: 看診項目
    options: [一般皮膚, 青春痘, 醫美諮詢]
```

姓名、電話一律必問,不寫在設定裡。`loader` 把 `booking` 列為選填檔案;不存在時字典沒有 `booking` 鍵,一律用 `data.get("booking")` 讀取(回傳 `None`)。

### validate 新規則

| 規則 | 等級 | 內容 |
|---|---|---|
| 11 | ERROR | `booking.yaml` 格式:`label` 非空;`days` 只能是「一」到「日」且不重複;`times` 是 `HH:MM` 且不重複;`capacity` ≥ 0;`max_party` ≥ 1;`advance_days` 1–60;`fields` 每項有 `key`、`label`、非空 `options`,`key` 不可是 `name`/`phone`/`party_size`/`date`/`time` |
| 12 | BLOCK | 每個時段都要落在 `company.hours` 的營業區間內(用規則 8 既有的解析) |

### 資料庫(一個 Alembic 遷移)

- `companies.booking_config`:JSON,可為空。seed 寫入 `booking.yaml` 的內容;`None` = 不提供預約
- `bookings`:

| 欄位 | 型別 | 說明 |
|---|---|---|
| `id` | Integer PK | |
| `company_id` | String(36) FK → companies(CASCADE) | |
| `user_id` | String(36) FK → users(CASCADE) | 哪位 LINE 客人訂的 |
| `booking_date` | Date | |
| `booking_time` | String(5) | `HH:MM` |
| `party_size` | Integer,預設 1 | |
| `extra` | JSON,預設 `{}` | 額外欄位(如看診項目) |
| `name` | String(50) | |
| `phone` | String(20) | 正規化後(去掉 `-` 與空白) |
| `status` | String(16),預設 `confirmed` | 先只有這個值;預留給取消 |
| `created_at` | DateTime(timezone) | server_default now() |

索引 `ix_bookings_slot`(`company_id`, `booking_date`, `booking_time`)。

### 設定(`app/config.py`)

| 欄位 | 預設 | 說明 |
|---|---|---|
| `agent_max_steps` | 4 | 最後一步不給工具;模型仍回工具呼叫時照樣執行(轉真人優先),不再問模型,回覆由程式補(2026-10-08 實測 NVIDIA 不帶工具照樣回工具呼叫) |
| `agent_total_budget_seconds` | 45 | 整個 loop 合計;每一步拿剩下的時間 |
| `nvidia_enable_thinking` | False | 送給 NVIDIA 的 `chat_template_kwargs.enable_thinking`,預設關。2026-10-08 實測:預約說明很長,開著思考時開放式問題 5 次裡 3 次把 `max_tokens` 用光、content 是空的(11~37 秒);關掉後 5/5 在 0.9~3 秒內答出。只送 NVIDIA,OpenRouter 不帶 |

「正在輸入」動畫的秒數改用 `agent_total_budget_seconds`(LINE 接受 5–60、5 的倍數)。
既有的 `llm_total_budget_seconds`(25)保留給單次 `complete()` 沒有傳入截止時間時使用。

## 五、工具與把關(`app/booking.py`)

工具定義由 `booking_config` 產生(名稱用英文,說明與欄位用中文、取自 `label` 與 `fields`)。

### check_availability

參數:`date`(YYYY-MM-DD);`party_size`(僅 `max_party > 1` 時)。
回傳:`{date, weekday, open, slots: [{time, available}]}`;只回能不能訂,不回剩幾組。
日期超出範圍、公休、人數超過上限時回傳 `{error: "<中文說明>"}`。

### create_booking

參數:`date`、`time`、`name`、`phone`、`party_size`(僅 `max_party > 1`)、每個 `fields` 的 `key`(值為選項之一)。

依序檢查,第一個不通過的回傳 `{ok: false, error: "<中文說明>"}`:

1. 日期在 `[今天, 今天 + advance_days]` 內,而且是 `days` 之一(台灣時間)
2. 時段在 `times` 內;日期是今天時,時段要晚於現在
3. 人數 1 ≤ n ≤ `max_party`;超過時錯誤訊息附上店家 `contact`
4. 姓名去掉空白後非空、≤ 50 字
5. 電話去掉 `-` 與空白後符合 `^0\d{8,9}$`
6. 每個 `fields` 的值在 `options` 內
7. **防重複**:先鎖住該公司的 `companies` 列(見第 8 點),再查同一位客人、同一天、同一時段是否已有 `confirmed`
   預約 → 有就直接回傳那筆(`{ok: true, duplicate: true, booking, note}`),不建立第二筆。`note` 寫明內容沒有更改、
   要修改或取消請客人直接聯繫 `contact` —— 客人想改人數或時間時,模型才不會把這個 `ok` 說成「已幫您改好」
8. **名額**:在同一個鎖裡數該時段 `confirmed` 筆數,< `capacity` 才寫入。鎖(`SELECT … FOR NO KEY UPDATE`)排在
   防重複之前:同一位客人同時送兩次、或兩個人搶最後一個名額,第二個請求都要等第一個 commit 完才看得到結果
   (PostgreSQL;SQLite 只用在開發與測試,不保證這件事)。`NO KEY` 就夠了,不會擋住其他資料表對 `companies` 的外鍵寫入

成功回傳 `{ok: true, booking: {date, weekday, time, party_size, name, extra}}`。
新預約成立時(webhook 在回覆客人之後)寫一行 info log:`{label}成立 客人 …f6e402 2026-10-09 20:00 4 位`
(餐廳是「訂位成立」;不記電話;重複的那筆不記)。

### 每次請求附加的預約說明

只有 `booking_config` 不為 `None` 時,在 system prompt 後面接上(每次請求現算,不存資料庫):

- 「今天是 2026-10-06(二),時區是台灣」
- 未來 `advance_days` 天的對照表,公休日標「公休」
- 規則:「週五」沒有特別說明就是從今天起算最近的那一天(今天就是週五則指今天),不要反問;建立前一定要先查空位;
  不可以自己編造空位;缺什麼一次問完;每筆最多 `max_party` 位,超過請客人聯繫 `contact`;
  已成立的預約不能在這裡修改或取消,客人要改人數(`max_party > 1` 時)、時間或取消,請他直接聯繫 `contact`

## 六、agent loop(`app/agent/loop.py`)

```python
def run_agent(messages, *, tools, execute, max_steps, budget_seconds, complete_fn) -> AgentResult
```

`complete_fn` 由 webhook 傳入它自己的 `complete`(測試可以替換);`max_steps`、`budget_seconds` 取自設定。

- `execute(name, arguments) -> dict`:由 webhook 綁好公司、客人、session 後傳入;loop 本身不碰資料庫
- `AgentResult`:`text`(最後文字,可能為 `None`)、`handoff`(轉真人的 `ToolCall` 或 `None`)、
  `booking`(本次成立的預約,或 `None`)、`steps`、`tools_used`(依序執行過的工具名稱,給 log 用)、
  `token_count`、`latency_ms`

每一步:

1. 剩餘時間 = 截止時間 − 現在;不足 1 秒就停止
2. `complete(messages, tools=… , deadline=…)`;第 `agent_max_steps` 步(最後一步)不給工具(`tools=None`,見下)。呼叫失敗(`LLMError`,或上游回應
   格式怪異時的其他例外)→ 停止並回傳 `text=None`,本次已成立的 `booking` 照樣帶著
3. 有 `transfer_to_human` → 立刻回傳 `handoff`,不執行同一回應裡的其他工具
4. 其他工具 → 依序執行;把 assistant 的 `tool_calls` 與每個 `{"role": "tool", "tool_call_id", "content"}`
   附加到 messages;工具丟例外時以 `{ok: false, error: "系統暫時無法處理"}` 作為結果
5. 沒有工具呼叫 → 回傳文字

**最後一步**:仍送 `tools=None`(有些上游因此改回文字),但回來的工具呼叫不丟掉 —— 有 `transfer_to_human`
照樣轉真人優先、其他工具照樣依序執行(記進 `tools_used`、成立的 `booking` 記下來、例外變 `{ok: false, …}`),
執行完直接回傳(`text` 是模型同時回的文字,通常是 `None`),不把結果放回對話、不再呼叫模型。2026-10-08 實測
NVIDIA 在最後一步不帶工具、`tool_choice: none`、關思考三種做法下都照樣回 `create_booking`(各 3/3)。
回覆由 webhook 補:有成立的 `booking` 且沒有文字 → 確認句;都沒有 → fallback。

每則訊息寫一行 info log:`agent 步數=3 工具=check_availability,create_booking`。

### `llm.py` 的配合修改

- 回傳**所有** tool_calls(含 `id`),不只第一個;`LLMResult.tool_call` 保留(= 第一個)以免動到既有呼叫端
- `complete()` 接受選填的 `deadline`(monotonic 秒),有傳就用它取代 `llm_total_budget_seconds`;每個請求本身的
  timeout 也縮到剩下的時間(最少 0.5 秒)—— NVIDIA 不串流,生成完之前一個位元組都不送,只在收到資料時看時鐘擋不住
- 每一步各自走 NVIDIA → OpenRouter 的備援
- 各家上游自己的額外參數放在 `_Endpoint.extra_body`,跟網址、key 一起走:NVIDIA 帶
  `chat_template_kwargs`(`nvidia_enable_thinking`,預設 False),OpenRouter 不帶

## 七、錯誤處理

| 情況 | 處理 |
|---|---|
| **預約已成立,但送出去的不是模型自己的回答** | 由程式組確認句:「已為您{label} 10/09(五)20:00,4 位,{label}人王小明。」(`max_party` 為 1 時不寫人數;有 `fields` 時附上值)—— 客人絕不能訂好了卻不知道。**模型超時或失敗**:只送確認句,不接 fallback(「連線有點問題,可以再問一次嗎」會讓人以為沒訂到)。**之後又轉真人、或回答觸發安全網被改成轉真人**:確認句接在轉真人那句話前面 |
| 還沒成立就超時、模型全部失敗 | 既有的 fallback 訊息 |
| 工具回傳錯誤(名額被搶、格式不對) | 當成工具結果還給模型,由模型解釋並建議其他時段 |
| 工具丟例外 | `{ok: false, error: "系統暫時無法處理"}` 給模型;log 記 exception |
| 步數用完 | 最後一步不給工具;模型仍回工具呼叫時照樣執行(轉真人優先),不再問模型,回覆由程式補(成立 → 確認句,沒有 → fallback)。2026-10-08 實測 NVIDIA 不帶工具照樣回工具呼叫 |
| 模型呼叫不存在的工具 | `{ok: false, error: "沒有這個工具"}` |
| 推播店員失敗 | 只記 log,不影響預約 |

## 八、店家端

- `python -m app.cli bookings --slug <slug> [--date YYYY-MM-DD]`:預設列出今天(台灣)以後的 `confirmed` 預約,
  依日期、時間排序;欄位:日期、星期、時間、人數、姓名、電話、額外欄位、建立時間(台灣時間)
- 有 `staff_notify_to` 時推播:「【新{label}】10/09(五)20:00 4 位 王小明 0912345678」
  (推播算進 LINE 每月免費訊息額度)
- 調整名額:改 `booking.yaml`、重跑 seed

## 九、檔案清單

### 新增

- `app/booking.py`、`app/agent/loop.py`、`app/models/booking.py`
- `industries/restaurant/booking.yaml`、`industries/clinic/booking.yaml`
- `alembic/versions/c5e8a1f3d920_bookings.py`
- `tests/test_booking.py`、`tests/test_agent_loop.py`

### 修改

- `app/agent/llm.py`(多個 tool_calls、`deadline`、各上游額外參數)
- `app/routers/webhook.py`(呼叫 loop、超時確認句、推播店員、loading 秒數)
- `app/knowledge/loader.py`(選填檔案)、`app/knowledge/validate.py`(規則 11、12)
- `app/cli.py`(seed 寫 `booking_config`;新指令 `bookings`)
- `app/config.py`、`app/models/company.py`、`app/models/__init__.py`
- 測試:`test_agent.py`、`test_webhook.py`、`test_validate.py`、`test_cli.py`、`test_alembic.py`、`test_industries.py`
- 文件:`README.md`、`docs/report.md`、`docs/demo-checklist.md`、`industries/README.md`

## 十、測試策略

先寫測試、看它失敗再實作。

- **`test_booking.py`**(真的資料庫):可預約範圍邊界、公休日、今天已過的時段、人數 0 / 上限 / 上限+1、
  電話格式(含 `-`、空白、市話、錯誤長度)、選項外的值、名額滿、防重複回傳既有那筆、`max_party: 1` 不收人數、
  工具定義由設定產生(餐廳有 `party_size`、診所有 `service` 選項)、預約說明的日期對照表與公休標記(固定「今天」)
- **`test_agent_loop.py`**(假的模型回應序列):只回文字;查空位 → 建立 → 文字;轉真人優先且不執行其他工具;
  步數用完時最後一步沒有帶工具、但回來的工具呼叫照樣執行且不再問模型(轉真人優先);**建立後超時 → 結果帶 `booking` 且沒有文字**;工具錯誤與例外回給模型;
  不存在的工具;每一步拿剩下的時間
- **`test_agent.py`**:多個 tool_calls 都解析出來(含 id);`deadline` 取代預設上限;送 NVIDIA 的 body 預設帶
  `enable_thinking: false`(可由設定打開)、退回 OpenRouter 時不帶
- **`test_webhook.py`**:整段訂位寫進 `bookings`;對話紀錄只有 user + 最後文字;超時時送出程式組的確認句;建立後又轉真人時,轉真人的話前面接上確認句;
  有 `staff_notify_to` 時推播;限流照樣在 loop 之前擋;沒有 `booking_config` 時工具清單沒有預約工具
- **`test_validate.py`**:規則 11 的每一種 ERROR;規則 12 的 BLOCK;沒有 `booking.yaml` 不報錯
- **`test_cli.py`**:seed 寫入 / 清空 `booking_config`;`bookings` 指令的篩選與排序
- **`test_alembic.py`**:升級與降版
- CI:SQLite 與 PostgreSQL 各一輪

**真模型驗收(不進 CI)**:部署前用正式的 prompt 與工具定義重跑實測情境(分四句、一句講完、已滿時段、診所),
「分四句」要能穩定完成。

## 十一、驗收標準

- [ ] 全套測試通過,兩個 CI job 都綠
- [ ] 真模型驗收:四個情境各 3 次,建立的預約日期全對、沒有在已滿時段建立
- [ ] 部署後 `alembic upgrade head` 自動完成,重跑 seed 後 `booking_config` 有值
- [ ] 手機在 LINE 上完整訂一次位,收到確認;`app.cli bookings` 列得出那筆
- [ ] log 有 `agent 步數=…` 與 `{label}成立 …`(餐廳是「訂位成立」)

## 十二、已知限制

- 客人的姓名、電話會隨對話送到模型供應商;NVIDIA 試用條款寫明會收集輸入內容 —— demo 可以,接真實店家前要換成
  不收集資料的方案
- 只能新增,不能取消或修改(請客人來電)
- 名額以組計,不處理桌位
- 對話紀錄不存工具過程:下一則訊息時模型看不到上一則的查詢結果(建立時會再查一次,所以不會超訂)
- 最後一步才回工具呼叫時,結果不會再交給模型解釋:成立 → 客人只收到程式組的確認句(模型沒有同時回文字時);被拒絕(名額被搶、格式不對)
  → 收到 fallback,要再問一次
- 多步驟讓每則訊息最多呼叫模型 4 次;全站每分鐘 30 則在最壞情況下會超過 NVIDIA 的每分鐘約 40 次
