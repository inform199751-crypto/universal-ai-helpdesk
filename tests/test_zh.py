"""簡繁轉換的兜底。

真機上抓到模型把「停車位」寫成「停车位」之後,prompt 已經加了語言規定,
但 prompt 只能降低漂移、消不掉 —— 免費模型的語料大量是簡體。

這一層是程式端的兜底。語音訂位那個專案學到的同一課:
**弱模型要靠程式兜底,不能只靠 prompt。**
"""

from app.agent.zh import ensure_traditional


def test_converts_simplified_mixed_into_traditional():
    """真機上實際出現過的那一句。"""
    out = ensure_traditional("店門口有兩個停车位,滿了的話對面有收費停車場。")
    assert "停車位" in out
    assert "车" not in out


def test_leaves_correct_traditional_untouched():
    """最重要的一條:這層會套在每一則回覆上,絕不能改壞本來就對的答案。"""
    for s in ("店門口有兩格停車位,滿了的話對面有收費停車場。",
              "滿 990 元免運,未滿收 80 元。超商取貨一律 60 元。",
              "這需要醫師當面評估才能回答,我無法在線上判斷。",
              "我目前只看得懂文字訊息,麻煩您用打字的跟我說 🙏"):
        assert ensure_traditional(s) == s


def test_leaves_non_chinese_untouched():
    for s in ("Sorry, we only accept text messages.", "02-8765-4321", "", "990"):
        assert ensure_traditional(s) == s


def test_never_raises_even_if_the_converter_breaks(monkeypatch):
    """轉換是品質修飾,不是主流程。它壞掉時要原樣回傳,
    不能讓一則本來算得出來的答案因此送不出去。"""
    import app.agent.zh as zh

    class Boom:
        def convert(self, text):
            raise RuntimeError("字典壞了")

    monkeypatch.setattr(zh, "_converter", lambda: Boom())
    assert ensure_traditional("店門口有兩个停车位") == "店門口有兩个停车位"
