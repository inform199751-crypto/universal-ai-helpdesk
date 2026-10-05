# Demo 前檢查清單

網址現在是固定的(ngrok 固定網域),重啟、重開機都不會變 —— 抄網址、
貼 Console、按 Verify 這三件事整個消失了,現場流程從六步變三步。
**還是不要靠記憶,照這份清單跑一遍。**

## 出門前(在家做)

- [ ] `pytest` 全綠
- [ ] `python -m app.cli validate --industry <要 demo 的行業>` 零 ERROR
- [ ] `alembic upgrade head`
- [ ] OpenRouter 金鑰還有額度:
      `curl -H "Authorization: Bearer $OPENROUTER_API_KEY" https://openrouter.ai/api/v1/key`

## 現場(面試前十分鐘)

- [ ] `docker compose ps` —— 三個都在跑(`app` / `db` / `ngrok`)
- [ ] `curl https://unsaid-expend-eagle.ngrok-free.dev/health` 回
      `{"status":"ok",...,"database":"ok"}`
- [ ] **自己先傳一則訊息**,確認有回 —— 不要讓面試官當第一個測試者

**網址是固定的,不用抄、不用貼、不用按 Verify。** 電腦重開過也一樣
(前提是 Docker Desktop 有設成開機時啟動,見 [README](../README.md))。

會議室換 Wi-Fi 不影響 —— 對外連線是容器主動連出去建立的,不看你這邊
連的是哪個網路。真的連不上時,`docker compose logs ngrok --tail 50`
看有沒有連上,`docker compose restart ngrok` 重連。

## 第一次串接才要做的(之後不用重做)

LINE Developers Console → 你的 Messaging API channel:

- [ ] `docker compose exec app python -m app.cli set-webhook --slug bistro --url https://unsaid-expend-eagle.ngrok-free.dev`
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
      docker compose exec app python -m app.cli seed --industry restaurant --slug bistro --destination Uxxxx
      ```
      沒做這步不會壞,但「路徑說是 A 公司、body 卻是 B 公司的 bot」這道
      防線是關著的,而且不會有任何跡象。
- [ ] LINE Official Account Manager → 回應設定 → 打開**聊天**(真人客服在官方帳號後台回覆)
- [ ] **店員 = 登入 LINE Developers Console 的那個帳號。** Console → Basic settings →
      **Your user ID** 顯示的是「目前登入 Console 的帳號」的 userId,不是客人的、
      也不是 bot 的。所以那個帳號就是店員;**客人必須用另一個 LINE 帳號**。
      店員帳號先加 bot 好友(不加,推播送不到),抄下那串 userId,
      跑這支 seed。它是遷移之後**第一支完整的 seed**,`--reset-history` 不能省:
      目前線上那一列是電商(`ecommerce`),換成餐飲時不清的話,舊對話和舊的轉接話術
      會留在最近十則的視窗裡,模型看到會學著念話術、而不是呼叫工具:
      ```bash
      docker compose exec app python -m app.cli seed --industry restaurant --slug bistro --staff-notify-to Uxxxx --reset-history
      ```
      **不做的後果(不是「不會壞」):**
      - 遷移剛跑完、還沒跑過任何一次 seed:`escalation_rules` 是空的,
        **什麼都不會轉** —— 規則層和工具都沒啟用,行為跟改版前一樣。要 seed 一次才會打開。
      - seed 跑了、但沒給 `--staff-notify-to`:轉真人**照樣發生**(客人收到話術、進 HUMAN 模式),
        但**沒有任何人收到通知**,客人會在 30 分鐘內都收不到回覆。log 只有一行 warning。
- [ ] **店員回覆要到 LINE Official Account Manager 的聊天(網頁或手機 app)去回,
      千萬不要在通知跳出來的 bot 對話框裡回。** 推播通知出現在「店員帳號 ↔ bot」那個
      對話框,在那裡打字只會送進 bot 的 webhook,客人永遠收不到。
      另外,**店員帳號傳給 bot 的訊息不會被回應**(程式擋掉,不當客人處理)——
      所以店員帳號不能拿來當客人測試。

## 現場要示範的五個步驟

| # | 操作 | 預期 |
|---|---|---|
| 1 | 傳「有停車位嗎」 | AI 正常回答 |
| 2 | 傳「我朋友吃完過敏送醫了」 | 回 safety 的 script;**店員手機跳通知**;log 顯示沒有呼叫 OpenRouter |
| 3 | 再傳一句 | AI 不回;店員在官方帳號後台回覆,客人收得到 |
| 4 | `release --slug bistro` 後傳「我女兒吃完全身起紅疹」 | 沒命中關鍵字,**模型呼叫工具**轉真人,店員手機又跳通知 |
| 5 | 換診所行業(`seed --industry clinic ... --reset-history`)後傳「我這個症狀是不是癌症」 | 命中診所 safety 的 trigger「我這個症狀是不是」,回診所的 script,店員手機跳通知 |

**示範要兩個 LINE 帳號**:一個當客人,一個當店員(= 登入 Console 的帳號;收通知、
到官方帳號後台的聊天回覆)。兩個不能是同一個帳號 —— 店員帳號傳的訊息不會被回應。

轉真人之後客人會卡在 HUMAN 模式 30 分鐘。要接著示範下一段:

```bash
docker compose exec app python -m app.cli release --slug bistro
```

## 換行業(現場 demo 的王牌)

同一個 LINE 帳號、同一個 webhook 網址,**不用重啟服務、不用再給憑證**。
跑完下一則訊息就是新行業。**用 `docker compose exec` 進 `app` 容器跑** ——
在主機上直接跑 `python -m app.cli seed` 會寫進主機那份 SQLite,不是容器裡
服務真正在讀的 PostgreSQL,demo 現場看起來像沒生效。

```bash
# 餐飲 —— 微醺之夜 Bistro
docker compose exec app python -m app.cli seed --industry restaurant --slug bistro --reset-history

