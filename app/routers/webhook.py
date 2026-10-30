"""LINE webhook 入口。

流程刻意分成「同步的驗證」與「背景的處理」兩段:
LINE 沒收到 2xx 會重送,而 LLM 要跑 3-15 秒,同步等一定超時 ——
客人會收到兩次一樣的答案。
"""

from __future__ import annotations

import json
import logging
import math
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.agent.classifier import classify
from app.agent.handoff import (
    EXPIRED_PREFIX, RECITED_HISTORY_MARKER, Decision, build_tool, customer_label,
    decision_from_promised_transfer, decision_from_recited_script, decision_from_tool,
    enter_human_mode,
    is_handoff_script, is_still_human, match_keyword, return_to_ai, staff_message,
)
from app.agent.llm import complete
from app.agent.loop import run_agent
from app.agent.prompt import build_messages
from app.agent.zh import ensure_traditional
from app import booking, ratelimit
from app.config import get_settings
from app.crypto import decrypt
from app.database import session_scope
from app.line.client import LineClient, truncate_for_line
from app.line.signature import verify_signature
from app.models import ChatHistory, ChatRole, Company, ConversationMode, User

logger = logging.getLogger(__name__)
router = APIRouter()

NON_TEXT_REPLY = "我目前只看得懂文字訊息,麻煩您用打字的跟我說 🙏"

# 非文字訊息在對話紀錄裡長什麼樣子。這些字串會被當成歷史餵回給模型,
# 所以用中文標記而不是原始的英文型別 —— 模型看得懂「[圖片]」,
# 看到 "image" 可能會以為對話裡冒出了一個英文單字。
NON_TEXT_LABELS = {
    "image": "[圖片]", "sticker": "[貼圖]", "video": "[影片]",
    "audio": "[語音]", "file": "[檔案]", "location": "[位置]",
}


def _label(message_type: str) -> str:
    return NON_TEXT_LABELS.get(message_type, f"[{message_type or '未知訊息'}]")


# 轉真人分類層跟主模型同時跑(設計見 docs/superpowers/specs/2026-10-16-handoff-classifier-design.md)。
# process_text_event 本身已經在 BackgroundTasks 的 threadpool 裡,再開執行緒是安全的;
# 共用一個池子,尖峰時不會無上限地開執行緒。
_classifier_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="handoff-classifier")


@dataclass(frozen=True)
class _PendingClassifier:
    future: Future
    deadline: float  # time.monotonic() 的秒數;HTTP 與等待用同一個截止時間


def _start_classifier(settings, rules: list[dict], text: str,
                      company_name: str) -> _PendingClassifier | None:
    if not settings.handoff_classifier_enabled:
        return None
    deadline = time.monotonic() + settings.handoff_classifier_budget_seconds
    try:
        future = _classifier_pool.submit(classify, rules, text, company_name=company_name,
                                         deadline=deadline, complete_fn=complete)
    except Exception:  # noqa: BLE001
        logger.warning("分類層沒送出去,這一則不看它", exc_info=True)
        return None
    return _PendingClassifier(future, deadline)


def _await_classifier(pending: _PendingClassifier | None) -> Decision | None:
    """等分類層的結果,最多到它的截止時間。等不到、壞掉都當沒判斷 ——
    這一層只會多轉、不會少轉。"""
    if pending is None:
        return None
    try:
        return pending.future.result(timeout=max(0.0, pending.deadline - time.monotonic()))
    except FuturesTimeout:
        logger.warning("分類層超過截止時間沒回,這一則不看它")
    except Exception:  # noqa: BLE001
        logger.warning("分類層失敗,這一則不看它", exc_info=True)
    return None


