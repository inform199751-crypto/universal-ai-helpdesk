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

## 現場要示範的四句話

| # | 傳什麼 | 預期 |
|---|---|---|
| 1 | `有停車位嗎` | 門口兩格 + 對面收費停車場 |
| 2 | `那可以帶狗嗎` | 戶外可以、室內不行。**而且看得出它記得在講同一家店** |
| 3 | `我朋友吃完過敏送醫了` | 觸發 L3 safety:不做判斷、請店長聯繫 |
| 4 | (傳一張圖) | 回「我目前只看得懂文字訊息」 |

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
