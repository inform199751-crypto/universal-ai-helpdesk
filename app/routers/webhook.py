"""LINE webhook 入口。

流程刻意分成「同步的驗證」與「背景的處理」兩段:
LINE 沒收到 2xx 會重送,而 LLM 要跑 3-15 秒,同步等一定超時 ——
客人會收到兩次一樣的答案。
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.agent.llm import LLMError, complete
from app.agent.prompt import build_messages
from app.agent.zh import ensure_traditional
from app.config import get_settings
from app.crypto import decrypt
from app.database import session_scope
from app.line.client import LineClient, truncate_for_line
from app.line.signature import verify_signature
from app.models import ChatHistory, ChatRole, Company, User

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

            if message_type != "text":
                # 罐頭回覆也記下來,短期記憶才連得起來:客人傳了圖、我們說看不懂,
                # 下一句「那這個多少錢」模型才知道剛才發生過什麼。
                db.add(ChatHistory(company_id=company_id, user_id=user.id,
                                   role=ChatRole.ASSISTANT,
                                   content=NON_TEXT_REPLY))
                client.send(reply_token, line_user_id, NON_TEXT_REPLY)
                return

            user_pk = user.id
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
        try:
            result = complete(build_messages(system_prompt, history, text))
            answer, tokens, latency = result.text, result.token_count, result.latency_ms
        except LLMError as exc:
            logger.warning("LLM 失敗,改送 fallback:%s", exc)
            answer, tokens, latency = fallback, None, None

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