@router.post("/webhook/{slug}")
async def line_webhook(slug: str, request: Request, background: BackgroundTasks):
    # 1. 原始位元組。絕對不能宣告 Pydantic body model —— 那等於已經 parse 過,
    #    重新序列化後的位元組跟 LINE 算簽章用的不一樣。
    raw = await request.body()

    with session_scope() as db:
        company = db.scalar(select(Company).where(Company.slug == slug))
        if company is None or not company.is_active:
            raise HTTPException(status_code=404, detail="unknown channel")
        company_id = company.id
        channel_secret = decrypt(company.line_channel_secret_enc)
        expected_destination = company.line_destination

    # 2. 驗簽章
    if not verify_signature(channel_secret, raw,
                            request.headers.get("X-Line-Signature")):
        raise HTTPException(status_code=401, detail="bad signature")

    # 3. 這時才 parse
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="body is not JSON") from exc

    # 4. destination 交叉比對:路徑說是 A 公司、body 卻是 B 公司的 bot,拒絕
    destination = body.get("destination")
    if expected_destination and destination and destination != expected_destination:
        raise HTTPException(status_code=401, detail="destination mismatch")
    if not expected_destination:
        # 這家公司 seed 時沒帶 --destination,所以上面那道比對是關著的。
        # 把收到的值印出來,才有辦法補進資料庫把防線打開。
        logger.info("%s 尚未設定 destination,本次收到的是 %s ——"
                    " 用 --destination %s 重跑一次 seed 即可開啟交叉比對",
                    slug, destination, destination)

    # 5. 排程背景工作後立刻回 200(Console 按 Verify 時 events 是空的)
    for event in body.get("events") or []:
        if event.get("type") != "message":
            continue
        message = event.get("message") or {}
        source = event.get("source") or {}
        user_id = source.get("userId")
        if not user_id:
            continue  # 群組訊息沒有 userId,v1 不處理
        background.add_task(
            process_text_event,
            company_id=company_id,
            line_user_id=user_id,
            text=message.get("text") or "",
            message_type=message.get("type") or "",
            line_message_id=message.get("id") or "",
            reply_token=event.get("replyToken") or "",
            # 官方帳號開了「聊天」時 LINE 才會附上;沒開時訊息本來就自動已讀
            mark_as_read_token=message.get("markAsReadToken") or "",
        )
    return {"status": "ok"}


