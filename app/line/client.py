"""送訊息給 LINE。

reply 與 push 的差別很重要:
  reply —— 用 replyToken,只有一分鐘、只能用一次,但不計入推播額度。
  push  —— 用 userId,隨時可送,但會吃掉官方帳號的推播額度。

所以策略是 reply 優先、失敗才 push。全用 push 會燒額度,
全用 reply 則 LLM 一慢就整則掉訊息。
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.line.me"
REPLY_PATH = "/v2/bot/message/reply"
PUSH_PATH = "/v2/bot/message/push"
SUFFIX = "(訊息過長已截斷)"


def truncate_for_line(text: str, limit: int = 5000) -> str:
    """LINE 單則文字訊息有長度上限,超過會整則失敗 —— 客人什麼都收不到,
    而錯誤只留在伺服器 log 裡。

    不分則:分則會讓客人收到一串沒頭沒尾的訊息,而客服答案本來就不該
    那麼長。會超過上限本身就是資料寫壞了,應該在 validate 階段就發現。
    """
    if len(text) <= limit:
        return text
    return text[: limit - len(SUFFIX)] + SUFFIX


class LineClient:
    def __init__(self, access_token: str, timeout: float = 10.0) -> None:
        self._token = access_token.strip()
        self._http = httpx.Client(base_url=API_BASE, timeout=timeout)

    def _post(self, path: str, payload: dict) -> bool:
        try:
            r = self._http.post(
                path,
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except httpx.HTTPError as exc:
            logger.warning("LINE %s 連線失敗:%s", path, exc)
            return False
        if r.status_code != 200:
            logger.warning("LINE %s 回 %s:%s", path, r.status_code, r.text[:300])
            return False
        return True

    def reply(self, reply_token: str, text: str) -> bool:
        return self._post(REPLY_PATH, {
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": text}],
        })

    def push(self, to: str, text: str) -> bool:
        return self._post(PUSH_PATH, {
            "to": to,
            "messages": [{"type": "text", "text": text}],
        })

    def send(self, reply_token: str, user_id: str, text: str) -> bool:
        """先 reply,失敗改 push。兩個都失敗就放棄 ——
        再重試只會讓客人收到延遲很久的孤立訊息。"""
        if reply_token and self.reply(reply_token, text):
            return True
        logger.info("reply 失敗,改用 push 送給 %s", user_id)
        return self.push(user_id, text)
