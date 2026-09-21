"""資料層檢查。

兩個等級的意思不同,不要混用:
  ERROR —— 資料壞了,擋 seed。
  BLOCK —— 資料沒壞,但不該就這樣交給客戶上線。不擋 seed,
           但要列在報告最上方,在導入會議上確認過才算解除。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

REQUIRED_COMPANY = ("name", "industry", "hours", "contact", "tone", "forbidden_phrases")
REQUIRED_HIGH_RISK = {
    "legal": "法律(提告、消保官)",
    "safety": "醫療 / 人身安全",
    "money": "金錢爭議(退款、重複扣款)",
    "privacy": "個資",
}
PLACEHOLDERS = ("請填入", "TODO", "XXX", "待補")
HOURS_RE = re.compile(r"\d{1,2}:\d{2}\s*[-~]\s*\d{1,2}:\d{2}")


@dataclass(frozen=True)
class Finding:
    level: str  # "ERROR" 或 "BLOCK"
    rule: int
    message: str


def _texts(data: dict[str, Any]):
    """走訪所有會被客人看到的字串,附上來源描述。"""
    for i, item in enumerate(data.get("faq") or []):
        yield f"faq[{i}].a", str(item.get("a", ""))
    for i, item in enumerate(data.get("policies") or []):
        yield f"policies[{i}].content", str(item.get("content", ""))
    for i, item in enumerate(data.get("escalation") or []):
        yield f"escalation[{i}].script", str(item.get("script", ""))
    for i, item in enumerate(data.get("glossary") or []):
        yield f"glossary[{i}].meaning", str(item.get("meaning", ""))


def validate(data: dict[str, Any], *, max_text_length: int = 5000) -> list[Finding]:
    out: list[Finding] = []
    company = data.get("company") or {}
    forbidden = [p for p in (company.get("forbidden_phrases") or []) if p]

    # 規則 2:必填欄位
    for key in REQUIRED_COMPANY:
        if not company.get(key):
            out.append(Finding("ERROR", 2, f"company.yaml 缺少必填欄位:{key}"))
    for i, item in enumerate(data.get("faq") or []):
        if not item.get("q") or not item.get("a"):
            out.append(Finding("ERROR", 2, f"faq[{i}] 的 q 或 a 是空的"))

    for where, text in _texts(data):
        # 規則 3:答案違反自己的禁語清單
        for phrase in forbidden:
            if phrase in text:
                out.append(Finding("ERROR", 3, f"{where} 命中自己的禁語「{phrase}」"))
        # 規則 4:超過 LINE 的訊息長度上限
        if len(text) > max_text_length:
            out.append(Finding("ERROR", 4,
                               f"{where} 長 {len(text)} 字,超過 {max_text_length} 上限"))
        # 規則 6:殘留 placeholder
        for ph in PLACEHOLDERS:
            if ph in text:
                out.append(Finding("BLOCK", 6, f"{where} 還留著「{ph}」"))

    # 規則 5:高風險情境涵蓋
    covered = {e.get("category") for e in (data.get("escalation") or [])}
    for key, label in REQUIRED_HIGH_RISK.items():
        if key not in covered:
            out.append(Finding("BLOCK", 5, f"escalation 沒有涵蓋「{label}」情境"))

    # 規則 7:術語與禁語衝突
    for i, item in enumerate(data.get("glossary") or []):
        term = str(item.get("term", ""))
        if term and term in forbidden:
            out.append(Finding("ERROR", 7, f"glossary[{i}] 的術語「{term}」同時在禁語清單裡"))

    # 規則 8:營業時間可解析
    hours = str(company.get("hours", ""))
    if hours and not HOURS_RE.search(hours):
        out.append(Finding("ERROR", 8, f"company.hours 解析不了:「{hours}」"))

    # BLOCK 排前面 —— 它們是導入會議的議程,不該被淹沒在 ERROR 裡
    return sorted(out, key=lambda f: (f.level != "BLOCK", f.rule))
