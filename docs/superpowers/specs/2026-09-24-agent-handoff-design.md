# 轉真人 agent 化 — 設計文件

## 一、目標與範圍

### 要解決的問題

v1 是「有記憶、有規則的 LLM 客服」,不是 agent:每則訊息都是固定的
「組 prompt → 問模型一次 → 回覆」,模型只決定**說什麼**,不決定**做什麼**。
升級規則(`escalation.yaml`)寫了 `action: transfer`,但系統從來沒有真的轉過 ——
模型只是照 prompt 把 script 念出來,客人聽到「我立刻請店長與您聯繫」,
實際上沒有任何人被通知。

資料庫早就預留了 HUMAN 模式(`users.mode`、`mode_expires_at`、
`companies.human_mode_timeout_minutes`),但 webhook 完全沒有讀它。

### 做

- 模型帶著 `transfer_to_human` 工具回答,**由模型自己決定**要不要轉真人
- 關鍵字規則保底:命中 `action: transfer` 的 trigger 就直接轉,不問模型
- 轉真人:切 HUMAN 模式、推播通知店員、回覆該類別的 script
- HUMAN 模式期間 AI 完全不回;逾時自動交還給 AI
- 真人客服用 **LINE 官方帳號後台**(Official Account Manager / 手機 app)回覆
- `app.cli release`:把客人立刻交還給 AI(示範時要用)

### 不做

- 真人客服的網頁後台
- 真人說的話進對話紀錄(後台回覆不經過我們的 webhook)
- 真人每回一句就延長期限(同上,我們看不到真人什麼時候回)
- 多步驟的 agent 迴圈、其他工具(查訂單、查空位)

## 二、決策紀錄

| # | 決策 | 選擇 | 理由 |
|---|---|---|---|
| 1 | 真人在哪裡回覆 | LINE 官方帳號後台 | 店家本來就有,webhook 開著也能同時用;不用另外學新系統 |
| 2 | 怎麼通知店員 | bot 推播給 `staff_notify_to` | 示範時另一支手機會跳通知;不通知的話店員根本不知道有人在等 |
| 3 | 誰決定要不要轉 | 關鍵字規則 + 模型判斷兩層 | 安全攸關的路徑不能只靠 LLM;免費模型常塞車,只靠模型等於把漏接的機率交給上游 |
| 4 | 實作方式 | 原生 tool calling,轉真人是**最後一步** | 呼叫工具後直接回 script,不再問第二次 —— 每則訊息仍然只花 1 次請求 |
| 5 | 模型 | 主要 `openrouter/free`、備用 `nvidia/nemotron-3-super-120b-a12b:free` | 兩個都支援 tools;`z-ai/glm-5.2:free` 不支援,不能再當主要模型 |
| 6 | 推播失敗時 | 照樣切 HUMAN | 客人已經被告知「會有人聯繫」,AI 這時又開始回答反而更混亂 |

決策 4 的替代方案與不選的理由:

- **請模型輸出 JSON**(`{"reply": ..., "transfer": ...}`):所有模型都能用,
  但不是 tool calling,說它是 agent 很勉強;免費模型的 JSON 格式也常出錯。
- **完整的 agent 迴圈**(呼叫工具 → 看結果 → 再決定):每則訊息 2 次以上請求,
  每天 50 次的額度撐不住;而且只有一個工具,迴圈沒有東西可以迴。

決策 6 的代價:推播失敗、店員沒收到通知的話,客人在 `human_mode_timeout_minutes`
(預設 30 分鐘)內收不到任何回覆。error log 是唯一的線索。

## 三、整體流程

`process_text_event` 在「寫入客人訊息、去重」之後(這段不變)分成三條路:

```
客人訊息 → 寫進 chat_histories(去重,跟現在一樣)
   │
   ├─ ① HUMAN 模式、未過期
   │     → 不回覆、不問模型,只存訊息
   │
   ├─ ① HUMAN 模式、已過期
   │     → 切回 AI,再照 ②③ 處理這一則;只有走到 ③ 而且模型回的是文字時,
   │       回覆開頭才加「專員目前不在線上,我先幫您處理。」
   │       (又被轉真人時不加 —— 「專員不在線上」接「我立刻請店長聯繫」自相矛盾)
   │
   ├─ ② 關鍵字命中 action: transfer 的規則       ← 規則層
   │     → handoff(該規則的 category),不問模型
   │
   └─ ③ 帶 transfer_to_human 工具問模型          ← agent 層
          ├─ 回文字     → 跟現在一樣送出
          ├─ 呼叫工具   → handoff(模型給的 category)
          └─ 模型失敗   → fallback(跟現在一樣)
```

非文字訊息(圖片、貼圖)在 ① 之前就已經處理掉了 —— 這一段不變。
但 HUMAN 模式中客人傳圖片,也**不該**回「我只看得懂文字」:① 的判斷要移到
非文字分支之前。

### handoff —— 規則層與 agent 層共用

1. `user.mode = HUMAN`、`mode_changed_at = now`、
   `mode_expires_at = now + company.human_mode_timeout_minutes`
2. 回覆客人該類別的 `script`,並寫進 `chat_histories`(role = assistant)
3. 推播給 `company.staff_notify_to`:
   ```
   【<類別>】<客人顯示名稱或 userId 末 6 碼>
   <客人訊息前 100 字>
   判斷依據:關鍵字「<命中的詞>」   ← 規則層
   判斷依據:AI —— <模型給的 reason> ← agent 層
   ```
   「判斷依據」讓店員(與面試官)一眼看出這次是規則轉的還是模型轉的。

**先回客人、再推播**:reply token 約一分鐘有效,推播沒有時限。

兩層走同一個函式,行為保證一致 —— 不會出現「關鍵字轉的會通知、模型轉的不會」。

### 兩個刻意的取捨

- **`action: apologize`(L1)不進規則層**,照舊交給模型用 prompt 回。它不需要轉真人,
  道歉本來就是模型做得好的事。
- **HUMAN 模式期間 AI 一句都不回**,連「已轉接,請稍候」都不重複。
  罐頭訊息會跟店員在後台的回覆混在一起。

## 四、資料與設定

### Alembic 遷移:`companies` 加兩欄

| 欄位 | 型別 | 說明 |
|---|---|---|
| `escalation_rules` | JSON,NOT NULL,預設 `[]` | seed 時把 `escalation.yaml` 整份存進來 |
| `staff_notify_to` | String(64),可為 NULL | 店員的 userId 或群組 ID。NULL = 不推播,只寫 warning log |

**為什麼存進資料庫、不在執行時讀 yaml:** 換行業靠的是重跑 seed,資料庫才是
「現在服務的是哪一家」唯一的真相來源。執行時去讀 `industries/<industry>/escalation.yaml`
的話,改了 yaml 卻沒重跑 seed,prompt(seed 時組好的)和規則(執行時讀的)
就會講不同的話,而且不會有任何錯誤訊息。

`escalation_rules` 預設 `[]`:既有資料列遷移後規則是空的,規則層與工具都不會啟用,
行為跟現在一樣 —— 重跑一次 seed 才會打開。

### seed

- 加 `--staff-notify-to U...`,用法跟 `--destination` 一樣:沒給是「不動」,不是「清空」
- 每次 seed 都覆寫 `escalation_rules`
- `--reset-history` 同時把這家公司所有客人的 `mode` 重設為 AI、`mode_expires_at` 清空
  (舊對話都清了,還卡在 HUMAN 沒有意義)

### release

```bash
python -m app.cli release --slug bistro
```

把這家公司所有 HUMAN 模式的客人交還給 AI,印出交還了幾位。

### 關鍵字比對

`trigger` 用頓號(、)切開,客人訊息**包含**任何一個詞就算命中。不斷詞、不模糊比對。
多條規則同時命中時取 yaml 裡排在前面的(L3 在前,所以安全優先)。