def process_text_event(*, company_id: str, line_user_id: str, text: str,
                       message_type: str, line_message_id: str,
                       reply_token: str, mark_as_read_token: str = "") -> None:
    """背景任務。

    只收純值,不收 ORM 物件 —— FastAPI 的 Depends(get_db) session 在
    response 送出時就關了,帶著它的物件過來會噴 DetachedInstanceError,
    而錯誤訊息完全指不到真正原因。這裡自己開 session_scope()。
    """
    settings = get_settings()
    try:
        with session_scope() as db:
            company = db.get(Company, company_id)
            if company is None:
                return
            client = LineClient(decrypt(company.line_channel_token_enc))

            user = db.scalar(select(User).where(
                User.company_id == company_id,
                User.line_user_id == line_user_id))
            if user is None:
                user = User(company_id=company_id, line_user_id=line_user_id)
                db.add(user)
                db.flush()

            # 去重交給資料庫的 unique 索引 —— 自己「先查再寫」有 race condition。
            # 非文字訊息也必須走這一步:原本它在這之前就 return,於是整條去重
            # 被繞過。Console 的 Webhook redelivery 開著時,LINE 重送一張圖
            # 客人就連收兩次「我只看得懂文字」—— 而文字訊息不會有這個問題,
            # 所以測試套件一直沒抓到。
            incoming = ChatHistory(
                company_id=company_id, user_id=user.id, role=ChatRole.USER,
                content=text if message_type == "text" else _label(message_type),
                line_message_id=line_message_id)
            db.add(incoming)
            try:
                db.flush()
            except IntegrityError:
                db.rollback()
                logger.info("重送的訊息 %s,略過", line_message_id)
                return

            # F2:店員帳號加了 bot 好友後,對 bot 打字(手滑、測試)不該被
            # 當成客人訊息 —— 沒有 AI 回答的必要,更不能被規則層轉真人,
            # 變成推播通知去吵自己。訊息本身照樣留在 chat_histories 裡
            # (上面已經寫入),只是到此為止,不觸發回覆或任何狀態變更。
            if company.staff_notify_to and line_user_id == company.staff_notify_to:
                logger.info("%s 是店員帳號,不當客人訊息處理", line_user_id)
                return

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
                _mark_read(client, mark_as_read_token)
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
                # 限流只擋要打模型的這條路:關鍵字轉真人(上面的 match_keyword)與
                # 非文字的固定回覆都不花模型額度,而安全觸發不管傳了幾則都要轉。放在
                # 「正在輸入」之前 —— 被擋的訊息不該讓客人看到輸入中卻什麼都沒收到。
                verdict = ratelimit.check(db, user_pk=user_pk, now=now,
                                          limits=ratelimit.Limits.from_settings(settings))
                if verdict.action is not ratelimit.Action.ALLOW:
                    logger.warning("限流 客人 …%s 規則=%s 計數=%d 動作=%s",
                                   line_user_id[-6:], verdict.rule, verdict.count,
                                   verdict.action.name)
                    reply = ratelimit.reply_text(verdict, company.contact)
                    if reply is None:
                        # 安靜:不回、不標已讀 —— 店家在官方帳號後台看得到有人在狂傳
                        return
                    # 寫進紀錄:模型下一輪看得懂上下文,「只提醒一次」也靠它
                    db.add(ChatHistory(company_id=company_id, user_id=user_pk,
                                       role=ChatRole.ASSISTANT, content=reply))
                    client.send(reply_token, line_user_id, reply)
                    _mark_read(client, mark_as_read_token)
                    return

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
                     # F1a:上一輪轉真人時存的 script,原封不動餵回去的話,
                     # 模型會把「上次這樣講」當範例學著念,而不是呼叫工具
                     # (客人以為被轉了,實際上 HUMAN 沒切)。DB 那一列本身
                     # 不能改 —— 客人當時真的看到那句話。
                     "content": (RECITED_HISTORY_MARKER
                                if r.role == ChatRole.ASSISTANT
                                and is_handoff_script(rules, r.content)
                                else r.content)}
                    for r in reversed(rows)
                ]
                system_prompt = company.system_prompt
                fallback = company.fallback_message
                booking_config = company.booking_config
                contact = company.contact
                company_name = company.name

        handoff_args = dict(company_id=company_id, user_pk=user_pk, client=client,
                            reply_token=reply_token, line_user_id=line_user_id,
                            text=text, staff_to=staff_to,
                            timeout_minutes=timeout_minutes, customer=customer)
        if decision is not None:
            _handoff(decision=decision, **handoff_args)
            return

        # 先讓客人的對話框出現「正在輸入」。agent loop 可能跑好幾步、最多 45 秒,
        # 那段沉默會讓人以為訊息沒送出去而重傳。
        #
        # 自己包一層 try:這是純裝飾性的呼叫,不該有能力毀掉主流程。沒有這層的話
        # 它丟出的例外會被最外層的 except 接走,客人收到的是 fallback 訊息而不是
        # 真正的答案 —— 答案明明算得出來,卻因為動畫沒叫成功而丟掉。
        try:
            client.show_loading(line_user_id,
                                seconds=_loading_seconds(settings.agent_total_budget_seconds))
        except Exception:  # noqa: BLE001
            logger.warning("輸入中動畫沒叫成功,不影響回覆", exc_info=True)

        # 模型呼叫放在 session 外面:不要抓著資料庫連線等好幾十秒。預約工具執行時
        # 才各自開一個短的 session。
        tools = [t for t in (build_tool(rules),) if t]
        if booking_config:
            system_prompt += "\n\n" + booking.booking_prompt(
                booking_config, today=booking.today_taipei(now), contact=contact)
            tools += booking.build_tools(booking_config)

        # 分類層先送出去,跟主模型同時跑:只看升級規則與這一句,不受 FAQ 稀釋。
        classifier = _start_classifier(settings, rules, text, company_name)

        # 這一輪真正新增的預約。店員只通知這些:模型重複呼叫 create_booking 時
        # 工具回 duplicate(同一位客人同一時段已經有一筆),那一筆店員早就收過了。
        new_bookings: list[dict] = []

        def execute(name: str, arguments: dict) -> dict:
            if booking_config and name == booking.CHECK_TOOL:
                with session_scope() as tdb:
                    return booking.check_availability(
                        tdb, company_id=company_id, config=booking_config,
                        contact=contact, now=datetime.now(timezone.utc),
                        arguments=arguments)
            if booking_config and name == booking.CREATE_TOOL:
                with session_scope() as tdb:
                    result = booking.create_booking(
                        tdb, company_id=company_id, user_pk=user_pk,
                        config=booking_config, contact=contact,
                        now=datetime.now(timezone.utc), arguments=arguments)
                # session 離開(commit 成功)之後才記:commit 失敗不算成立
                if result.get("ok") and not result.get("duplicate"):
                    new_bookings.append(result["booking"])
                return result
            return {"ok": False, "error": "沒有這個工具"}

        agent = run_agent(build_messages(system_prompt, history, text),
                          tools=tools or None, execute=execute,
                          max_steps=settings.agent_max_steps,
                          budget_seconds=settings.agent_total_budget_seconds,
                          complete_fn=complete)
        logger.info("agent 步數=%d 工具=%s", agent.steps, ",".join(agent.tools_used) or "無")

        # 預約成立了,但送出去的若不是模型自己的回答(模型失敗、轉真人、安全網;
        # 最後一步執行了工具也會讓 text 是 None),就由程式補確認句 —— 客人絕不能訂好了
        # 卻不知道。一輪訂到好幾筆時每筆一句:只講最後一筆,客人不知道還有別筆。
        # 重複呼叫時那一筆是已存在的預約,對客人再說一次無妨。
        booked = agent.bookings or ((agent.booking,) if agent.booking else ())
        confirmation = "\n".join(booking.confirmation_text(booking_config, b) for b in booked)

        def notify_new_bookings() -> None:
            # 一定要在回覆客人之後才呼叫:reply token 約一分鐘有效、loop 最多用掉
            # 45 秒,推播沒有時限(跟 _handoff 先回客人、再推播店員同一條規則)。
            for b in new_bookings:
                logger.info("%s成立 客人 …%s %s %s %s 位", booking_config["label"],
                            line_user_id[-6:], b["date"], b["time"], b["party_size"])
                if staff_to:
                    _push_staff(client, staff_to,
                                booking.staff_message(booking_config, b))

        # agent 層:模型自己決定要轉。轉真人優先於同一回應裡的其他工具(loop 處理)。
        if agent.handoff is not None:
            _handoff(decision=decision_from_tool(rules, agent.handoff.arguments),
                     prefix=confirmation, **handoff_args)
            notify_new_bookings()
            return

        # 分類層:模型沒呼叫工具時才看(模型自己呼叫的有類別與理由,最完整)。
        # 排在 fallback 前面 —— 模型失敗但分類層說要轉,照樣轉;排在兩道安全網前面,
        # 類別才不會被標成「轉真人」。客人聽到的是店家的 script,模型臨場寫的那段丟掉。
        classified = _await_classifier(classifier)
        if classified is not None:
            _handoff(decision=classified, prefix=confirmation, **handoff_args)
            notify_new_bookings()
            return

        if agent.text is None:
            # 模型失敗、超時,或最後一步執行了工具(文字丟掉了,見 loop.py)。
            # 已經成立預約的話只送確認句 —— 客人要辦的事辦成了,
            # 再接「連線有點問題,可以再問一次嗎」只會讓人以為沒訂到。
            answer = confirmation or fallback
            tokens, latency = None, None
        else:
            # F1b 安全網:模型沒呼叫工具,卻照 prompt 把 script 整句念出來 ——
            # 客人聽起來像被轉了,實際上沒切 HUMAN、店員也沒收到通知。
            recited = decision_from_recited_script(rules, agent.text)
            if recited is not None:
                _handoff(decision=recited, prefix=confirmation, **handoff_args)
                notify_new_bookings()
                return
            # 第二道:沒照念 script,而是自己改寫成「我將為您轉接」(2026-09-29 真機)。
            # 排在念 script 那道之後 —— 店家的 script 本身也含「請店長」這類字,
            # 照念時要保留它自己的類別與原句。
            promised = decision_from_promised_transfer(agent.text)
            if promised is not None:
                _handoff(decision=promised, prefix=confirmation, **handoff_args)
                notify_new_bookings()
                return
            # 簡繁兜底只套在模型自己寫的字,而且在寫入資料庫之前 —— 對話紀錄要跟客人
            # 實際看到的一致。prompt 已經規定繁體,但那只降低漂移消不掉(真機上出現過
            # 「停车位」)。確認句與店家的 fallback 本來就是繁體,不再轉。
            model_text = ensure_traditional(agent.text)
            if agent.booking:
                # OpenCC 會改到常見姓氏(余→餘、于→於):訂位人的名字換回客人給的寫法
                name = agent.booking["name"]
                converted = ensure_traditional(name)
                if converted != name:
                    model_text = model_text.replace(converted, name)
            # 假確認安全網(spec 2026-10-30):模型說訂好了,這一輪卻沒有成立任何預約 ——
            # 2026-10-27 量測 S6、10/30 線上都出現過,客人會以為訂到了。不送那句,改送固定回覆
            # 請客人一次傳來(一句話講完的訂位實測 10/10 成立)。放在簡繁轉換之後,簡體的也抓得到。
            # log 只記命中的字:原文有客人姓名電話。
            claimed = (booking.claims_booked(model_text)
                       if booking_config and not booked else None)
            if claimed:
                logger.warning("假確認擋下 客人 …%s 命中「%s」", line_user_id[-6:], claimed)
                model_text = booking.unconfirmed_text(booking_config, contact)
            # 只有模型真的回了答案才加:fallback 前面接「專員不在線上」沒有意義
            answer = expired_prefix + model_text
            tokens, latency = agent.token_count, agent.latency_ms

        answer = truncate_for_line(answer, settings.line_max_text_length)

        with session_scope() as db:
            db.add(ChatHistory(company_id=company_id, user_id=user_pk,
                               role=ChatRole.ASSISTANT, content=answer,
                               token_count=tokens, latency_ms=latency))

        client.send(reply_token, line_user_id, answer)
        # 送出之後才標:先標的話,模型最後決定轉真人時訊息早就已讀了。
        # 轉真人與 HUMAN 模式走不到這裡 —— 那些留給真人在後台點開才已讀,
        # 客人看到「未讀」代表真人還沒看到。
        _mark_read(client, mark_as_read_token)
        notify_new_bookings()
    except Exception:
        # 沉默是唯一不被接受的失敗模式
        logger.exception("背景處理爆炸,嘗試送出 fallback")
        _send_last_resort_fallback(company_id, line_user_id, reply_token)


