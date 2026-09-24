# 轉真人 agent 化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 模型帶著 `transfer_to_human` 工具回答、由它自己決定要不要轉真人;關鍵字規則保底;轉了之後 AI 停止回答、推播通知店員、逾時自動交還。

**Architecture:** 新模組 `app/agent/handoff.py` 放所有「要不要轉、轉了怎麼改狀態」的純函式,規則層與 agent 層共用。`complete()` 多一個可選的 `tools` 參數,回傳多一個 `tool_call`。`webhook.process_text_event` 在去重之後分三條路:HUMAN 中不回 → 關鍵字命中直接轉 → 帶工具問模型。

**Tech Stack:** FastAPI · SQLAlchemy 2 · Alembic · httpx · OpenRouter tool calling · LINE Messaging API(reply / push)· pytest

**Spec:** [docs/superpowers/specs/2026-09-24-agent-handoff-design.md](../specs/2026-09-24-agent-handoff-design.md)

**Branch:** `feat/agent-handoff`(已建立,spec 已 commit 在上面)

## Global Constraints

每一個 task 的要求都隱含包含這一節。

1. **指令一律用 PowerShell 語法**,Python 一律用 `.\.venv\Scripts\python.exe`。
2. **測試不碰網路。** LINE 與 OpenRouter 一律 monkeypatch 或 `httpx.MockTransport`。
3. **時間一律 UTC,而且 SQLite 讀回來的 datetime 沒有時區**(實測 `tzinfo = None`)。拿資料庫讀出來的時間跟 `datetime.now(timezone.utc)` 比之前,一律先過 `handoff.as_utc()`,否則 `TypeError`。只在 PostgreSQL 上測會漏掉這一條。
4. **模糊的時候往「轉真人」那邊倒。** 模型呼叫了工具但參數壞掉、類別不認得,照樣轉(回通用句)。
5. **先回客人、再推播店員。** reply token 約一分鐘有效,推播沒有時限。
6. **推播失敗照樣維持 HUMAN**(spec 決策 6),只寫 error log。
7. **`tools` 不傳時 `complete()` 行為完全不變** —— 既有測試不准因此改動。
8. **沉默是唯一不被接受的失敗模式**(既有原則)。但 HUMAN 模式中「AI 不回」是刻意的,不算沉默 —— 真人在後台回。
9. **每個 task 結束時全套測試要綠**:`.\.venv\Scripts\python.exe -m pytest -q`。

---

### Task 1: `complete()` 支援工具呼叫

**Files:**
- Modify: `app/agent/llm.py`
- Test: `tests/test_agent.py`(檔尾追加)

**Interfaces:**
- Consumes: 無
- Produces:
  - `app.agent.llm.ToolCall(name: str, arguments: dict)` —— frozen dataclass;`arguments` 解析失敗時是 `{}`
  - `LLMResult` 新增欄位 `tool_call: ToolCall | None = None`(放最後,既有的位置參數呼叫不受影響)
  - `complete(messages, *, tools: list[dict] | None = None, client=None) -> LLMResult`

- [ ] **Step 1: 寫失敗的測試**

`tests/test_agent.py` 檔尾追加(`_json`、`_http`、`_ok` 都是檔案裡既有的):

```python
# --- 工具呼叫 ----------------------------------------------------------------

from app.agent.llm import ToolCall  # noqa: E402

TOOL = {"type": "function", "function": {
    "name": "transfer_to_human", "description": "轉給真人",
    "parameters": {"type": "object",
                   "properties": {"category": {"type": "string", "enum": ["safety"]}},
                   "required": ["category"]}}}


def _tool_response(arguments='{"category": "safety", "reason": "起紅疹"}', content=None):
    """OpenAI 相容格式的工具呼叫。arguments 是「JSON 字串」不是物件 ——
    這是規格,也是模型最常寫壞的地方。"""
    return httpx.Response(200, json={
        "choices": [{"message": {
            "content": content,
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "transfer_to_human",
                                         "arguments": arguments}}]}}],
        "usage": {"total_tokens": 30}})


def test_a_tool_call_with_empty_content_is_a_success():
    """呼叫工具時 content 本來就常常是 null。沿用「沒有文字就是失敗」的判斷,
    每一次轉真人都會被當成模型壞掉、改送 fallback。"""
    r = complete([{"role": "user", "content": "我女兒吃完全身起紅疹"}],
                 tools=[TOOL], client=_http(lambda req: _tool_response()))
    assert r.tool_call == ToolCall("transfer_to_human",
                                   {"category": "safety", "reason": "起紅疹"})
    assert r.text == ""


def test_tools_are_sent_upstream_only_when_given():
    seen = []

    def handler(request):
        seen.append(_json.loads(request.content))
        return _ok()

    complete([{"role": "user", "content": "嗨"}], client=_http(handler))
    complete([{"role": "user", "content": "嗨"}], tools=[TOOL], client=_http(handler))
    assert "tools" not in seen[0]
    assert seen[1]["tools"] == [TOOL]


def test_text_and_a_tool_call_are_both_returned():
    """同時回了文字與工具呼叫時兩個都要交出去,由呼叫端決定 ——
    在這裡丟掉任何一個,webhook 就沒辦法實作「以工具為準」。"""
    r = complete([{"role": "user", "content": "嗨"}], tools=[TOOL],
                 client=_http(lambda req: _tool_response(content="我幫您轉給專人")))
    assert r.text == "我幫您轉給專人"
    assert r.tool_call is not None


def test_broken_tool_arguments_still_produce_a_tool_call():
    """模型已經表達「要轉」了。參數 JSON 寫壞不該讓整次呼叫失敗 ——
    那會讓客人的緊急狀況掉進 fallback。"""
    r = complete([{"role": "user", "content": "嗨"}], tools=[TOOL],
                 client=_http(lambda req: _tool_response(arguments="{category: safety")))
    assert r.tool_call == ToolCall("transfer_to_human", {})


def test_no_text_and_no_tool_call_is_still_a_failure():
    empty = lambda req: httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})
    with pytest.raises(LLMError):
        complete([{"role": "user", "content": "嗨"}], tools=[TOOL], client=_http(empty))
```

- [ ] **Step 2: 跑測試確認失敗**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_agent.py -q
```

Expected: collection error —— `ImportError: cannot import name 'ToolCall' from 'app.agent.llm'`

- [ ] **Step 3: 實作**

`app/agent/llm.py`,把 `LLMResult` 那段換成:

```python
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
```

`_complete_once` 的簽名與 payload 改成:

```python
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
```

`_complete_once` 結尾從 `choices = body.get("choices") or []` 到 `return` 換成:

```python
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
```

`complete` 的簽名與呼叫改成:

```python
def complete(messages: list[dict], *, tools: list[dict] | None = None,
             client: httpx.Client | None = None) -> LLMResult:
```

```python
                result = _complete_once(http, messages, model, s, deadline, tools)
```

- [ ] **Step 4: 跑測試確認通過**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_agent.py -q
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: `test_agent.py` 全綠(21 passed);全套 171 passed, 1 skipped。

- [ ] **Step 5: Commit**

```powershell
git add app/agent/llm.py tests/test_agent.py
git commit -m "feat(llm): complete() 支援工具呼叫

呼叫工具時 content 本來就常常是 null,沿用「沒有文字就是失敗」
的判斷會把每一次轉真人都當成模型壞掉、改送 fallback —— 放寬成
「沒有文字、也沒有工具呼叫」才算失敗。

參數 JSON 寫壞時回 {} 而不是丟例外:模型已經表達「要做這件事」,
參數寫壞不該讓客人掉進 fallback。

tools 不傳時行為完全不變。

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `app/agent/handoff.py` —— 要不要轉、轉了怎麼改狀態

**Files:**
- Create: `app/agent/handoff.py`
- Test: `tests/test_handoff.py`(新建)

**Interfaces:**
- Consumes: `app.models.ConversationMode`、`app.models.User`
- Produces(Task 4、6 都靠這些名字):
  - 常數 `TOOL_NAME = "transfer_to_human"`、`GENERIC_SCRIPT`、`EXPIRED_PREFIX`
  - `Decision(category: str | None, script: str, basis: str)` —— frozen dataclass
  - `match_keyword(rules: list[dict], text: str) -> Decision | None`
  - `build_tool(rules: list[dict]) -> dict | None`
  - `decision_from_tool(rules: list[dict], arguments: dict) -> Decision`
  - `as_utc(value: datetime | None) -> datetime | None`
  - `is_still_human(user: User, now: datetime) -> bool`
  - `enter_human_mode(user: User, *, timeout_minutes: int, now: datetime) -> None`
  - `return_to_ai(user: User, now: datetime) -> None`
  - `customer_label(user: User) -> str`
  - `staff_message(decision: Decision, customer: str, text: str) -> str`