### 模型設定

主要改 `openrouter/free`、備用改 `nvidia/nemotron-3-super-120b-a12b:free`。
`.env`、`.env.example`、`compose.yaml`、`app/config.py` 四個地方的預設值要一致。

### prompt 樣板(`prompts/system.j2`)

現在的樣板對**所有**升級規則都說「直接回覆 script」。不改的話,模型遇到
「起紅疹」會照 prompt 念 script 而不呼叫工具 —— 客人看起來像被轉了,
實際上沒切 HUMAN、店員沒收到通知。**這是整個設計裡最容易漏掉的一步。**

- `action: transfer` → 「呼叫 `transfer_to_human`,category 填 `<category>`」
- `action: apologize` → 維持「直接回覆 script」

## 五、模型介面

### `complete()`

```python
def complete(messages, *, tools=None, client=None) -> LLMResult
```

- `tools` 不傳時行為完全不變 —— 既有測試不用改
- `LLMResult` 加欄位 `tool_call: ToolCall | None`(`name: str`、`arguments: dict`)
- 同時回了文字與工具呼叫:**以工具為準**
- 「拿不到內容」的判斷放寬為「沒有文字、也沒有工具呼叫」—— 呼叫工具時
  `content` 本來就常常是空的,沿用現在的判斷會把每一次轉真人都當成失敗
- 合計 25 秒上限、主要 → 備用的退回機制沿用

### 工具定義

從 `escalation_rules` 產生,換行業時自動跟著變:

```json
{
  "type": "function",
  "function": {
    "name": "transfer_to_human",
    "description": "客人遇到下列情況時轉給真人,不要自己回答:safety(過敏、食物中毒…)、legal(提告、律師…)…",
    "parameters": {
      "type": "object",
      "properties": {
        "category": {"type": "string", "enum": ["safety", "legal", "money", "privacy"]},
        "reason": {"type": "string"}
      },
      "required": ["category"]
    }
  }
}
```

`enum` 只列 `action: transfer` 的類別。`escalation_rules` 裡沒有任何 transfer 類
時不帶 `tools`。

## 六、錯誤處理

原則:**模糊的時候往「轉真人」那邊倒。**

| 狀況 | 處理 | 理由 |
|---|---|---|
| `category` 不在 enum 裡,或 `arguments` 不是合法 JSON | **照樣轉**,回通用句「這部分我請專人與您聯繫,請稍候。」 | 模型已經表達「需要真人」,參數寫錯不該讓客人被漏掉 |
| 分到的模型不支援 tools(上游 4xx) | 當成這個模型失敗,換下一個;全失敗送 fallback | 沿用既有機制 |
| 推播失敗 | 照樣切 HUMAN,寫 error log | 決策 6 |
| `staff_notify_to` 是 NULL | 不推播,寫 warning log | 沒設定的店家也要能正常運作 |

**推播額度:** LINE 免費方案每月 200 則,一次轉真人用 1 則。示範綽綽有餘,
上線前要注意。

## 七、檔案清單

### 新增

- `alembic/versions/<rev>_escalation_rules_and_staff_notify.py`
- `app/agent/handoff.py` —— 關鍵字比對、工具定義產生、`handoff()`
- `tests/test_handoff.py`

### 修改

- `app/agent/llm.py` —— `tools` 參數、`ToolCall`、內容判斷放寬
- `app/routers/webhook.py` —— 三條路的分流
- `app/models/company.py` —— 兩個新欄位
- `app/cli.py` —— `--staff-notify-to`、`release`、`--reset-history` 重設模式
- `prompts/system.j2` —— transfer 與 apologize 分開寫
- `app/config.py`、`compose.yaml`、`.env.example` —— 模型預設值
- `tests/test_agent.py`、`tests/test_webhook.py`、`tests/test_cli.py`
- `docs/demo-checklist.md` —— 示範流程、第一次設定 `--staff-notify-to`

