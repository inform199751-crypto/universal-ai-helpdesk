"""FastAPI 進入點。"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Response
from sqlalchemy import text

from app.database import engine
from app.routers import webhook

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s | %(message)s")

logger = logging.getLogger(__name__)

app = FastAPI(title="AI 智慧客服(萬用)")
app.include_router(webhook.router)


def _database_ok() -> bool:
    """真的去問資料庫一句話,不是只看設定有沒有填。

    分成獨立函式是為了測試能換掉它 —— 要驗「資料庫斷了會怎樣」,
    總不能真的去把資料庫關掉。
    """
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return True


@app.get("/health")
def health(response: Response):
    # 不回傳任何設定值 —— 這個端點是公開的。例外訊息尤其不能往外送:
    # SQLAlchemy 的連線錯誤常常把整串連線字串印出來,而那裡面有密碼。
    try:
        _database_ok()
        database = "ok"
    except Exception:
        logger.exception("健康檢查:資料庫連不上")
        database = "fail"

    if database != "ok":
        response.status_code = 503

    return {
        "status": "ok" if database == "ok" else "degraded",
        "version": "0.1.0",
        "database": database,
    }