這個模組不開 session、不送訊息,只做決定與改 `User` 物件 —— 所以測試不需要資料庫。

- [ ] **Step 1: 寫失敗的測試**

新建 `tests/test_handoff.py`:

```python
"""轉真人判斷的測試。

規則層是安全攸關的保底,它壞掉的方式通常不是報錯,而是「該轉沒轉」
或「什麼都轉」—— 兩種都不會有錯誤訊息,所以每一種都要有測試守著。
"""

from datetime import datetime, timedelta, timezone

from app.agent.handoff import (
    EXPIRED_PREFIX, GENERIC_SCRIPT, TOOL_NAME, Decision, as_utc, build_tool,
    customer_label, decision_from_tool, enter_human_mode, is_still_human,
    match_keyword, return_to_ai, staff_message,
)
from app.models import ConversationMode, User

RULES = [
    {"level": "L3", "category": "safety", "trigger": "過敏、食物中毒、送醫",
     "action": "transfer", "script": "這件事我立刻請店長與您聯繫。"},
    {"level": "L3", "category": "legal", "trigger": "提告、律師",
     "action": "transfer", "script": "這部分我請主管直接與您說明。"},
    {"level": "L1", "category": "service", "trigger": "上菜太慢、態度不佳",
     "action": "apologize", "script": "很抱歉讓您有這樣的感受。"},
]

NOW = datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)


# --- 規則層 -------------------------------------------------------------------

def test_a_trigger_word_anywhere_in_the_message_is_a_hit():
    d = match_keyword(RULES, "我朋友吃完過敏送醫了")
    assert d == Decision("safety", "這件事我立刻請店長與您聯繫。", "關鍵字「過敏」")


def test_apologize_rules_never_transfer():
    """L1 不需要轉真人,道歉本來就是模型做得好的事。"""
    assert match_keyword(RULES, "你們上菜太慢了") is None


def test_no_trigger_word_is_no_hit():
    assert match_keyword(RULES, "有停車位嗎") is None


def test_the_first_matching_rule_wins():
    """yaml 裡 L3 safety 排在最前面 —— 同時命中時安全優先。"""
    assert match_keyword(RULES, "我要提告,我朋友過敏送醫").category == "safety"


def test_an_empty_word_from_a_stray_separator_matches_nothing():
    """trigger 寫成「過敏、」時切出來會有空字串,而 "" in 任何字串都是 True ——
    不擋的話,每一則訊息都會被轉真人。"""
    rules = [{"category": "safety", "trigger": "過敏、", "action": "transfer",
              "script": "s"}]
    assert match_keyword(rules, "有停車位嗎") is None


# --- 工具定義 -----------------------------------------------------------------

def test_the_tool_only_offers_transfer_categories():
    tool = build_tool(RULES)
    assert tool["function"]["name"] == TOOL_NAME
    enum = tool["function"]["parameters"]["properties"]["category"]["enum"]
    assert enum == ["safety", "legal"]


def test_the_tool_description_carries_the_trigger_words():
    """模型要靠描述知道每一類大概是什麼情況,才判斷得出換句話說的版本。"""
    assert "過敏" in build_tool(RULES)["function"]["description"]


def test_no_transfer_rules_means_no_tool():
    assert build_tool([RULES[2]]) is None
    assert build_tool([]) is None


def test_a_known_category_from_the_model_uses_that_script():
    d = decision_from_tool(RULES, {"category": "legal", "reason": "客人說要找律師"})
    assert d == Decision("legal", "這部分我請主管直接與您說明。", "AI —— 客人說要找律師")


def test_an_unknown_category_still_transfers_with_the_generic_script():
    """模型已經表達「需要真人」,類別寫錯不該讓客人被漏掉。"""
    d = decision_from_tool(RULES, {"category": "weather"})
    assert d.category is None
    assert d.script == GENERIC_SCRIPT


def test_empty_arguments_still_transfer():
    """Task 1 在參數 JSON 壞掉時給的就是 {}。"""
    assert decision_from_tool(RULES, {}).script == GENERIC_SCRIPT


# --- 模式與時間 ---------------------------------------------------------------

def test_naive_datetimes_from_sqlite_are_marked_utc():
    assert as_utc(datetime(2026, 9, 24, 8, 0)) == NOW
    assert as_utc(None) is None


def test_ai_mode_is_not_human():
    assert not is_still_human(User(mode=ConversationMode.AI), NOW)


def test_human_mode_before_the_deadline_is_still_human():
    u = User(mode=ConversationMode.HUMAN, mode_expires_at=NOW + timedelta(minutes=1))
    assert is_still_human(u, NOW)


def test_human_mode_after_the_deadline_is_over():
    u = User(mode=ConversationMode.HUMAN, mode_expires_at=NOW - timedelta(minutes=1))
    assert not is_still_human(u, NOW)


def test_a_naive_deadline_read_back_from_sqlite_does_not_crash():
    """SQLite 讀回來 tzinfo=None,直接跟有時區的 now 比會 TypeError ——
    而那個例外會被 webhook 最外層接走,客人收到的是 fallback。"""
    u = User(mode=ConversationMode.HUMAN, mode_expires_at=datetime(2026, 9, 24, 7, 59))
    assert not is_still_human(u, NOW)


def test_entering_human_mode_sets_the_deadline():
    u = User(mode=ConversationMode.AI)
    enter_human_mode(u, timeout_minutes=30, now=NOW)
    assert u.mode == ConversationMode.HUMAN
    assert u.mode_changed_at == NOW
    assert u.mode_expires_at == NOW + timedelta(minutes=30)


def test_returning_to_ai_clears_the_deadline():
    u = User(mode=ConversationMode.HUMAN, mode_expires_at=NOW)
    return_to_ai(u, NOW)
    assert u.mode == ConversationMode.AI and u.mode_expires_at is None


# --- 給店員的通知 -------------------------------------------------------------

def test_customer_label_prefers_the_display_name():
    assert customer_label(User(line_user_id="U1234567890", display_name="王小明")) == "王小明"
    assert customer_label(User(line_user_id="U1234567890")) == "客人 …567890"


def test_staff_message_says_what_happened_and_why():
    d = Decision("safety", "s", "關鍵字「過敏」")
    msg = staff_message(d, "王小明", "我朋友吃完過敏送醫了")
    assert msg.splitlines() == ["【安全】王小明", "我朋友吃完過敏送醫了",
                                "判斷依據:關鍵字「過敏」"]


def test_staff_message_truncates_long_messages():
    msg = staff_message(Decision("safety", "s", "b"), "王小明", "過" * 300)
    assert "過" * 100 in msg and "過" * 101 not in msg


def test_staff_message_for_an_unknown_category():
    msg = staff_message(Decision(None, GENERIC_SCRIPT, "AI —— x"), "王小明", "嗨")
    assert msg.startswith("【轉真人】")


def test_the_expired_prefix_is_a_complete_sentence():
    """它會直接接在模型答案前面,少了句號兩句話會黏在一起。"""
    assert EXPIRED_PREFIX.endswith("。")
```

