"""LINE 憑證的對稱加密。

為什麼不直接存明文:channel_secret 與 access_token 等於別人官方帳號的
控制權。資料庫備份、SELECT * 的截圖、不小心 commit 的 dump —— 任何一次
外洩都是別人家的帳號被接管。
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


@lru_cache
def _cipher() -> Fernet:
    return Fernet(get_settings().fernet_key.encode())


def encrypt(plaintext: str) -> bytes:
    return _cipher().encrypt(plaintext.encode("utf-8"))


def decrypt(token: bytes) -> str:
    try:
        return _cipher().decrypt(token).decode("utf-8")
    except InvalidToken as exc:
        # 換成 ValueError,呼叫端不必 import cryptography 才接得到
        raise ValueError("憑證解密失敗:金鑰不對或密文被竄改") from exc
