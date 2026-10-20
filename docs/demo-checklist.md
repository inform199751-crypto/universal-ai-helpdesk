# Demo 前檢查清單

服務從 2026-10-05 起跑在 Google Cloud 的一台小主機上(同一份 docker compose:
`app`、`db`、`ngrok`),**不在筆電上** —— 筆電關機、重開、換網路都不影響 bot。
網址也是固定的(ngrok 固定網域),抄網址、貼 Console、按 Verify 這三件事整個消失了。
**還是不要靠記憶,照這份清單跑一遍。**

**下面所有 `docker compose ...` 指令,都要先 SSH 到雲端主機、`cd` 進主機上 clone 下來的
repo 再跑。** 唯一的例外是 `curl .../health`,在筆電上直接跑就好。
`exec` 一律帶 `-T`(不配虛擬終端機):互動登入後跑沒差別,從筆電用 `ssh` 一行帶指令時也不會卡住。

## 出門前(在家做)

- [ ] GitHub 上 master 最後一次 CI 是綠的(測試、SQLite 與 PostgreSQL 兩輪、三個行業的 validate
      都在 CI 裡跑),筆電上:
      `gh run list --branch master --limit 1`
- [ ] 主機上跑的程式是最新的(在主機上):
      ```bash
      git fetch -q
      git diff --stat HEAD origin/master -- app industries prompts alembic alembic.ini compose.yaml Dockerfile docker-entrypoint.sh pyproject.toml constraints.txt
      ```
      沒有輸出 = 程式和行業內容都跟 master 一樣。只差 `docs/`、`README.md` 是正常的 ——
      只改文件的 PR 合併後不用部署。有輸出就照 [report 第八節第 1 點](report.md)的更新流程:
      `git pull` → `docker compose build app` → `docker compose up -d app`
      (build 與啟動分開跑;up 之後大約要 50 秒才會 `healthy`)。
      資料庫遷移在容器啟動時自動跑(`docker-entrypoint.sh`),不用自己 `alembic upgrade head`。
- [ ] 要 demo 的行業零 ERROR(在容器裡驗,驗的就是映像裡那一份):
      `docker compose exec -T app python -m app.cli validate --industry <要 demo 的行業>`