- [ ] **Step 2: 跑測試確認失敗**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_handoff.py -q
```

Expected: `ModuleNotFoundError: No module named 'app.agent.handoff'`

- [ ] **Step 3: 實作**

新建 `app/agent/handoff.py`:

```python
"""轉真人:規則層與 agent 層共用的判斷與狀態變更。

兩層走同一套函式,行為才保證一致 —— 不會出現「關鍵字轉的會通知、
模型轉的不會」。這個模組不開 session、不送訊息,只做決定與改 User
物件;送出去是 webhook 的事。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.models import ConversationMode, User

TOOL_NAME = "transfer_to_human"
# 模型呼叫了工具、類別卻不認得時回的話。不能沿用任何一類的 script ——
# 猜錯類別的話,客人會聽到跟他的狀況無關的承諾。
GENERIC_SCRIPT = "這部分我請專人與您聯繫,請稍候。"
EXPIRED_PREFIX = "專員目前不在線上,我先幫您處理。"
CATEGORY_LABELS = {"safety": "安全", "legal": "法律", "money": "帳務", "privacy": "個資"}


@dataclass(frozen=True)
class Decision:
    category: str | None   # None:模型給了不認得的類別
    script: str            # 回給客人的話
    basis: str             # 給店員看的判斷依據 —— 規則轉的還是模型轉的


def _transfer_rules(rules: list[dict]) -> list[dict]:
    return [r for r in rules if r.get("action") == "transfer"]


def _words(trigger: str) -> list[str]:
    # 空字串要濾掉:trigger 寫成「過敏、」時會切出 "",而 "" in 任何字串
    # 都是 True —— 每一則訊息都會被轉真人。
    return [w.strip() for w in (trigger or "").split("、") if w.strip()]


def match_keyword(rules: list[dict], text: str) -> Decision | None:
    """第一條命中的 transfer 規則。yaml 裡 L3 排在前面,所以安全優先。"""
    for rule in _transfer_rules(rules):
        for word in _words(rule.get("trigger", "")):
            if word in text:
                return Decision(rule.get("category"), rule["script"], f"關鍵字「{word}」")
    return None


def build_tool(rules: list[dict]) -> dict | None:
    """從升級規則產生工具定義。換行業時自動跟著變。"""
    transfer = _transfer_rules(rules)
    if not transfer:
        return None
    hints = ";".join(f"{r['category']}({r.get('trigger', '')})" for r in transfer)
    return {"type": "function", "function": {
        "name": TOOL_NAME,
        "description": ("客人的狀況需要真人處理時呼叫,不要自己回答。"
                        f"類別:{hints}。意思相近、換句話說的也算。"),
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string",
                             "enum": [r["category"] for r in transfer]},
                "reason": {"type": "string", "description": "一句話說明為什麼要轉"},
            },
            "required": ["category"],
        },
    }}


def decision_from_tool(rules: list[dict], arguments: dict) -> Decision:
    """模型呼叫了工具。類別不認得也照樣轉 —— 模型已經表達「需要真人」。"""
    reason = str(arguments.get("reason") or "").strip() or "(模型沒有說明)"
    category = arguments.get("category")
    for rule in _transfer_rules(rules):
        if rule.get("category") == category:
            return Decision(category, rule["script"], f"AI —— {reason}")
    return Decision(None, GENERIC_SCRIPT, f"AI —— {reason}")


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite 讀回來的 datetime 沒有時區(實測 tzinfo=None),直接跟
    datetime.now(timezone.utc) 比會 TypeError。PostgreSQL 讀回來有時區,
    所以只在 PG 上測會漏掉這一條。"""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def is_still_human(user: User, now: datetime) -> bool:
    if user.mode != ConversationMode.HUMAN:
        return False
    expires = as_utc(user.mode_expires_at)
    # NULL = 不會自動切換(schema 決策 4)。這裡的 handoff 一定會設期限,
    # NULL 只會出現在有人手動改資料庫的時候。
    return expires is None or now < expires


def enter_human_mode(user: User, *, timeout_minutes: int, now: datetime) -> None:
    user.mode = ConversationMode.HUMAN
    user.mode_changed_at = now
    user.mode_expires_at = now + timedelta(minutes=timeout_minutes)


def return_to_ai(user: User, now: datetime) -> None:
    user.mode = ConversationMode.AI
    user.mode_changed_at = now
    user.mode_expires_at = None


def customer_label(user: User) -> str:
    return user.display_name or f"客人 …{user.line_user_id[-6:]}"


def staff_message(decision: Decision, customer: str, text: str) -> str:
    label = CATEGORY_LABELS.get(decision.category or "", "轉真人")
    return f"【{label}】{customer}\n{text[:100]}\n判斷依據:{decision.basis}"
```

- [ ] **Step 4: 跑測試確認通過**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_handoff.py -q
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: `test_handoff.py` 23 passed;全套 194 passed, 1 skipped。

- [ ] **Step 5: Commit**

```powershell
git add app/agent/handoff.py tests/test_handoff.py
git commit -m "feat(agent): handoff 模組 —— 要不要轉、轉了怎麼改狀態

規則層與 agent 層共用同一套函式,行為才保證一致:不會出現
「關鍵字轉的會通知、模型轉的不會」。

兩個不會報錯的坑各有一條測試守著:
- trigger 尾端多一個頓號會切出空字串,而 \"\" in 任何字串都是 True,
  每一則訊息都會被轉真人
- SQLite 讀回來的 datetime 沒有時區,跟有時區的 now 比會 TypeError,
  而那個例外會被 webhook 最外層接走,客人收到 fallback

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Alembic 遷移 —— `companies` 加兩欄

**Files:**
- Modify: `app/models/company.py`
- Create: `alembic/versions/3f2a9c1d7b4e_escalation_rules_and_staff_notify.py`
- Test: `tests/test_alembic.py`(新建)

**Interfaces:**
- Consumes: 無
- Produces: `Company.escalation_rules: list`(NOT NULL,預設 `[]`)、`Company.staff_notify_to: str | None`

既有測試全部用 `Base.metadata.create_all` 建表 —— 那驗的是 model,不是遷移檔。這個 task 補第一個真的跑 `alembic upgrade` 的測試。

- [ ] **Step 1: 寫失敗的測試**

新建 `tests/test_alembic.py`:

```python
"""Alembic 遷移的測試。

其他測試都用 Base.metadata.create_all 建表 —— 驗的是 model,不是遷移檔。
遷移檔寫錯(例如 NOT NULL 欄位忘了 server_default)只會在對著「已經有
資料」的資料庫跑 upgrade 時才爆,也就是部署當下。

只跑 SQLite:對 CI 的 PostgreSQL 測試庫跑 upgrade 會跟其他測試的
create_all / drop_all 互相踩。PostgreSQL 的遷移由部署時 app 容器開機
的 `alembic upgrade head` 驗(計畫 Task 7 Step 4),而那時資料庫裡有真的資料。
"""

import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
INITIAL = "66766e088730"


@pytest.fixture
def alembic_sqlite(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'migrate.db'}"
    # alembic/env.py 讀的是 get_settings().database_url,而它有 lru_cache
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    # 刻意不給 ini 檔路徑:有檔名的話 env.py 會跑 fileConfig(),預設
    # disable_existing_loggers=True,之後的測試抓 log 會莫名其妙抓不到。
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    yield cfg, url
    get_settings.cache_clear()


def _columns(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns("companies")}
    finally:
        engine.dispose()


def test_upgrade_keeps_existing_companies_and_defaults_the_rules_to_empty(alembic_sqlite):
    """companies 已經有資料列時,NOT NULL 欄位沒有 server_default 的話
    ALTER TABLE 會直接失敗 —— 部署時就是這個情況。"""
    cfg, url = alembic_sqlite
    command.upgrade(cfg, INITIAL)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO companies (id, slug, name, industry, line_channel_secret_enc,"
            " line_channel_token_enc, system_prompt, tone, forbidden_phrases,"
            " fallback_message, human_mode_timeout_minutes, vector_collection, is_active)"
            " VALUES ('c1', 'bistro', '微醺', 'restaurant', x'00', x'00', 'p', 't', '[]',"
            " 'f', 30, 'kb_bistro', 1)"))
    engine.dispose()

    command.upgrade(cfg, "head")

    assert {"escalation_rules", "staff_notify_to"} <= _columns(url)
    engine = create_engine(url)
    with engine.connect() as conn:
        rules, staff = conn.execute(text(
            "SELECT escalation_rules, staff_notify_to FROM companies")).one()
    engine.dispose()
    assert json.loads(rules) == []
    assert staff is None


def test_downgrade_removes_both_columns(alembic_sqlite):
    cfg, url = alembic_sqlite
    command.upgrade(cfg, "head")
    command.downgrade(cfg, INITIAL)
    assert not {"escalation_rules", "staff_notify_to"} & _columns(url)
```

- [ ] **Step 2: 跑測試確認失敗**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_alembic.py -q
```

Expected: 兩個都 FAIL —— `assert {'escalation_rules', 'staff_notify_to'} <= {...}`(head 還是初始版本,欄位不存在)。

- [ ] **Step 3: model 加欄位**

`app/models/company.py`,在 `human_mode_timeout_minutes` 那一行之後加:

```python
    # 升級規則(escalation.yaml 整份)。seed 時寫入,執行時規則層與工具定義
    # 都從這裡讀,不讀 yaml 檔 —— system_prompt 也是 seed 時組好的,兩者要
    # 來自同一次 seed。改了 yaml 卻沒重跑 seed 時,若規則在執行時讀檔,
    # prompt 與規則就會講不同的話,而且不會有任何錯誤訊息。
    escalation_rules: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # 轉真人時推播通知的對象(店員的 userId 或群組 ID)。NULL = 不推播,
    # 只寫 warning log —— 沒設定的店家也要能正常運作。
    staff_notify_to: Mapped[str | None] = mapped_column(String(64))
