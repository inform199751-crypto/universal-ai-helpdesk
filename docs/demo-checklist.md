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

- [ ] `python scripts/dev.py`
- [ ] 抄下印出來的 `https://xxx.trycloudflare.com`
- [ ] `curl <網址>/health` 回 `{"status":"ok"}`
- [ ] LINE Console → Webhook URL 改成 `<網址>/webhook/<slug>`
- [ ] 按 **Verify**,要 Success
- [ ] **自己先傳一則訊息**,確認有回 —— 不要讓面試官當第一個測試者

## 第一次串接才要做的(之後不用重做)

LINE Developers Console → 你的 Messaging API channel:

- [ ] Webhook URL 填好、按 Verify、打開 **Use webhook**
- [ ] 打開 **Webhook redelivery**(第二層保險:沒回 2xx 時 LINE 會重送,
      而 `chat_histories.line_message_id` 的 unique 索引擋得住重複回答)
- [ ] 關掉 **Auto-reply messages** 與 **Greeting messages**,
      否則官方的罐頭回覆會蓋掉 AI 的答案
- [ ] 補上 `--destination`,把交叉比對打開:
      先照上面接好,傳一則訊息,伺服器 log 會印出
      `尚未設定 destination,本次收到的是 Uxxxx`,把那串抄下來重跑一次 seed:
      ```bash
      python -m app.cli seed --industry restaurant --slug bistro \
          --channel-secret <同上> --channel-token <同上> --destination Uxxxx
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

想證明「換行業只換資料」,當場再跑一次 seed 換成診所,
同一個 LINE 帳號、同一個 webhook,傳 `我這個症狀是不是癌症` ——
應該拒答並請對方來電或就醫。

```bash
python -m app.cli seed --industry clinic --slug bistro \
    --channel-secret <同上> --channel-token <同上> --destination <同上>
```

## 如果當場壞掉

1. 先看 `scripts/dev.py` 的終端機,錯誤會印在那裡
2. 網址變了沒?→ 重貼 Console、重按 Verify
3. 電腦的網路換了沒?(會議室 Wi-Fi 換了,tunnel 要重開)
4. 都不行 → 講設計文件。**第二節決策紀錄與第六節的坑本身就是面試素材**,
   東西沒跑起來不代表沒東西可談
