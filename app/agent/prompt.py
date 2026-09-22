"""把人設、最近對話與這一句話組成 messages 陣列。"""

from __future__ import annotations


def build_messages(system_prompt: str, history: list[dict],
                   user_text: str) -> list[dict]:
    """history 由舊到新,每筆 {"role": "user"|"assistant", "content": str}。"""
    return [
        {"role": "system", "content": system_prompt},
        *[{"role": m["role"], "content": m["content"]} for m in history],
        {"role": "user", "content": user_text},
    ]
