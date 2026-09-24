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
from dataclasses import dataclass, replace

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """呼叫失敗,或回來的東西不能當答案用。"""


@dataclass(frozen=True)
class ToolCall:
    """模型要求執行的工具。

    arguments 解析失敗時是 {},不是例外 —— 模型已經表達「要做這件事」,
    參數寫壞不該讓整次呼叫被當成失敗(那會讓客人掉進 fallback)。
    """
    name: str
    arguments: dict


@dataclass(frozen=True)
class LLMResult:
    text: str
    token_count: int | None
    latency_ms: int
    # 實際答出來的是哪一個模型。有了退回機制,這就不再是固定值了 ——
    # 而「今天是誰在答」是排查品質忽好忽壞時第一個要知道的事。
    model: str | None = None
    # 放在最後,既有的 LLMResult("文字", token_count=..., latency_ms=...) 不受影響
    tool_call: ToolCall | None = None


def _parse_tool_call(message: dict) -> ToolCall | None:
    """只取第一個工具呼叫。目前只提供一個工具,多的沒有意義。"""
    calls = message.get("tool_calls") or []
    if not calls:
        return None
    fn = calls[0].get("function") or {}
    try:
        arguments = json.loads(fn.get("arguments") or "{}")
    except ValueError:
        arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    return ToolCall(name=fn.get("name") or "", arguments=arguments)


def _complete_once(http: httpx.Client, messages: list[dict], model: str,
                   s, deadline: float, tools: list[dict] | None = None) -> LLMResult:
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": s.llm_max_tokens,
        "reasoning": {"exclude": True},
    }
    if tools:
        payload["tools"] = tools
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
    message = (choices[0].get("message") or {}) if choices else {}
    text = (message.get("content") or "").strip()
    tool_call = _parse_tool_call(message)
    # 呼叫工具時 content 本來就常常是 null —— 兩個都沒有才算失敗
    if not text and tool_call is None:
        raise LLMError("拿不到內容 —— 不能把空字串送給客人")

    usage = body.get("usage") or {}
    return LLMResult(text=text, token_count=usage.get("total_tokens"),
                     latency_ms=elapsed, model=model, tool_call=tool_call)


def complete(messages: list[dict], *, tools: list[dict] | None = None,
             client: httpx.Client | None = None) -> LLMResult:
    """依序試偏好模型、退回模型,第一個答得出來的就用。

    這不是「延後重送」—— 那會讓客人收到延遲很久的孤立訊息。這裡是在
    同一次請求裡換一個上游,客人只會看到一次回覆,只是慢了幾秒。

    兩個模型合計受 llm_total_budget_seconds 限制,不會越過 LINE reply token
    的一分鐘效期。回傳的 latency_ms 也是合計的 —— 客人實際等了多久。
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
    started = time.monotonic()
    deadline = started + s.llm_total_budget_seconds
    failures: list[str] = []
    try:
        for i, model in enumerate(models):
            if time.monotonic() > deadline:
                failures.append(f"{model} → 沒時間試了")
                break
            try:
                result = _complete_once(http, messages, model, s, deadline, tools)
                # 記客人實際等了多久,不是最後那個模型花了多久 —— 只記最後
                # 一個的話,退回越常發生紀錄越偏低。
                return replace(result, latency_ms=int((time.monotonic() - started) * 1000))
            except LLMError as exc:
                failures.append(f"{model} → {exc}")
                if i + 1 < len(models):
                    logger.warning("模型 %s 失敗,改用 %s 再試一次:%s",
                                   model, models[i + 1], exc)
        raise LLMError("所有模型都失敗:" + " ; ".join(failures))
    finally:
        if own:
            http.close()
