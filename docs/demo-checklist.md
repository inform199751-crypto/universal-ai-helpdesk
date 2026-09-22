# Demo 前檢查清單

Quick Tunnel 的網址每次重啟都會變,所以這份清單每次 demo 前都要跑一遍。
**不要靠記憶。**

## 出門前(在家做)

- [ ] `pytest` 全綠
- [ ] `python -m app.cli validate --industry <要 demo 的行業>` 零 ERROR
- [ ] `alembic upgrade head`
- [ ] OpenRouter 金鑰還有額度:
      `curl -H "Authorization: Bearer $OPENROUTER_API_KEY" https://openrouter.ai/api/v1/key`

## 現場(面試前十分鐘)

- [ ] `python scripts/dev.py --slug bistro`
- [ ] 等等號框印出「已自動寫回 LINE Console」
- [ ] **自己先傳一則訊息**,確認有回 —— 不要讓面試官當第一個測試者

`--slug` 會讓腳本自己把當次的 tunnel 網址 PUT 回 LINE,不必抄網址、
不必開 Console、不必按 Verify。**忘了更新的症狀是 530,而 530 長得
完全不像「網址過期」** —— 這一步自動化掉的價值就在這裡。

框裡如果印的是「請手動貼上」,才需要回到舊流程:複製那串 webhook 網址、
貼進 Console → Messaging API → Webhook URL、按 Verify。

## 第一次串接才要做的(之後不用重做)

LINE Developers Console → 你的 Messaging API channel:

- [ ] Webhook URL 填好、按 Verify、打開 **Use webhook**
- [ ] 打開 **Webhook redelivery**(第二層保險:沒回 2xx 時 LINE 會重送,
      而 `chat_histories.line_message_id` 的 unique 索引擋得住重複回答)
- [ ] 關掉 **Auto-reply messages** 與 **Greeting messages**,
      否則官方的罐頭回覆會蓋掉 AI 的答案
- [ ] 補上 `--destination`,把交叉比對打開。bot 的 userId 不必等訊息進來,
      也不必再給憑證 —— 打 `GET https://api.line.me/v2/bot/info`
      帶 `Authorization: Bearer <access token>`,回應裡的 `userId` 就是:
      ```bash
      python -m app.cli seed --industry restaurant --slug bistro --destination Uxxxx
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
跑完下一則訊息就是新行業。

```bash
# 餐飲 —— 微醺之夜 Bistro
python -m app.cli seed --industry restaurant --slug bistro --reset-history

# 電商 —— 好日子生活選物
python -m app.cli seed --industry ecommerce --slug bistro --reset-history

# 診所 —— 晴日皮膚科(三個裡面最有說服力)
python -m app.cli seed --industry clinic --slug bistro --reset-history
```

Windows PowerShell 要把 `python` 換成 `.\.venv\Scripts\python.exe`。

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

1. 先看 `scripts/dev.py` 的終端機,錯誤會印在那裡
2. LINE 說 **530**?→ tunnel 斷了或網址換了。重跑 `--slug` 那道指令就會自己接回去
3. 電腦的網路換了沒?(會議室 Wi-Fi 換了,tunnel 要重開)
4. 都不行 → 講設計文件。**第二節決策紀錄與第六節的坑本身就是面試素材**,
   東西沒跑起來不代表沒東西可談
