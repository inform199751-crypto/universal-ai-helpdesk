import pytest

from app.crypto import decrypt, encrypt


def test_roundtrip():
    assert decrypt(encrypt("my-channel-secret")) == "my-channel-secret"


def test_ciphertext_does_not_contain_plaintext():
    """密文裡不能看得到原文 —— 這是這個模組存在的唯一理由。"""
    assert b"my-channel-secret" not in encrypt("my-channel-secret")


def test_same_plaintext_gives_different_ciphertext():
    """Fernet 每次加密都帶不同的 IV,兩次結果不該一樣。"""
    assert encrypt("same") != encrypt("same")


def test_decrypt_rejects_tampered_token():
    token = bytearray(encrypt("secret"))
    token[-1] ^= 0xFF
    with pytest.raises(ValueError):
        decrypt(bytes(token))


def test_roundtrip_handles_unicode():
    assert decrypt(encrypt("金鑰🔑")) == "金鑰🔑"