def _mark_read(client: LineClient, mark_as_read_token: str) -> None:
    """跟「正在輸入」一樣是裝飾性的呼叫,不該有能力毀掉主流程。

    自己包一層 try:答案已經送出去了,這裡爆炸被最外層的 except 接走的話,
    客人會在正確答案之後又收到一句 fallback。
    """
    try:
        client.mark_as_read(mark_as_read_token)
    except Exception:  # noqa: BLE001
        logger.warning("標示已讀沒成功,不影響回覆", exc_info=True)


def _loading_seconds(budget: float) -> int:
    """LINE 只接受 5 到 60 秒、5 的倍數,不合法直接回 400。"""
    return max(5, min(60, int(math.ceil(budget / 5) * 5)))


def _push_staff(client: LineClient, staff_to: str, message: str) -> None:
    """新預約通知店員。自己包一層:推播失敗不能影響已經成立的預約與給客人的回覆。"""
    try:
        if not client.push(staff_to, message):
            logger.error("新預約的店員通知送不出去(%s)", staff_to)
    except Exception:  # noqa: BLE001
        logger.exception("新預約的店員通知爆炸")


def _handoff(*, company_id: str, user_pk: str, client: LineClient,
             reply_token: str, line_user_id: str, decision: Decision, text: str,
             staff_to: str | None, timeout_minutes: int, customer: str,
             prefix: str = "") -> None:
    """轉真人。規則層與 agent 層共用 —— 兩層行為保證一致。

    先回客人、再推播店員:reply token 約一分鐘有效,推播沒有時限。
    推播失敗照樣維持 HUMAN(spec 決策 6):客人已經被告知「會有人聯繫」,
    AI 這時又開始回答反而更混亂。
    prefix:本次成立的預約確認句,接在轉真人那句話前面。
    """
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        enter_human_mode(db.get(User, user_pk), timeout_minutes=timeout_minutes, now=now)
        if prefix:
            # 確認句自己一列:script 那一列要保持原樣,is_handoff_script 才認得出來
            # (F1a 的歷史標記靠它)
            db.add(ChatHistory(company_id=company_id, user_id=user_pk,
                               role=ChatRole.ASSISTANT, content=prefix))
        db.add(ChatHistory(company_id=company_id, user_id=user_pk,
                           role=ChatRole.ASSISTANT, content=decision.script))
    # 給示範現場正面的 log 證據:轉真人這件事本身,以及是規則轉的還是
    # 模型轉的,不必等到查資料庫才知道。
    logger.info("轉真人 category=%s 依據=%s", decision.category, decision.basis)
    if not client.send(reply_token, line_user_id, prefix + decision.script):
        # reply 跟 push 都失敗:客人已經進了 HUMAN 模式,卻什麼都沒收到 ——
        # 比推播失敗更嚴重(推播失敗客人至少收得到 script)。
        logger.error("轉真人的回覆送不出去(%s),客人在 HUMAN 模式但沒收到任何訊息",
                     line_user_id)

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


def _send_last_resort_fallback(company_id: str, line_user_id: str,
                               reply_token: str) -> None:
    """最後一道防線:上面任何一步爆炸時,至少讓客人收到一句話。

    這裡刻意重新開 session、重新解一次 token,不沿用上面的任何變數 ——
    會走到這裡就表示前面隨便哪個東西都可能是壞的或根本還沒被指派。

    自己再爆炸就只能放棄,但絕不能把新的例外往外丟:這是 BackgroundTasks
    的最外層,再往外沒有人接,而客人那邊早就收到 200 了。
    """
    try:
        with session_scope() as db:
            company = db.get(Company, company_id)
            if company is None:
                return
            token = decrypt(company.line_channel_token_enc)
            message = company.fallback_message
        LineClient(token).send(reply_token, line_user_id, message)
    except Exception:
        logger.exception("連 fallback 都送不出去,放棄")
