"""agent loop:模型呼叫工具 → 程式執行 → 結果放回對話 → 模型再決定下一步。

設計見 docs/superpowers/specs/2026-10-06-booking-agent-design.md 第六節。
- 最多 max_steps 步;最後一步不給工具,不會繞圈到什麼都沒回。但實測 NVIDIA 照樣回工具
  呼叫(2026-10-08,不帶工具、tool_choice none、關思考各 3/3),所以照樣執行、不再問模型;
  同時回的文字丟掉(那是在工具結果出來之前寫的,可能只是客套話、也可能謊稱已訂位),
  回覆由 webhook 補(有成立預約 → 程式確認句,沒有 → fallback)
- 所有步驟共用一個截止時間:每一步拿的是剩下的時間,不是每步各一份
- 轉真人優先:同一個回應裡有 transfer_to_human,其他工具一律不執行
- 這裡不碰資料庫、不送訊息:工具怎麼執行由呼叫端給的 execute 決定
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Callable

from app.agent.handoff import TOOL_NAME as HANDOFF_TOOL
from app.agent.llm import LLMError, LLMResult, ToolCall

logger = logging.getLogger(__name__)

# 剩不到一秒就不再問模型 —— 問了也等不到答案,只會讓客人多等
MIN_STEP_SECONDS = 1.0
TOOL_FAILURE = {"ok": False, "error": "系統暫時無法處理"}


@dataclass(frozen=True)
class AgentResult:
    text: str | None                 # 最後的文字回答;None = 模型失敗、超時或沒回文字
    handoff: ToolCall | None = None  # 模型要轉真人
    booking: dict | None = None      # 這次成立的預約(工具回傳的 booking)
    steps: int = 0
    tools_used: tuple[str, ...] = ()
    token_count: int | None = None
    latency_ms: int = 0


def _calls(result: LLMResult) -> tuple[ToolCall, ...]:
    # 既有的呼叫端(與測試替身)只填 tool_call、沒填 tool_calls
    if result.tool_calls:
        return result.tool_calls
    return (result.tool_call,) if result.tool_call is not None else ()


def run_agent(messages: list[dict], *, tools: list[dict] | None,
              execute: Callable[[str, dict], dict], max_steps: int,
              budget_seconds: float,
              complete_fn: Callable[..., LLMResult]) -> AgentResult:
    deadline = time.monotonic() + budget_seconds
    msgs = list(messages)
    booking: dict | None = None
    used: list[str] = []
    tokens: int | None = None
    latency = 0  # 各步模型回報的 latency 加總(含每一步裡的備援),跟以前記的是同一種數字

    def finish(text: str | None, steps: int, handoff: ToolCall | None = None) -> AgentResult:
        return AgentResult(text=text, handoff=handoff, booking=booking, steps=steps,
                           tools_used=tuple(used), token_count=tokens, latency_ms=latency)

    def run_tool(c: ToolCall) -> dict:
        """執行一個工具:每一步(含最後一步)都走這裡,例外一律變成 TOOL_FAILURE,
        成立的預約記下來。"""
        nonlocal booking
        used.append(c.name)
        try:
            out = execute(c.name, c.arguments)
        except Exception:  # noqa: BLE001
            logger.exception("工具 %s 執行失敗", c.name)
            out = dict(TOOL_FAILURE)
        if out.get("ok") and out.get("booking"):
            booking = out["booking"]
        return out

    for step in range(1, max_steps + 1):
        if deadline - time.monotonic() < MIN_STEP_SECONDS:
            logger.warning("agent 時間用完,停在第 %d 步之前", step)
            return finish(None, step - 1)
        last = step == max_steps
        try:
            result = complete_fn(msgs, tools=None if last else tools, deadline=deadline)
        except LLMError as exc:
            logger.warning("agent 第 %d 步模型失敗:%s", step, exc)
            return finish(None, step)
        except Exception:  # noqa: BLE001
            # 上游回了格式怪異的東西時,解析可能丟出 LLMError 以外的例外。讓它往外飛的話,
            # 前幾步已經成立的預約會被最外層吞掉,客人只收到通用 fallback、不知道訂好了。
            logger.exception("agent 第 %d 步模型呼叫發生非預期錯誤", step)
            return finish(None, step)
        if result.token_count is not None:
            tokens = (tokens or 0) + result.token_count
        latency += result.latency_ms

        calls = _calls(result)
        if not calls:
            return finish(result.text or None, step)
        handoff = next((c for c in calls if c.name == HANDOFF_TOOL), None)
        if handoff is not None:
            return finish(None, step, handoff)

        if last:
            # 最後一步沒給工具,但上游照樣回了工具呼叫(NVIDIA 實測如此):資料齊全的訂位
            # 不能因此丟掉,所以照樣執行。不把結果塞回對話 —— 沒有下一步了,再問一次模型
            # 只會又得到工具呼叫。同時回的文字一律丟掉:那是在工具結果出來之前寫的,
            # 成立時可能只是「好的,幫您處理」(客人看不到確認句),被拒絕時甚至可能寫著
            # 「已為您訂位」。text=None,回覆由呼叫端補(有成立預約 → 確認句,沒有 → fallback)。
            logger.warning("agent 最後一步上游仍回工具呼叫(%s),照樣執行、不再問模型",
                           ",".join(c.name for c in calls))
            for c in calls:
                run_tool(c)
            return finish(None, step)

        msgs.append({"role": "assistant", "content": result.text or "", "tool_calls": [
            {"id": c.id, "type": "function",
             "function": {"name": c.name,
                          "arguments": json.dumps(c.arguments, ensure_ascii=False)}}
            for c in calls]})
        for c in calls:
            out = run_tool(c)
            msgs.append({"role": "tool", "tool_call_id": c.id,
                         "content": json.dumps(out, ensure_ascii=False, default=str)})

    return finish(None, max_steps)  # 走不到:最後一步一定會 return