## 八、測試策略

| 對象 | 要驗的事 |
|---|---|
| 關鍵字比對 | 頓號切開後包含比對;只有 transfer 會觸發;apologize 不會;多條命中取第一條 |
| 工具定義 | enum 只列 transfer 類;沒有 transfer 類時回 None |
| `complete()` | 上游回 `tool_calls` 時解析出來;工具優先於文字;有工具呼叫但 content 空也算成功;不帶 tools 時行為不變 |
| webhook ① | HUMAN 未過期 → 不回覆、不呼叫模型;HUMAN 中傳圖片也不回 |
| webhook ① | HUMAN 已過期 → 切回 AI,回覆開頭多那一句 |
| webhook ② | 關鍵字命中 → 模型呼叫 0 次、回 script、切 HUMAN、推播 |
| webhook ③ | 模型呼叫工具 → 切 HUMAN、回該類 script;參數壞掉 → 仍然轉、回通用句 |
| handoff | 推播失敗 → 仍是 HUMAN;`staff_notify_to` 為 NULL → 不推播 |
| seed / release | `escalation_rules` 寫入;`--staff-notify-to` 沒給不清空;`--reset-history` 重設模式;`release` 交還 |
| 遷移 | `alembic upgrade head` 在 SQLite 與 PostgreSQL 都過(CI 兩個 job) |
| prompt 樣板 | transfer 類的句子提到 `transfer_to_human`,不是「直接回覆」 |

## 九、驗收標準

1. 自動測試全綠,SQLite 與 PostgreSQL 各一輪
2. 手機實測(約 3-4 次模型額度,規則層那一步不花):

| # | 操作 | 預期 |
|---|---|---|
| 1 | 傳「有停車位嗎」 | AI 正常回答 |
| 2 | 傳「我朋友吃完過敏送醫了」 | 回 safety 的 script;**店員手機跳通知**;log 顯示沒有呼叫 OpenRouter |
| 3 | 再傳一句 | AI 不回;店員在官方帳號後台回覆,客人收得到 |
| 4 | `release --slug bistro` 後傳「我女兒吃完全身起紅疹」 | 沒命中關鍵字,**模型呼叫工具**轉真人,店員手機又跳通知 |
| 5 | 換診所行業(`seed --industry clinic ... --reset-history`)後傳「我這個症狀是不是癌症」 | 命中診所 safety 的 trigger「我這個症狀是不是」,回診所的 script,店員手機跳通知 |

第 5 步也是一個**行為改變**:今天(2026-09-24)的實測裡,這句只會回 script;
做完之後同一句會真的轉真人、通知店員。回給客人的文字不變。

第 4 步是整個設計的核心 —— 它證明「模型自己決定要做什麼」。

## 十、一次性手動步驟

- LINE Official Account Manager → **回應設定**:**聊天(Chat)** 要打開,店員才能在
  官方帳號後台回覆。Webhook 與聊天可以同時開;**自動回應訊息**維持關閉
  (既有檢查清單的要求,否則罐頭回覆會蓋掉 AI 的答案)
- 店員(或店員群組)先加 bot 好友,取得 userId 或 groupId 後
  `seed --staff-notify-to <id>`

## 十一、開放問題

- 店員群組的 groupId 要從 webhook 的 `join` 事件或群組訊息取得,而 v1 不處理群組訊息。
  **示範先用個人 userId**:LINE Developers Console → 該 channel 的 **Basic settings**
  → **Your user ID**,就是 channel 管理者自己的 userId,不必寫程式去撈。
  該帳號要先加 bot 好友,推播才送得到。
- 示範要**兩個 LINE 帳號**:一個當客人、一個當店員(收通知、在後台回覆)。
  同一個帳號兼兩個角色的話,通知會出現在同一個對話框裡,跟客人的對話混在一起 ——
  功能測得過,但面試現場看不出「另一支手機跳通知」的效果。