# 電商 —— 好日子生活選物
docker compose exec app python -m app.cli seed --industry ecommerce --slug bistro --reset-history

# 診所 —— 晴日皮膚科(三個裡面最有說服力)
docker compose exec app python -m app.cli seed --industry clinic --slug bistro --reset-history
```

忘記有哪些行業、哪個資料夾是哪一家:`docker compose exec app python -m app.cli list`。
對照表也寫在 [industries/README.md](../industries/README.md)。

**兩個容易搞混的地方:**

- `--slug bistro` 是 **webhook 路徑**,不是行業。三個行業共用同一個路徑,
  所以換行業完全不必碰 LINE Console。這個 slug 當初照餐廳取名,現在看起來
  矛盾,但改它要連 webhook 網址一起換,不值得動。
- `--reset-history` 清掉上一個行業的對話。不清的話,人設換成診所了,
  模型的最近十則卻還在講餐廳停車位 —— 它會被帶偏,現場很難看。

**換完各傳一句驗:**

| 行業 | 傳這句 | 應該回 |
|---|---|---|
| 餐飲 | `有停車位嗎` | 門口兩格,滿了對面有收費停車場 |
| 電商 | `運費怎麼算` | 滿 990 免運、未滿 80、超商取貨 60 |
| 診所 | `我這個症狀是不是癌症` | 拒答,請來電或就醫;**話術裡不會出現「診斷」兩個字** |

## 如果當場壞掉

1. `docker compose logs app --tail 50`,錯誤會印在那裡
2. LINE 說 **530**?→ 現在網址是固定的,所以 530 不再是「網址過期」,
   而是服務或 tunnel 沒起來。`docker compose ps` 看哪個不在,
   `docker compose logs <服務名> --tail 50` 看原因
3. 都不行 → 講設計文件。[docs/report.md](report.md) 第七節的坑
   (含 Tailscale Funnel 為什麼換成 ngrok 的除錯過程)本身就是面試素材,
   東西沒跑起來不代表沒東西可談
