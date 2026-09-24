"""呼叫 OpenRouter。

四個踩過的坑都在這裡處理:
  1. OpenRouter 會用 HTTP 200 包著 error 物件回來(provider 暫時滿載)。
     只看 status_code 會誤判成功,拿空字串當答案送給客人。
  2. 推理模型會把英文內部思考當答案唸出來 —— 要 reasoning.exclude。
  3. max_tokens 太小會讓答案被截斷,看起來像模型講到一半斷線。
  4. 免費模型的供應商滿載是常態,不是例外(實測單獨打一次就踩到)。
     偏好模型失敗就換 openrouter/free 自動路由再試一次,不要直接讓客人
     吃到 fallback 訊息。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """呼叫失敗,或回來的東西不能當答案用。"""


@dataclass(frozen=True)
class LLMResult:
    text: str
    token_count: int | None
    latency_ms: int
    # 實際答出來的是哪一個模型。有了退回機制,這就不再是固定值了 ——
    # 而「今天是誰在答」是排查品質忽好忽壞時第一個要知道的事。
    model: str | None = None


def _complete_once(http: httpx.Client, messages: list[dict], model: str,
                   s, deadline: float) -> LLMResult:
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": s.llm_max_tokens,
        "reasoning": {"exclude": True},
    }
    started = time.monotonic()
    try:
        # 用串流邊讀邊看時鐘,不用 http.post 一次讀完:OpenRouter 生成期間
        # 會持續送空白維持連線,http.post 會一直等下去,timeout 也不會觸發。
        with http.stream(
            "POST",
            f"{s.openrouter_base_url}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {s.openrouter_api_key}"},
        ) as r:
            chunks = []
            for chunk in r.iter_bytes():
                if time.monotonic() > deadline:
                    raise LLMError(
                        f"超過整體時間上限 {s.llm_total_budget_seconds:g} 秒,上游還沒送完")
                chunks.append(chunk)
            raw = b"".join(chunks)
    except httpx.HTTPError as exc:
        raise LLMError(f"連線失敗:{exc}") from exc

    elapsed = int((time.monotonic() - started) * 1000)

    if r.status_code != 200:
        raise LLMError(f"HTTP {r.status_code}:{raw.decode(errors='replace')[:300]}")

    try:
        body = json.loads(raw)
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
                     latency_ms=elapsed, model=model)


def complete(messages: list[dict], *, client: httpx.Client | None = None) -> LLMResult:
    """依序試偏好模型、退回模型,第一個答得出來的就用。

    這不是「延後重送」—— 那會讓客人收到延遲很久的孤立訊息。這裡是在
    同一次請求裡換一個上游,客人只會看到一次回覆,只是慢了幾秒。

    超時的情況要知道:兩次各等 llm_timeout_seconds,合起來可能超過 LINE
    reply token 的一分鐘效期。那條路 LineClient 會自動改用 push,客人還是
    收得到,只是會吃掉推播額度。
    """
    s = get_settings()
    models = [s.openrouter_model]
    fallback = getattr(s, "openrouter_fallback_model", None)
    if fallback and fallback != s.openrouter_model:
        models.append(fallback)

    own = client is None
    http = client or httpx.Client(timeout=s.llm_timeout_seconds)
    # 合計的截止時間,不是每個模型各一份 —— 各自一份的話最壞是兩倍時間,
    # 正好越過 reply token 的效期。
    deadline = time.monotonic() + s.llm_total_budget_seconds
    failures: list[str] = []
    try:
        for i, model in enumerate(models):
            if time.monotonic() > deadline:
                failures.append(f"{model} → 沒時間試了")
                break
            try:
                return _complete_once(http, messages, model, s, deadline)
            except LLMError as exc:
                failures.append(f"{model} → {exc}")
                if i + 1 < len(models):
                    logger.warning("模型 %s 失敗,改用 %s 再試一次:%s",
                                   model, models[i + 1], exc)
        raise LLMError("所有模型都失敗:" + " ; ".join(failures))
    finally:
        if own:
            http.close()