- [ ] **客人帳號今天還剩幾則。** 每人每天只有 **20 則 AI 回覆**(算的是 AI 回了幾則,台灣午夜歸零)。
      前一天晚上、當天早上自己測太多次,面試時會先撞到上限 —— bot 回一句「今天跟我聊的次數已經到上限了」
      之後就完全安靜,看起來跟壞掉一模一樣。查今天每個客人用掉幾則:
      ```bash
      docker compose exec -T db psql -U helpdesk -d helpdesk -c "SELECT right(u.line_user_id, 4) AS user_tail, u.mode, count(h.id) AS ai_replies_today FROM users u LEFT JOIN chat_histories h ON h.user_id = u.id AND h.role = 'assistant' AND h.created_at >= date_trunc('day', now() AT TIME ZONE 'Asia/Taipei') AT TIME ZONE 'Asia/Taipei' GROUP BY u.id ORDER BY 3 DESC;"
      ```
      快到 20 的話,開場前跑一次換行業的 seed(帶 `--reset-history`,見[換行業](#換行業現場-demo-的王牌))——
      計數是直接從對話紀錄算的,紀錄清掉就歸零;代價是這家店的對話紀錄一起清掉。
      `mode` 那欄是 `HUMAN` 的話,先跑[交還 AI](#轉真人-demo-完要交還-ai) 那道指令。
- [ ] 備援的 OpenRouter 金鑰還有效。主要模型是 NVIDIA,OpenRouter 只在 NVIDIA 失敗時接手
      (免費方案一天 50 次):
      ```bash
      docker compose exec -T app python -c "import os, httpx; r = httpx.get('https://openrouter.ai/api/v1/key', headers={'Authorization': 'Bearer ' + os.environ['OPENROUTER_API_KEY']}); print(r.status_code, r.json().get('data', {}).get('is_free_tier'))"
      ```
      回 `200` 就是金鑰有效。NVIDIA 沒有對應的查詢端點(免費層沒有每日上限,只有每分鐘的),
      它的檢查放在現場「自己先傳一則」那一步看 log。

## 現場(面試前十分鐘)

- [ ] `curl https://unsaid-expend-eagle.ngrok-free.dev/health` 回
      `{"status":"ok",...,"database":"ok"}` —— 筆電上直接跑,不用 SSH
- [ ] SSH 到雲端主機,`docker compose ps` —— 三個都在跑(`app` / `db` / `ngrok`),
      `app` 與 `db` 是 `healthy`
- [ ] 另開一個 SSH 視窗跟著 log 跑,示範時每一步都有證據可以指給面試官看:
      `docker compose logs -f --tail 20 app`
- [ ] **自己先傳一則訊息**,確認有回 —— 不要讓面試官當第一個測試者。
      log 要看到 `模型 nvidia/nemotron-3-super-120b-a12b(NVIDIA)回答完成`;
      如果是 `(OpenRouter)回答完成`,代表這次 NVIDIA 失敗、備援接手了,
      往上找 `失敗,改用` 那一行看原因

**現場兩個最常踩的上限:**

- **每人每天 20 則 AI 回覆**(另外每人每分鐘 5 則)。超過時第一次回提醒、之後安靜,
  log 會有 `限流 客人 …xxxx 規則=user_day`(每分鐘的是 `user_minute`)。
  示範要用的客人帳號,當天就別拿來閒聊測試。
- **NVIDIA 免費層大約每分鐘二十幾次就回 429**(2026-10-16 量到約每分鐘 24 次就開始)。
  每則客人訊息至少打 NVIDIA 兩次(主模型與轉真人分類層同時發),預約這種多步的一則要三、四次 ——
  所以**等它回完再傳下一句,不要連發**。429 時主模型自動改用 OpenRouter(慢幾秒、吃掉一天 50 次的額度),
  分類層那一次當沒判斷;log 會有 `失敗,改用 … HTTP 429`。

服務不在筆電上,所以會議室換 Wi-Fi、筆電睡眠或重開都不影響 bot —— LINE 打的是雲端主機上的
ngrok,而 ngrok 是主機主動往外連出去的。筆電的網路只影響「能不能 SSH 下指令」:
會議室網路擋 SSH 時改用手機熱點,bot 本身照常回覆。

雲端主機本身重開機這條路還沒實際走過(照設計 Docker 開機自動啟動、三個容器 `restart: unless-stopped`
會自己回來,見 [report 第八節第 1 點](report.md))—— 面試前不要去重開它。
健康檢查不通時,SSH 進主機 `docker compose logs ngrok --tail 50` 看 tunnel 有沒有連上,
`docker compose restart ngrok` 重連。

## 第一次串接時做過的(已經做完,換主機也不用重做)

這些設定存在 LINE 那邊和資料庫裡,跟服務跑在哪台機器無關。記下來是為了換帳號、換店家時照著做。
指令一樣是在主機上跑。

LINE Developers Console → 你的 Messaging API channel:

- [ ] `docker compose exec -T app python -m app.cli set-webhook --slug bistro --url https://unsaid-expend-eagle.ngrok-free.dev`
      —— 一輩子只跑一次,不必開 Console、也不必按 Verify
- [ ] Console 裡打開 **Use webhook**
- [ ] 打開 **Webhook redelivery**(第二層保險:沒回 2xx 時 LINE 會重送,
      而 `chat_histories.line_message_id` 的 unique 索引擋得住重複回答)
- [ ] 關掉 **Auto-reply messages** 與 **Greeting messages**,
      否則官方的罐頭回覆會蓋掉 AI 的答案
- [ ] 補上 `--destination`,把交叉比對打開。bot 的 userId 不必等訊息進來,
      也不必再給憑證 —— 打 `GET https://api.line.me/v2/bot/info`
      帶 `Authorization: Bearer <access token>`,回應裡的 `userId` 就是:
      ```bash
      docker compose exec -T app python -m app.cli seed --industry restaurant --slug bistro --destination Uxxxx
      ```
      沒做這步不會壞,但「路徑說是 A 公司、body 卻是 B 公司的 bot」這道
      防線是關著的,而且不會有任何跡象。
- [ ] LINE Official Account Manager → 回應設定 → 打開**聊天**(真人客服在官方帳號後台回覆)
- [ ] **店員 = 登入 LINE Developers Console 的那個帳號。** Console → Basic settings →
      **Your user ID** 顯示的是「目前登入 Console 的帳號」的 userId,不是客人的、
      也不是 bot 的。所以那個帳號就是店員;**客人必須用另一個 LINE 帳號**。
      店員帳號先加 bot 好友(不加,推播送不到),抄下那串 userId,
      跑這支 seed。`--reset-history` 建議加:舊對話和舊的轉接話術會留在最近十則的視窗裡,
      模型看到會學著念話術、而不是呼叫工具:
      ```bash
      docker compose exec -T app python -m app.cli seed --industry restaurant --slug bistro --staff-notify-to Uxxxx --reset-history
      ```
      **不做的後果(不是「不會壞」):**
      - 遷移剛跑完、還沒跑過任何一次 seed:`escalation_rules` 是空的,
        **什麼都不會轉** —— 規則層和工具都沒啟用,行為跟改版前一樣。要 seed 一次才會打開。
      - seed 跑了、但沒給 `--staff-notify-to`:轉真人**照樣發生**(客人收到話術、進 HUMAN 模式),
        但**沒有任何人收到通知**,客人會在 30 分鐘內都收不到回覆。log 只有一行 warning。
      - **目前線上就是這個狀態**:還沒準備第二個 LINE 帳號當店員,所以沒設 `staff_notify_to`。
        下面示範步驟裡的「店員手機跳通知」現在不會出現,log 會有
        `沒有設定 staff_notify_to,這次轉真人沒有通知任何人`。要示範通知,先備好第二個帳號、跑上面這支 seed。
- [ ] **店員回覆要到 LINE Official Account Manager 的聊天(網頁或手機 app)去回,
      千萬不要在通知跳出來的 bot 對話框裡回。** 推播通知出現在「店員帳號 ↔ bot」那個
      對話框,在那裡打字只會送進 bot 的 webhook,客人永遠收不到。
      另外,**店員帳號傳給 bot 的訊息不會被回應**(程式擋掉,不當客人處理)——
      所以店員帳號不能拿來當客人測試。

## 現場要示範的六個步驟

| # | 操作 | 預期 |
|---|---|---|
| 1 | 傳「有停車位嗎」 | AI 正常回答;log 有 `(NVIDIA)回答完成` |
| 2 | 傳「我朋友吃完過敏送醫了」 | 回 safety 的 script;**店員手機跳通知**(要先設 `staff_notify_to`,見上);log 有 `轉真人 category=safety`,而且這一則沒有 `回答完成` —— 關鍵字層直接轉,沒有呼叫模型 |
| 3 | 再傳一句 | AI 不回(log:`由真人接手中,AI 不回`);店員在官方帳號後台回覆,客人收得到 |
| 4 | [交還 AI](#轉真人-demo-完要交還-ai) 後傳「我女兒吃完全身起紅疹」 | 沒命中關鍵字,由**模型呼叫工具**或**轉真人分類層**(log:`分類層 判斷=safety`)轉真人 |
| 5 | 換診所行業(`seed --industry clinic ... --reset-history`)後傳「我這個症狀是不是癌症」 | 命中診所 safety 的 trigger「我這個症狀是不是」,回診所的 script |
| 6 | 換回餐廳(`seed --industry restaurant --slug bistro --reset-history`)、交還 AI 後傳「我要訂這週五晚上 8 點,4 位,王小明 0912345678」 | 模型先查空位再建立預約,回確認句;`docker compose exec -T app python -m app.cli bookings --slug bistro` 列得出那筆;log 有 `agent 步數=3` |

**示範要兩個 LINE 帳號**:一個當客人,一個當店員(= 登入 Console 的帳號;收通知、
到官方帳號後台的聊天回覆)。兩個不能是同一個帳號 —— 店員帳號傳的訊息不會被回應。

### 轉真人 demo 完要交還 AI

轉真人之後客人會卡在 HUMAN 模式 30 分鐘才自動交還,這段時間 AI 一句都不會回 ——
接下一段示範前、**面試結束離開前**都要跑一次(在主機上):

```bash
docker compose exec -T app python -m app.cli release --slug bistro
```

跑完會印 `✓ 已把 N 位客人交還給 AI`。

## 換行業(現場 demo 的王牌)

同一個 LINE 帳號、同一個 webhook 網址,**不用重啟服務、不用再給憑證**。
跑完下一則訊息就是新行業。**一定要 SSH 到主機、用 `docker compose exec -T app` 進容器跑** ——
服務讀的是容器網路裡的 PostgreSQL;在筆電上直接跑 `python -m app.cli seed`,
寫進的是筆電那份開發用的 SQLite,demo 現場看起來像沒生效。

```bash
# 餐飲 —— 青木餐酒館
docker compose exec -T app python -m app.cli seed --industry restaurant --slug bistro --reset-history

# 電商 —— 好日子生活選物
docker compose exec -T app python -m app.cli seed --industry ecommerce --slug bistro --reset-history

# 診所 —— 晴日皮膚科(三個裡面最有說服力)
docker compose exec -T app python -m app.cli seed --industry clinic --slug bistro --reset-history
```

忘記有哪些行業、哪個資料夾是哪一家:`docker compose exec -T app python -m app.cli list`。
對照表也寫在 [industries/README.md](../industries/README.md)。

**三個容易搞混的地方:**

- `--slug bistro` 是 **webhook 路徑**,不是行業。三個行業共用同一個路徑,
  所以換行業完全不必碰 LINE Console。這個 slug 當初照餐廳取名,現在看起來
  矛盾,但改它要連 webhook 網址一起換,不值得動。
- `--reset-history` 清掉上一個行業的對話。不清的話,人設換成診所了,
  模型的最近十則卻還在講餐廳停車位 —— 它會被帶偏,現場很難看。
  它也會把卡在 HUMAN 的客人交還 AI,並讓今天的每人計數歸零(計數是從對話紀錄算的)。
- 行業內容(`industries/`)是 build 時複製進映像的。改了 yaml 要重 build、
  重跑一次 seed 才會生效,只 `git pull` 不夠。

**換完各傳一句驗:**

| 行業 | 傳這句 | 應該回 |
|---|---|---|
| 餐飲 | `有停車位嗎` | 門口兩格,滿了對面有收費停車場 |
| 電商 | `運費怎麼算` | 滿 990 免運、未滿 80、超商取貨 60 |
| 診所 | `我這個症狀是不是癌症` | 拒答,請來電或就醫;**話術裡不會出現「診斷」兩個字** |

## 如果當場壞掉

先 SSH 到雲端主機。

1. `docker compose logs app --tail 50`,錯誤會印在那裡
2. LINE 說 **530**?→ 網址是固定的,所以 530 不是「網址過期」,
   而是服務或 tunnel 沒起來。`docker compose ps` 看哪個不在,
   `docker compose logs <服務名> --tail 50` 看原因
3. 健康檢查正常、bot 卻不回?→ 多半是上限,不是壞掉。log 有 `限流 … 規則=user_day`
   (或 `user_minute`)是撞到每人上限;有 `由真人接手中,AI 不回` 是客人還在 HUMAN 模式,
   跑[交還 AI](#轉真人-demo-完要交還-ai);有 `HTTP 429` 是 NVIDIA 限流,會退到 OpenRouter,慢但會回
4. SSH 連不上?→ 換手機熱點再試。bot 跑在雲端,SSH 連不上不影響客人收到回覆,
   只是不能現場換行業、交還 AI
5. 都不行 → 講設計文件。[docs/report.md](report.md) 第七節的坑
   (含 Tailscale Funnel 為什麼換成 ngrok 的除錯過程)本身就是面試素材,
   東西沒跑起來不代表沒東西可談
