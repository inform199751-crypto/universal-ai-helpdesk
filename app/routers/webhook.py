"""LINE webhook 入口。

流程刻意分成「同步的驗證」與「背景的處理」兩段:
LINE 沒收到 2xx 會重送,而 LLM 要跑 3-15 秒,同步等一定超時 ——
客人會收到兩次一樣的答案。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.agent.handoff import (
    EXPIRED_PREFIX, RECITED_HISTORY_MARKER, Decision, build_tool, customer_label,
    decision_from_recited_script, decision_from_tool, enter_human_mode,
    is_handoff_script, is_still_human, match_keyword, return_to_ai, staff_message,
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
        )
    return {"status": "ok"}


def process_text_event(*, company_id: str, line_user_id: str, text: str,
                       message_type: str, line_message_id: str,
                       reply_token: str) -> None:
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

        handoff_args = dict(company_id=company_id, user_pk=user_pk, client=client,
                            reply_token=reply_token, line_user_id=line_user_id,
                            text=text, staff_to=staff_to,
                            timeout_minutes=timeout_minutes, customer=customer)
        if decision is not None:
            _handoff(decision=decision, **handoff_args)
            return

        # 先讓客人的對話框出現「正在輸入」。LLM 要跑 3-15 秒,那段沉默會讓人
        # 以為訊息沒送出去而重傳。
        #
        # 自己包一層 try:這是純裝飾性的呼叫,不該有能力毀掉主流程。沒有這層的話
        # 它丟出的例外會被最外層的 except 接走,客人收到的是 fallback 訊息而不是
        # 真正的答案 —— 答案明明算得出來,卻因為動畫沒叫成功而丟掉。
        try:
            client.show_loading(line_user_id)
        except Exception:  # noqa: BLE001
            logger.warning("輸入中動畫沒叫成功,不影響回覆", exc_info=True)

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

        # F1b 安全網:模型沒呼叫工具,卻照 prompt 把 script 整句念出來 ——
        # 客人聽起來像被轉了,實際上沒切 HUMAN、店員也沒收到通知,是 spec
        # §四警告的失敗模式。當成轉真人處理,不要照樣把 script 當一般答案送出。
        if result is not None:
            recited = decision_from_recited_script(rules, result.text)
            if recited is not None:
                _handoff(decision=recited, **handoff_args)
                return

        # 只有模型真的回了答案才加:fallback 前面接「專員不在線上」沒有意義
        if result is not None:
            answer = expired_prefix + answer

        # 簡繁兜底要在寫入資料庫之前 —— 對話紀錄要跟客人實際看到的一致。
        # prompt 已經規定繁體,但那只降低漂移消不掉(真機上出現過「停车位」)。
        answer = ensure_traditional(answer)
        answer = truncate_for_line(answer, settings.line_max_text_length)

        with session_scope() as db:
            db.add(ChatHistory(company_id=company_id, user_id=user_pk,
                               role=ChatRole.ASSISTANT, content=answer,
                               token_count=tokens, latency_ms=latency))

        client.send(reply_token, line_user_id, answer)
    except Exception:
        # 沉默是唯一不被接受的失敗模式
        logger.exception("背景處理爆炸,嘗試送出 fallback")
        _send_last_resort_fallback(company_id, line_user_id, reply_token)


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
    # 給示範現場正面的 log 證據:轉真人這件事本身,以及是規則轉的還是
    # 模型轉的,不必等到查資料庫才知道。
    logger.info("轉真人 category=%s 依據=%s", decision.category, decision.basis)
    if not client.send(reply_token, line_user_id, decision.script):
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
