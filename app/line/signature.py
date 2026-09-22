"""LINE webhook 簽章驗證。

必須用收到的原始位元組計算。把 body parse 完再重新序列化,位元組就變了,
簽章永遠對不上 —— 而且自己寫測試時兩邊用同一個序列化器,所以一定會過,
只有 LINE 真的傳過來才會壞。
"""

from __future__ import annotations

import base64
import hashlib
import hmac


def compute_signature(channel_secret: str, body: bytes) -> str:
    digest = hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_signature(channel_secret: str, body: bytes, header: str | None) -> bool:
    if not header:
        return False
    expected = compute_signature(channel_secret, body)
    # compare_digest:避免用 == 比較造成時間差旁路
    return hmac.compare_digest(expected, header)
