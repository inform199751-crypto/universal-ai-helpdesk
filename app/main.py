"""FastAPI 進入點。"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from app.routers import webhook

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s | %(message)s")

app = FastAPI(title="AI 智慧客服(萬用)")
app.include_router(webhook.router)


@app.get("/health")
def health():
    # 不回傳任何設定值 —— 這個端點是公開的
    return {"status": "ok", "version": "0.1.0"}
