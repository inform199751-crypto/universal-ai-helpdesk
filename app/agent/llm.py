"""呼叫 OpenRouter。

三個踩過的坑都在這裡處理:
  1. OpenRouter 會用 HTTP 200 包著 error 物件回來(provider 暫時滿載)。
     只看 status_code 會誤判成功,拿空字串當答案送給客人。
  2. 推理模型會把英文內部思考當答案唸出來 —— 要 reasoning.exclude。
  3. max_tokens 太小會讓答案被截斷,看起來像模型講到一半斷線。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from app.config import get_settings


class LLMError(RuntimeError):
    """呼叫失敗,或回來的東西不能當答案用。"""


@dataclass(frozen=True)
class LLMResult:
    text: str
    token_count: int | None
    latency_ms: int


def complete(messages: list[dict], *, client: httpx.Client | None = None) -> LLMResult:
    s = get_settings()
    payload = {
        "model": s.openrouter_model,
        "messages": messages,
        "max_tokens": s.llm_max_tokens,
        "reasoning": {"exclude": True},
    }
    own = client is None
    http = client or httpx.Client(timeout=s.llm_timeout_seconds)
    started = time.monotonic()
    try:
        r = http.post(
            f"{s.openrouter_base_url}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {s.openrouter_api_key}"},
        )
    except httpx.HTTPError as exc:
        raise LLMError(f"連線失敗:{exc}") from exc
    finally:
        if own:
            http.close()

    elapsed = int((time.monotonic() - started) * 1000)

    if r.status_code != 200:
        raise LLMError(f"HTTP {r.status_code}:{r.text[:300]}")

    try:
        body = r.json()
    except ValueError as exc:
        raise LLMError("回應不是 JSON") from exc

    # 坑 1:200 裡面包 error
    if "error" in body:
        raise LLMError(f"上游回報錯誤:{body['error']}")

    choices = body.get("choices") or []
    text = (choices[0].get("message", {}).get("content") or "").strip() if choices else ""
    if not text:
        raise LLMError("拿不到內容 —— 不能把空字串送給客人")

    usage = body.get("usage") or {}
    return LLMResult(text=text, token_count=usage.get("total_tokens"),
                     latency_ms=elapsed)