```

- [ ] **Step 4: 寫遷移檔**

新建 `alembic/versions/3f2a9c1d7b4e_escalation_rules_and_staff_notify.py`:

```python
"""escalation_rules and staff_notify_to

Revision ID: 3f2a9c1d7b4e
Revises: 66766e088730
Create Date: 2026-09-24 16:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '3f2a9c1d7b4e'
down_revision: Union[str, Sequence[str], None] = '66766e088730'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default 是必要的:companies 已經有資料列,NOT NULL 欄位沒有
    # 預設值的話 PostgreSQL 直接拒絕 ALTER TABLE。'[]' = 沒有規則,
    # 規則層與工具都不啟用,行為跟遷移前一樣 —— 重跑一次 seed 才會打開。
    #
    # batch_alter_table:SQLite 的 ALTER TABLE 不支援 DROP COLUMN,
    # downgrade 要靠 batch 模式重建表。
    with op.batch_alter_table("companies") as batch:
        batch.add_column(sa.Column("escalation_rules", sa.JSON(), nullable=False,
                                   server_default=sa.text("'[]'")))
        batch.add_column(sa.Column("staff_notify_to", sa.String(length=64),
                                   nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("companies") as batch:
        batch.drop_column("staff_notify_to")
        batch.drop_column("escalation_rules")
```

- [ ] **Step 5: 跑測試確認通過**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_alembic.py -q
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: `test_alembic.py` 2 passed;全套 196 passed, 1 skipped。

- [ ] **Step 6: Commit**

```powershell
git add app/models/company.py alembic/versions/3f2a9c1d7b4e_escalation_rules_and_staff_notify.py tests/test_alembic.py
git commit -m "feat(db): companies 加 escalation_rules 與 staff_notify_to

escalation_rules 存進資料庫而不是執行時讀 yaml:system_prompt 是 seed
時組好的,規則也要來自同一次 seed,否則改了 yaml 沒重跑 seed 時兩邊會
講不同的話,而且不會報錯。

server_default '[]' 是必要的 —— companies 已經有資料列,NOT NULL 欄位
沒有預設值的話 PostgreSQL 直接拒絕 ALTER TABLE。

補上第一個真的跑 alembic upgrade 的測試。其他測試都用 create_all,
驗的是 model 不是遷移檔,遷移檔寫錯只會在部署當下爆。

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: seed 寫入規則與通知對象、`release` 指令

**Files:**
- Modify: `app/cli.py`
- Test: `tests/test_cli.py`(檔尾追加)

**Interfaces:**
- Consumes: Task 2 的 `return_to_ai(user, now)`;Task 3 的兩個欄位
- Produces:
  - `run_seed(..., staff_notify_to: str | None = None)` —— 新增 keyword 參數;每次都覆寫 `escalation_rules`;`reset_history=True` 時同時把這家公司所有客人交還 AI
  - `run_release(slug: str) -> int` —— 回傳交還了幾位
  - CLI:`seed --staff-notify-to`、`release --slug`

- [ ] **Step 1: 寫失敗的測試**

`tests/test_cli.py` 檔尾追加(`SECRET`、`TOKEN`、`_add_history` 是檔案裡既有的):

```python
# --- 轉真人:規則、通知對象、交還 --------------------------------------------

from app.cli import run_release  # noqa: E402
from app.models import ConversationMode  # noqa: E402


def _human_user(company_id: str, line_user_id: str) -> None:
    from datetime import datetime, timedelta, timezone
    with session_scope() as db:
        db.add(User(company_id=company_id, line_user_id=line_user_id,
                    mode=ConversationMode.HUMAN,
                    mode_expires_at=datetime.now(timezone.utc) + timedelta(minutes=30)))


def _modes(company_id: str) -> list[str]:
    with session_scope() as db:
        return [u.mode.value for u in db.query(User).filter(User.company_id == company_id)]


def test_seed_stores_the_escalation_rules():
    cid = run_seed("restaurant", slug="bistro", channel_secret=SECRET, channel_token=TOKEN)
    with session_scope() as db:
        rules = db.get(Company, cid).escalation_rules
    assert {"safety", "legal", "money", "privacy"} <= {r["category"] for r in rules}
    assert all(r["trigger"] and r["script"] and r["action"] for r in rules)


def test_switching_industry_rewrites_the_escalation_rules():
    """規則沒跟著換的話,診所的客人說「過敏」會被回餐廳的「請店長聯繫」。"""
    cid = run_seed("restaurant", slug="bistro", channel_secret=SECRET, channel_token=TOKEN)
    run_seed("clinic", slug="bistro")
    with session_scope() as db:
        safety = next(r for r in db.get(Company, cid).escalation_rules
                      if r["category"] == "safety")
    assert "醫師" in safety["script"]


def test_seed_stores_and_strips_staff_notify_to():
    cid = run_seed("restaurant", slug="bistro", channel_secret=SECRET,
                   channel_token=TOKEN, staff_notify_to="  Ustaff0001\n")
    with session_scope() as db:
        assert db.get(Company, cid).staff_notify_to == "Ustaff0001"


def test_seed_without_staff_notify_to_keeps_the_existing_value():
    """沒給是「不動」,不是「清空」—— 換行業時不該逼人再找一次店員 userId。"""
    cid = run_seed("restaurant", slug="bistro", channel_secret=SECRET,
                   channel_token=TOKEN, staff_notify_to="Ustaff0001")
    run_seed("clinic", slug="bistro")
    with session_scope() as db:
        assert db.get(Company, cid).staff_notify_to == "Ustaff0001"


def test_reset_history_returns_only_that_companys_customers_to_ai():
    """舊對話都清了,還卡在 HUMAN 沒有意義;但別家公司的客人不能被動到。"""
    a = run_seed("restaurant", slug="aa", channel_secret=SECRET, channel_token=TOKEN)
    b = run_seed("restaurant", slug="bb", channel_secret=SECRET, channel_token=TOKEN)
    _human_user(a, "Ua")
    _human_user(b, "Ub")
    run_seed("clinic", slug="aa", reset_history=True)
    assert _modes(a) == ["AI"]
    assert _modes(b) == ["HUMAN"]


def test_release_returns_every_human_customer_to_ai():
    cid = run_seed("restaurant", slug="bistro", channel_secret=SECRET, channel_token=TOKEN)
    _human_user(cid, "U1")
    _human_user(cid, "U2")
    assert run_release("bistro") == 2
    assert _modes(cid) == ["AI", "AI"]
    with session_scope() as db:
        assert all(u.mode_expires_at is None for u in db.query(User))


def test_release_unknown_slug_is_an_error():
    with pytest.raises(SystemExit) as exc:
        run_release("nope")
    assert "nope" in str(exc.value)


def test_main_release_prints_the_count(capsys):
    cid = run_seed("restaurant", slug="bistro", channel_secret=SECRET, channel_token=TOKEN)
    _human_user(cid, "U1")
    assert main(["release", "--slug", "bistro"]) == 0
    assert "1 位" in capsys.readouterr().out
```

- [ ] **Step 2: 跑測試確認失敗**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_cli.py -q
```

Expected: collection error —— `ImportError: cannot import name 'run_release' from 'app.cli'`

- [ ] **Step 3: 實作 `run_seed` 的改動**

`app/cli.py` 的 `run_seed` 簽名改成:

```python
def run_seed(industry: str, *, slug: str, channel_secret: str | None = None,
             channel_token: str | None = None, destination: str | None = None,
             reset_history: bool = False, staff_notify_to: str | None = None) -> str:
```

函式內的 import 區改成:

```python
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.agent.handoff import return_to_ai
    from app.crypto import encrypt
    from app.database import session_scope
    from app.models import ChatHistory, Company, User
```

在 `company.system_prompt = render_system_prompt(data)` 之後加:

```python
        # 跟 system_prompt 同一次 seed 寫入 —— 兩者要講同一套規則
        company.escalation_rules = list(data.get("escalation") or [])
```

在 `if destination:` 那個區塊之後加:

```python
        if staff_notify_to:
            company.staff_notify_to = staff_notify_to.strip()
```

在 `print(f"  已清掉 {removed} 則舊對話(--reset-history)")` 之後(仍在 `if reset_history:` 裡)加:

```python
            # 舊對話都清了,還卡在 HUMAN 沒有意義 —— 同樣只限這一家
            now = datetime.now(timezone.utc)
            users = db.scalars(select(User).where(User.company_id == company_id)).all()
            for user in users:
                return_to_ai(user, now)
```

- [ ] **Step 4: 實作 `run_release` 與 CLI**

在 `run_set_webhook` 之後、`_ensure_utf8_stdout` 之前加:

```python
def run_release(slug: str) -> int:
    """把這家公司所有 HUMAN 模式的客人立刻交還給 AI。

    示範時要用:轉真人之後客人會卡在 HUMAN 模式 30 分鐘,不交還的話
    下一段示範得換一支手機。
    """
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.agent.handoff import return_to_ai
    from app.database import session_scope
    from app.models import Company, ConversationMode, User

    with session_scope() as db:
        company = db.scalar(select(Company).where(Company.slug == slug))
        if company is None:
            raise SystemExit(
                f"找不到 slug「{slug}」。用 `python -m app.cli list` 看有哪些行業。")
        users = db.scalars(select(User).where(
            User.company_id == company.id,
            User.mode == ConversationMode.HUMAN)).all()
        now = datetime.now(timezone.utc)
        for user in users:
            return_to_ai(user, now)
        return len(users)
```

`main()` 裡,`s.add_argument("--reset-history", ...)` 之後加:

```python
    s.add_argument("--staff-notify-to",
                   help="轉真人時推播通知的對象(店員 userId)。沒給就保留原值。")

    r = sub.add_parser("release")
    r.add_argument("--slug", required=True)
```

`if args.cmd == "set-webhook":` 區塊之後加:

```python
    if args.cmd == "release":
        n = run_release(args.slug)
        print(f"✓ 已把 {n} 位客人交還給 AI")
        return 0
```

最後的 `run_seed(...)` 呼叫加上 `staff_notify_to=args.staff_notify_to`。

- [ ] **Step 5: 跑測試確認通過**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_cli.py -q
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: 全套 204 passed, 1 skipped。

- [ ] **Step 6: Commit**

```powershell
git add app/cli.py tests/test_cli.py
git commit -m "feat(cli): seed 寫入升級規則與店員通知對象,新增 release

--staff-notify-to 沒給是「不動」不是「清空」,跟 --destination 同一套
規矩 —— 換行業時不該逼人再找一次店員 userId。

--reset-history 同時把這家的客人交還 AI:舊對話都清了還卡在 HUMAN
沒有意義。release 給示範用,不然轉真人之後要等 30 分鐘或換一支手機。

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: prompt 樣板 —— transfer 類改成「呼叫工具」

**Files:**
- Modify: `prompts/system.j2`
- Modify: `tests/test_render.py`

**Interfaces:**
- Consumes: `TOOL_NAME` 的字面值 `transfer_to_human`(樣板是 Jinja,不 import Python 常數,所以測試要守住兩邊一致)
- Produces: transfer 類規則在 prompt 裡是「呼叫 transfer_to_human,category 填 X」,**不出現 script**;apologize 類維持「直接回覆 script」

**這是 spec 裡「最容易漏掉的一步」。** 樣板不改,模型遇到「起紅疹」會照 prompt 念 script 而不呼叫工具 —— 客人看起來像被轉了,實際上沒切 HUMAN、店員沒收到通知。

- [ ] **Step 1: 寫失敗的測試,並改掉一個跟新行為衝突的既有斷言**

`tests/test_render.py` 的 `test_prompt_contains_faq_and_escalation` 改成(既有斷言要求 transfer 規則的 script 出現在 prompt 裡,正是這次要拿掉的行為):

```python
def test_prompt_contains_faq_and_escalation():
    p = render_system_prompt(DATA)
    assert "有停車位嗎" in p
    assert "過敏 送醫" in p   # 觸發情境還在,模型才知道什麼時候該轉
```

檔尾追加:

```python
from app.agent.handoff import TOOL_NAME  # noqa: E402


def test_transfer_rules_tell_the_model_to_call_the_tool_not_to_recite_the_script():
    """prompt 裡留著 script 的話,模型會直接把它念出來而不呼叫工具 ——
    客人看起來像被轉了,實際上沒切 HUMAN、店員沒收到通知。"""
    p = render_system_prompt(DATA)
    assert TOOL_NAME in p
    assert "category 填 safety" in p
    assert "我立刻請主管與您聯繫" not in p


def test_apologize_rules_still_give_the_script():
    data = {**DATA, "escalation": [{
        "level": "L1", "category": "service", "trigger": "上菜太慢",
        "action": "apologize", "script": "很抱歉讓您久等"}]}
    p = render_system_prompt(data)
    assert "很抱歉讓您久等" in p
    assert TOOL_NAME not in p
```

- [ ] **Step 2: 跑測試確認失敗**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_render.py -q
```

Expected: `test_transfer_rules_tell_the_model_to_call_the_tool_not_to_recite_the_script` FAILED(`transfer_to_human` 不在 prompt 裡)。其餘通過。

- [ ] **Step 3: 改樣板**

`prompts/system.j2` 的「## 什麼時候要轉給真人」整段換成:

```jinja
## 什麼時候要轉給真人
{% for item in escalation %}{% if item.action == "transfer" %}- 遇到「{{ item.trigger }}」這類情況({{ item.level }}),或意思相近的說法:
  不要自己回答、不要給建議,呼叫 transfer_to_human 工具,category 填 {{ item.category }}。
{% else %}- 遇到「{{ item.trigger }}」這類情況({{ item.level }}):
  直接回覆「{{ item.script }}」
{% endif %}{% endfor %}
```

- [ ] **Step 4: 跑測試確認通過**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_render.py tests/test_industries.py tests/test_cli.py -q
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: 全套 206 passed, 1 skipped。`test_industries.py` 不受影響(它檢查的是 yaml 的 script,不是 prompt)。

- [ ] **Step 5: Commit**

```powershell
git add prompts/system.j2 tests/test_render.py
git commit -m "feat(prompt): transfer 類規則改成呼叫工具,不再給 script

prompt 裡留著 script 的話,模型會直接把它念出來而不呼叫工具 ——
客人看起來像被轉了,實際上沒切 HUMAN、店員沒收到通知,而且不會
有任何錯誤訊息。

apologize 類(L1)維持直接回覆 script:它不需要轉真人。

test_prompt_contains_faq_and_escalation 原本斷言 transfer 規則的
script 出現在 prompt 裡,正是這次要拿掉的行為,改成斷言觸發情境。

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: webhook 分三條路

**Files:**
- Modify: `app/routers/webhook.py`
- Test: `tests/test_webhook.py`(檔尾追加)

**Interfaces:**
- Consumes: Task 1 的 `complete(..., tools=)`、`LLMResult.tool_call`、`ToolCall`;Task 2 的全部;Task 3 的兩個欄位
- Produces: `_handoff(...)`(模組內部用)

- [ ] **Step 1: 寫失敗的測試**

`tests/test_webhook.py` 檔尾追加(`_body`、`_post`、`_image_body` 是檔案裡既有的):

```python
# --- 轉真人 -------------------------------------------------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

from sqlalchemy import select  # noqa: E402

from app.agent.handoff import EXPIRED_PREFIX, GENERIC_SCRIPT  # noqa: E402
from app.agent.llm import ToolCall  # noqa: E402
from app.models import ConversationMode  # noqa: E402

SAFETY_SCRIPT = "這件事我立刻請店長與您聯繫。"
RULES = [
    {"level": "L3", "category": "safety", "trigger": "過敏、送醫",
     "action": "transfer", "script": SAFETY_SCRIPT},
    {"level": "L1", "category": "service", "trigger": "上菜太慢",
     "action": "apologize", "script": "很抱歉讓您有這樣的感受。"},
]


@pytest.fixture
def rules():
    with session_scope() as db:
        c = db.scalar(select(Company).where(Company.slug == "acme"))
        c.escalation_rules = RULES
        c.staff_notify_to = "Ustaff"


@pytest.fixture
def line_out(monkeypatch):
    """回給客人的(reply)與推給店員的(push)記在同一條時間軸上,
    才驗得了先後順序。"""
    out = []
    monkeypatch.setattr("app.routers.webhook.LineClient.send",
                        lambda self, rt, uid, text: out.append(("reply", text)) or True)
    monkeypatch.setattr("app.routers.webhook.LineClient.push",
                        lambda self, to, text: out.append(("push", to, text)) or True)
    return out


def _llm(monkeypatch, result=None):
    calls = []

    def fake(messages, **kw):
        calls.append(kw)
        return result or LLMResult("您好", token_count=1, latency_ms=1)

    monkeypatch.setattr("app.routers.webhook.complete", fake)
    return calls


def _mode():
    with session_scope() as db:
        u = db.scalar(select(User))
        return u.mode, u.mode_expires_at


def _expire():
    with session_scope() as db:
        db.scalar(select(User)).mode_expires_at = (
            datetime.now(timezone.utc) - timedelta(minutes=1))


def _replies(out):
    return [e[1] for e in out if e[0] == "reply"]


def _pushes(out):
    return [e for e in out if e[0] == "push"]


def test_a_keyword_hit_transfers_without_asking_the_model(rules, line_out, monkeypatch):
    calls = _llm(monkeypatch)
    with TestClient(app) as client:
        _post(client, _body(text="我朋友吃完過敏送醫了"))
    assert calls == []
    assert _replies(line_out) == [SAFETY_SCRIPT]
    (_, to, text), = _pushes(line_out)
    assert to == "Ustaff"
    assert "【安全】" in text and "判斷依據:關鍵字「過敏」" in text
    mode, expires = _mode()
    assert mode == ConversationMode.HUMAN and expires is not None


def test_the_customer_reply_goes_out_before_the_staff_push(rules, line_out, monkeypatch):
    """reply token 約一分鐘有效,推播沒有時限。"""
    _llm(monkeypatch)
    with TestClient(app) as client:
        _post(client, _body(text="過敏"))
    assert [e[0] for e in line_out] == ["reply", "push"]


def test_apologize_rules_do_not_transfer(rules, line_out, monkeypatch):
    calls = _llm(monkeypatch)
    with TestClient(app) as client:
        _post(client, _body(text="你們上菜太慢了"))
    assert len(calls) == 1
    assert _pushes(line_out) == []
    assert _mode()[0] == ConversationMode.AI


def test_messages_during_human_mode_get_no_reply(rules, line_out, monkeypatch):
    calls = _llm(monkeypatch)
    with TestClient(app) as client:
        _post(client, _body(text="過敏", msg_id="A"))
        _post(client, _body(text="還在嗎", msg_id="B"))
    assert calls == []
    assert _replies(line_out) == [SAFETY_SCRIPT]
    with session_scope() as db:
        # 存下來:真人在後台看得到,AI 之後接手時也有上下文
        assert "還在嗎" in [r.content for r in db.query(ChatHistory)]


def test_images_during_human_mode_get_no_reply(rules, line_out, monkeypatch):
    """「我只看得懂文字」會跟店員在後台的回覆混在一起。"""
    _llm(monkeypatch)
    with TestClient(app) as client:
        _post(client, _body(text="過敏", msg_id="A"))
        _post(client, _image_body(msg_id="IMG"))
    assert _replies(line_out) == [SAFETY_SCRIPT]


def test_an_expired_human_mode_returns_to_ai_with_a_heads_up(rules, line_out, monkeypatch):
    _llm(monkeypatch)
    with TestClient(app) as client:
        _post(client, _body(text="過敏", msg_id="A"))
        _expire()
        _post(client, _body(text="有停車位嗎", msg_id="B"))
    assert _replies(line_out)[-1] == EXPIRED_PREFIX + "您好"
    assert _mode() == (ConversationMode.AI, None)


def test_an_expired_human_mode_hit_again_has_no_heads_up(rules, line_out, monkeypatch):
    """「專員不在線上」接「我立刻請店長聯繫」自相矛盾。"""
    _llm(monkeypatch)
    with TestClient(app) as client:
        _post(client, _body(text="過敏", msg_id="A"))
        _expire()
        _post(client, _body(text="還是很不舒服要送醫", msg_id="B"))
    assert _replies(line_out) == [SAFETY_SCRIPT, SAFETY_SCRIPT]
    assert _mode()[0] == ConversationMode.HUMAN


def test_the_model_calling_the_tool_transfers(rules, line_out, monkeypatch):
    """這是整個設計的核心:沒命中關鍵字,由模型自己決定要轉。"""
    _llm(monkeypatch, LLMResult("", token_count=1, latency_ms=1, tool_call=ToolCall(
        "transfer_to_human", {"category": "safety", "reason": "起紅疹疑似過敏反應"})))
    with TestClient(app) as client:
        _post(client, _body(text="我女兒吃完全身起紅疹"))
    assert _replies(line_out) == [SAFETY_SCRIPT]
    (_, _, text), = _pushes(line_out)
    assert "判斷依據:AI —— 起紅疹疑似過敏反應" in text
    assert _mode()[0] == ConversationMode.HUMAN


def test_a_tool_call_wins_over_text(rules, line_out, monkeypatch):
    _llm(monkeypatch, LLMResult("我幫您轉給專人喔", token_count=1, latency_ms=1,
                                tool_call=ToolCall("transfer_to_human", {"category": "safety"})))
    with TestClient(app) as client:
        _post(client, _body(text="我女兒吃完全身起紅疹"))
    assert _replies(line_out) == [SAFETY_SCRIPT]


def test_an_unknown_category_from_the_model_still_transfers(rules, line_out, monkeypatch):
    _llm(monkeypatch, LLMResult("", token_count=1, latency_ms=1,
                                tool_call=ToolCall("transfer_to_human", {"category": "weather"})))
    with TestClient(app) as client:
        _post(client, _body(text="我女兒吃完全身起紅疹"))
    assert _replies(line_out) == [GENERIC_SCRIPT]
    assert _mode()[0] == ConversationMode.HUMAN


def test_a_failed_push_keeps_human_mode(rules, monkeypatch):
    """客人已經被告知「會有人聯繫」,AI 這時又開始回答反而更混亂(spec 決策 6)。"""
    out = []
    monkeypatch.setattr("app.routers.webhook.LineClient.send",
                        lambda self, rt, uid, text: out.append(text) or True)
    monkeypatch.setattr("app.routers.webhook.LineClient.push",
                        lambda self, to, text: False)
    _llm(monkeypatch)
    with TestClient(app) as client:
        _post(client, _body(text="過敏"))
    assert out == [SAFETY_SCRIPT]
    assert _mode()[0] == ConversationMode.HUMAN


def test_no_staff_target_means_no_push_but_still_transfers(rules, line_out, monkeypatch):
    with session_scope() as db:
        db.scalar(select(Company).where(Company.slug == "acme")).staff_notify_to = None
    _llm(monkeypatch)
    with TestClient(app) as client:
        _post(client, _body(text="過敏"))
    assert _pushes(line_out) == []
    assert _mode()[0] == ConversationMode.HUMAN


def test_the_tool_is_offered_only_when_there_are_transfer_rules(line_out, monkeypatch):
    calls = _llm(monkeypatch)
    with TestClient(app) as client:
        _post(client, _body(text="嗨", msg_id="A"))
    assert calls[0]["tools"] is None
    with session_scope() as db:
        db.scalar(select(Company).where(Company.slug == "acme")).escalation_rules = RULES
    with TestClient(app) as client:
        _post(client, _body(text="嗨", msg_id="B"))
    assert calls[1]["tools"][0]["function"]["name"] == "transfer_to_human"
```

- [ ] **Step 2: 跑測試確認失敗**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_webhook.py -q
```

Expected: 新增的 13 個大多 FAIL(例如 `test_a_keyword_hit_transfers_without_asking_the_model`:`calls` 不是空的、沒有 push、mode 仍是 AI;`test_the_tool_is_offered_only_when_there_are_transfer_rules`:`KeyError: 'tools'`)。既有的 19 個仍然通過。

- [ ] **Step 3: 實作 —— import 與 `_handoff`**

`app/routers/webhook.py` 的 import 區改成:

```python
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.agent.handoff import (
    EXPIRED_PREFIX, Decision, build_tool, customer_label, decision_from_tool,
    enter_human_mode, is_still_human, match_keyword, return_to_ai, staff_message,
)
from app.agent.llm import LLMError, complete
from app.agent.prompt import build_messages
from app.agent.zh import ensure_traditional
from app.config import get_settings
from app.crypto import decrypt
from app.database import session_scope
from app.line.client import LineClient, truncate_for_line
from app.line.signature import verify_signature
from app.models import ChatHistory, ChatRole, Company, ConversationMode, User
```

在 `_send_last_resort_fallback` 之前加:

```python
def _handoff(*, company_id: str, user_pk: str, client: LineClient,
             reply_token: str, line_user_id: str, decision: Decision, text: str,
             staff_to: str | None, timeout_minutes: int, customer: str) -> None:
    """轉真人。規則層與 agent 層共用 —— 兩層行為保證一致。

    先回客人、再推播店員:reply token 約一分鐘有效,推播沒有時限。
    推播失敗照樣維持 HUMAN(spec 決策 6):客人已經被告知「會有人聯繫」,
    AI 這時又開始回答反而更混亂。
    """
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        enter_human_mode(db.get(User, user_pk), timeout_minutes=timeout_minutes, now=now)
        db.add(ChatHistory(company_id=company_id, user_id=user_pk,
                           role=ChatRole.ASSISTANT, content=decision.script))
    client.send(reply_token, line_user_id, decision.script)

    if not staff_to:
        logger.warning("公司 %s 沒有設定 staff_notify_to,這次轉真人沒有通知任何人",
                       company_id)
        return
    # 自己包一層:推播爆炸時不能被最外層的 except 接走 —— 那會再送一句
    # fallback 給客人,而他剛剛才收到「我立刻請店長與您聯繫」。
    try:
        ok = client.push(staff_to, staff_message(decision, customer, text))
    except Exception:  # noqa: BLE001
        logger.exception("轉真人的店員通知爆炸")
        ok = False
    if not ok:
        logger.error("轉真人的店員通知送不出去(%s),客人仍維持 HUMAN 模式", staff_to)
```

- [ ] **Step 4: 實作 —— `process_text_event` 的分流**

`process_text_event` 裡,從 `if message_type != "text":` 那一行開始、到 `fallback = company.fallback_message` 為止,換成:

```python
            # HUMAN 模式要在非文字分支之前判斷:真人接手期間客人傳圖片,
            # 也不該冒出一句「我只看得懂文字」—— 會跟店員的回覆混在一起。
            now = datetime.now(timezone.utc)
            expired_prefix = ""
            if user.mode == ConversationMode.HUMAN:
                if is_still_human(user, now):
                    # 訊息上面已經存了:真人在後台看得到,AI 之後接手也有上下文
                    logger.info("客人 %s 由真人接手中,AI 不回", line_user_id)
                    return
                return_to_ai(user, now)
                expired_prefix = EXPIRED_PREFIX

            if message_type != "text":
                # 罐頭回覆也記下來,短期記憶才連得起來:客人傳了圖、我們說看不懂,
                # 下一句「那這個多少錢」模型才知道剛才發生過什麼。
                db.add(ChatHistory(company_id=company_id, user_id=user.id,
                                   role=ChatRole.ASSISTANT,
                                   content=NON_TEXT_REPLY))
                client.send(reply_token, line_user_id, NON_TEXT_REPLY)
                return

            user_pk = user.id
            rules = list(company.escalation_rules or [])
            staff_to = company.staff_notify_to
            timeout_minutes = company.human_mode_timeout_minutes
            customer = customer_label(user)

            # 規則層:命中就轉,不問模型。安全攸關的路徑不能只靠 LLM ——
            # 免費模型常塞車,模型沒呼叫工具時客人的緊急狀況就漏掉了。
            decision = match_keyword(rules, text)
            if decision is None:
                # 取最近 N 則,但排除剛剛寫進去的這一句 —— 它會由 build_messages
                # 以 user 角色放在最後面,重複放會讓模型看到問題出現兩次。
                rows = db.scalars(
                    select(ChatHistory)
                    .where(ChatHistory.user_id == user_pk,
                           ChatHistory.id != incoming.id)
                    .order_by(ChatHistory.id.desc())
                    .limit(settings.history_limit)
                ).all()
                history = [
                    {"role": "assistant" if r.role in (ChatRole.ASSISTANT,
                                                       ChatRole.HUMAN_AGENT) else "user",
                     "content": r.content}
                    for r in reversed(rows)
                ]
                system_prompt = company.system_prompt
                fallback = company.fallback_message

        handoff_args = dict(company_id=company_id, user_pk=user_pk, client=client,
                            reply_token=reply_token, line_user_id=line_user_id,
                            text=text, staff_to=staff_to,
                            timeout_minutes=timeout_minutes, customer=customer)
        if decision is not None:
            _handoff(decision=decision, **handoff_args)
            return
```

接著,把「LLM 呼叫放在 session 外面」那個 `try` 區塊換成:

```python
        # LLM 呼叫放在 session 外面:不要抓著資料庫連線等十五秒
        tool = build_tool(rules)
        try:
            result = complete(build_messages(system_prompt, history, text),
                              tools=[tool] if tool else None)
            answer, tokens, latency = result.text, result.token_count, result.latency_ms
        except LLMError as exc:
            logger.warning("LLM 失敗,改送 fallback:%s", exc)
            result = None
            answer, tokens, latency = fallback, None, None

        # agent 層:模型自己決定要轉。同時回了文字與工具呼叫時以工具為準 ——
        # 模型已經表達「需要真人」。
        if result is not None and result.tool_call is not None:
            _handoff(decision=decision_from_tool(rules, result.tool_call.arguments),
                     **handoff_args)
            return

        # 只有模型真的回了答案才加:fallback 前面接「專員不在線上」沒有意義
        if result is not None:
            answer = expired_prefix + answer
```

`show_loading` 那段、`ensure_traditional` 之後的程式都不動。

- [ ] **Step 5: 跑測試確認通過**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_webhook.py -q
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: `test_webhook.py` 32 passed;全套 219 passed, 1 skipped。

- [ ] **Step 6: 再用 PostgreSQL 跑一輪**

webhook 會寫 `mode_expires_at` 再讀回來比較,這正是 Global Constraint 3 那個 SQLite / PG 行為不同的地方:

```powershell
docker run --rm -d --name helpdesk-pgtest -e POSTGRES_PASSWORD=test -e POSTGRES_DB=helpdesk_test -p 55432:5432 postgres:17-alpine
Start-Sleep -Seconds 5
$env:DATABASE_URL = "postgresql+psycopg://postgres:test@127.0.0.1:55432/helpdesk_test"
.\.venv\Scripts\python.exe -m pytest -q
Remove-Item Env:DATABASE_URL
docker stop helpdesk-pgtest
```

Expected: 全綠。`test_alembic.py` 在 PG 上也照跑(它自己指向 tmp 的 SQLite 檔,不碰 PG 測試庫)。

- [ ] **Step 7: Commit**

```powershell
git add app/routers/webhook.py tests/test_webhook.py
git commit -m "feat(webhook): 轉真人 —— 關鍵字保底,模型用工具自己決定

去重之後分三條路:HUMAN 中不回 → 關鍵字命中直接轉(不問模型)→
帶 transfer_to_human 工具問模型。兩層轉真人走同一個 _handoff。

HUMAN 判斷放在非文字分支之前:真人接手期間客人傳圖片,不該冒出
「我只看得懂文字」跟店員的回覆混在一起。

逾時交還時只有模型真的回了答案才加「專員不在線上」—— 又被轉真人時
加上去,會變成「專員不在線上,我立刻請店長聯繫」自相矛盾。

推播自己包一層 try:爆炸時被最外層接走的話,客人會在「我立刻請
店長聯繫」之後又收到一句 fallback。

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: 模型預設值、文件、部署與手機驗收

**Files:**
- Modify: `app/config.py`、`compose.yaml`、`.env.example`、`.env`(本機,不進版控)
- Modify: `tests/test_config.py`
- Modify: `docs/demo-checklist.md`、`README.md`

**Interfaces:**
- Consumes: 前六個 task 的全部產出
- Produces: 無程式介面。產出是**驗收標準通過的證據**。

**這個 task 有要人做的步驟(Step 5、7),不能由程式代勞。**

- [ ] **Step 1: 寫失敗的測試**

`tests/test_config.py` 檔尾追加:

```python
def test_default_models_both_support_tool_calling(monkeypatch):
    """z-ai/glm-5.2:free 不支援 tools(OpenRouter 的 supported_parameters 沒有
    tools)。把它設成主要模型的話,每一次請求都會失敗再退回,轉真人的
    agent 層等於只剩一半在工作,而且 log 只看得到「模型失敗」。"""
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_FALLBACK_MODEL", raising=False)
    s = Settings(_env_file=None, fernet_key="k", openrouter_api_key="k")
    assert s.openrouter_model == "openrouter/free"
    assert s.openrouter_fallback_model == "nvidia/nemotron-3-super-120b-a12b:free"
```

- [ ] **Step 2: 跑測試確認失敗**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_config.py -q
```

Expected: FAIL —— `assert 'nvidia/nemotron-3-super-120b-a12b:free' == 'openrouter/free'`

- [ ] **Step 3: 四個地方的預設值一起改**

`app/config.py`:

```python
    # 兩個都必須支援 tool calling(轉真人的 agent 層靠它)。openrouter/free 是
    # 自動路由:請求帶 tools 時只會分到支援工具的模型。2026-09-24 實測一整天,
    # 偏好模型每次都限流,真正在答的一直是它 —— 所以直接讓它當主要。
    openrouter_model: str = "openrouter/free"
    openrouter_fallback_model: str = "nvidia/nemotron-3-super-120b-a12b:free"
```

(取代原本的 `openrouter_model` 與 `openrouter_fallback_model` 兩行與它們上方的註解。)

`compose.yaml`:

```yaml
      OPENROUTER_MODEL: ${OPENROUTER_MODEL:-openrouter/free}
      OPENROUTER_FALLBACK_MODEL: ${OPENROUTER_FALLBACK_MODEL:-nvidia/nemotron-3-super-120b-a12b:free}
```

`.env.example` 的兩行:

```bash
OPENROUTER_MODEL=openrouter/free
```

```bash
OPENROUTER_FALLBACK_MODEL=nvidia/nemotron-3-super-120b-a12b:free
```

(並把兩行上方的註解改成跟 `app/config.py` 同一個理由:兩個都必須支援 tool calling。)

本機 `.env`(不進版控,**不加 BOM**):

```powershell
$p = (Resolve-Path .env).Path
$t = [IO.File]::ReadAllText($p)
$n = $t.Replace("OPENROUTER_MODEL=z-ai/glm-5.2:free", "OPENROUTER_MODEL=openrouter/free")
if ($n -eq $t) { throw "沒有換到 —— 先看 .env 裡 OPENROUTER_MODEL 現在是什麼" }
[IO.File]::WriteAllText($p, $n, (New-Object Text.UTF8Encoding $false))
Select-String -Path .env -Pattern "^OPENROUTER_" -Encoding utf8 | ForEach-Object { $_.Line }
```

- [ ] **Step 4: 跑測試、部署、確認 PostgreSQL 遷移**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
docker compose up -d --build app
Start-Sleep -Seconds 10
docker compose logs app --tail 20
docker compose exec -T db psql -U helpdesk -d helpdesk -c "\d companies" | Select-String "escalation_rules|staff_notify_to"
docker compose exec -T app python -c "from app.config import get_settings as g; s=g(); print(s.openrouter_model, '/', s.openrouter_fallback_model)"
curl.exe -s https://unsaid-expend-eagle.ngrok-free.dev/health
```

Expected:
- 全套 220 passed, 1 skipped
- log 裡有 `Running upgrade 66766e088730 -> 3f2a9c1d7b4e` —— **這是對著有真實資料的 PostgreSQL 跑遷移**,驗的是 Task 3 那條 server_default
- `\d companies` 列出兩個新欄位
- 模型印出 `openrouter/free / nvidia/nemotron-3-super-120b-a12b:free`
- `/health` 回 ok

- [ ] **Step 5: 人要做的一次性設定**

1. **LINE Official Account Manager → 回應設定**:打開**聊天**。Webhook 維持開著,**自動回應訊息**維持關閉。
2. **LINE Developers Console → 該 channel → Basic settings → Your user ID**:抄下來。這個帳號當店員。
3. 用**店員帳號**加 bot 好友(推播送不到非好友)。
4. 準備**另一個 LINE 帳號**當客人。

- [ ] **Step 6: 重新 seed(換回餐廳、寫入規則與通知對象)**

```powershell
docker compose exec -T app python -m app.cli seed --industry restaurant --slug bistro --reset-history --staff-notify-to <Step 5 抄下的 user ID>
docker compose exec -T db psql -U helpdesk -d helpdesk -At -c "select industry, staff_notify_to is not null, json_array_length(escalation_rules) from companies;"
```

Expected: `restaurant|t|5`

- [ ] **Step 7: 手機驗收(spec 第九節)**

用**客人帳號**依序做。每一步做完,跑下面的指令看結果:

```powershell
docker compose logs app --since 2m 2>&1 | Select-String -NotMatch "GET /health"
docker compose exec -T db psql -U helpdesk -d helpdesk -c "select u.mode, u.mode_expires_at at time zone 'Asia/Taipei' as expires, h.role, left(h.content,40) from chat_histories h join users u on u.id = h.user_id order by h.id desc limit 6;"
```

| # | 操作 | 預期 |
|---|---|---|
| 1 | 傳「有停車位嗎」 | AI 正常回答 |
| 2 | 傳「我朋友吃完過敏送醫了」 | 回「這件事我立刻請店長與您聯繫…」;**店員手機跳通知**,判斷依據是關鍵字「過敏」;log **沒有** `openrouter.ai` 那行 |
| 3 | 再傳「還在嗎」 | AI 不回;店員在官方帳號後台回一句,客人收得到 |
| 4 | 跑 `docker compose exec -T app python -m app.cli release --slug bistro`,再傳「我女兒吃完全身起紅疹」 | 沒命中關鍵字,**模型呼叫工具**;店員手機跳通知,判斷依據是「AI —— …」 |
| 5 | `seed --industry clinic --slug bistro --reset-history` 後傳「我這個症狀是不是癌症」 | 命中診所 safety 的「我這個症狀是不是」;回診所的 script;店員手機跳通知 |

**第 4 步是核心。** 如果模型沒呼叫工具、而是直接回了一段文字,先看 log 裡是哪個模型答的,再用同一句重試一次 —— 免費模型的判斷不穩定是預期中的風險。**連續兩次都沒呼叫工具就停下來回報,不要改 prompt 硬湊。**

- [ ] **Step 8: 文件**

`docs/demo-checklist.md` 的「第一次串接才要做的」最後加:

````markdown
- [ ] LINE Official Account Manager → 回應設定 → 打開**聊天**(真人客服在官方帳號後台回覆)
- [ ] 店員帳號加 bot 好友,從 LINE Developers Console → Basic settings → **Your user ID**
      抄下店員的 userId:
      ```bash
      docker compose exec app python -m app.cli seed --industry restaurant --slug bistro --staff-notify-to Uxxxx
      ```
      沒做這步不會壞,但轉真人時沒有人收到通知 —— 客人會在 30 分鐘內都收不到回覆。
````

「現場要示範的四句話」整節換成 spec 第九節那張五步驟的表,並在表後加:

````markdown
**示範要兩個 LINE 帳號**:一個當客人,一個當店員(收通知、在後台回覆)。

轉真人之後客人會卡在 HUMAN 模式 30 分鐘。要接著示範下一段:

```bash
docker compose exec app python -m app.cli release --slug bistro
```
````

`README.md` 的「狀態」一節第一句之後加一句:

```markdown
轉真人已 agent 化:關鍵字規則保底,模型透過 `transfer_to_human` 工具判斷換句話說的情況;
轉了之後 AI 停止回答、推播通知店員、30 分鐘後自動交還。
```

- [ ] **Step 9: Commit**

```powershell
git add app/config.py compose.yaml .env.example tests/test_config.py docs/demo-checklist.md README.md
git commit -m "chore(llm): 預設模型改成兩個都支援 tool calling,補示範文件

z-ai/glm-5.2:free 不支援 tools。當主要模型的話每一次請求都會失敗
再退回,轉真人的 agent 層等於只剩一半在工作,而 log 只看得到
「模型失敗」。

openrouter/free 帶 tools 時只會分到支援工具的模型,而且 2026-09-24
實測一整天,真正在答的一直是它 —— 直接讓它當主要。

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## 完成後的狀態

對照 [spec 第九節驗收標準](../specs/2026-09-24-agent-handoff-design.md):

| # | 驗收項目 | 由哪裡保證 |
|---|---|---|
| 1 | 自動測試全綠,兩種資料庫 | 每個 task 的最後一步;Task 6 Step 6(PG);CI |
| 2-1 | AI 正常回答 | Task 7 Step 7 #1 |
| 2-2 | 關鍵字轉真人、不問模型、店員收到通知 | Task 6 `test_a_keyword_hit_transfers_without_asking_the_model`;Task 7 Step 7 #2 |
| 2-3 | HUMAN 中 AI 不回、店員後台回得到 | Task 6 `test_messages_during_human_mode_get_no_reply`;Task 7 Step 7 #3 |
| 2-4 | **模型自己決定轉真人** | Task 6 `test_the_model_calling_the_tool_transfers`;Task 7 Step 7 #4 |
| 2-5 | 換行業照樣轉 | Task 4 `test_switching_industry_rewrites_the_escalation_rules`;Task 7 Step 7 #5 |
| — | PostgreSQL 遷移對著真實資料跑得過 | Task 7 Step 4 |

## 沒有做、而且是故意的

| 缺口 | 寫在哪 |
|---|---|
| 真人客服的網頁後台、真人說的話進對話紀錄 | spec 第一節「不做」 |
| 真人每回一句就延長期限 | spec 第一節(我們看不到真人什麼時候回) |
| 多步驟 agent 迴圈、其他工具 | spec 決策 4 |
| 店員群組(groupId) | spec 第十一節開放問題 |
