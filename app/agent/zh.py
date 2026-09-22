"""把混進回覆裡的簡體字轉成繁體。

為什麼要有這一層:prompt 已經明文規定「一律使用台灣習慣的繁體中文」,
但那只降低漂移,消不掉 —— 免費模型的訓練語料大量是簡體,真機上就出現過
「停车位」。而台灣店家的客服回簡體字,是客人一眼看得到的品質瑕疵。

**弱模型要靠程式兜底,不能只靠 prompt。**

用 s2t 而不是 s2twp:s2twp 會額外做台灣用詞在地化(例如把「软件」換成
「軟體」),那也會改寫本來正確的詞彙,有機會動到品名或專有名詞。
s2t 只換字,不改詞 —— 這一層只該修模型寫錯的字,不該重寫店家的用語。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_cc = None


def _converter():
    """延遲建立。OpenCC 要載入字典,不該在 import 時就付這個成本 ——
    validate 與 list 這些完全用不到它的指令也會被拖慢。"""
    global _cc
    if _cc is None:
        from opencc import OpenCC

        _cc = OpenCC("s2t")
    return _cc


def ensure_traditional(text: str) -> str:
    """回傳繁體版本。本來就是繁體的原樣回傳,英文與數字不受影響。

    轉換真的改到東西時寫一行 warning —— 那是「模型今天漂移了幾次」的
    唯一來源,而這個數字比「我們有規定它用繁體」有說服力得多。

    任何例外都吞掉並原樣回傳:這是品質修飾,不是主流程,不該讓一則
    本來算得出來的答案因為它而送不出去。
    """
    if not text:
        return text
    try:
        out = _converter().convert(text)
    except Exception:  # noqa: BLE001
        logger.warning("簡繁轉換失敗,原樣送出", exc_info=True)
        return text
    if out != text:
        changed = sum(1 for a, b in zip(text, out) if a != b)
        logger.warning("模型輸出混入簡體字,已轉為繁體(%d 個字不同)", changed)
    return out
