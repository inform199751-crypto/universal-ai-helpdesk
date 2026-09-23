"""把 SQLite 的資料搬進 PostgreSQL。一次性,跑完就不再用。

    python scripts/migrate_sqlite_to_postgres.py --dry-run
    python scripts/migrate_sqlite_to_postgres.py

前置:目標資料庫的表必須已經由 Alembic 建好 —— 先 `docker compose up -d`,
app 容器的 entrypoint 會跑 `alembic upgrade head`。

這支腳本不建表。建表是 Alembic 的職責,讓第二個地方也能建就是同一件事
有兩個真相來源,兩邊總有一天會不一致,而且會在「搬完之後某個欄位莫名
不存在」的時候才發現。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, func, inspect, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import Base  # noqa: E402
import app.models  # noqa: E402,F401  ← 不 import 的話 metadata 是空的

# 依外鍵順序。companies 一定要先(users 與 knowledge_documents 指向它),
# chat_histories 又指向 users。反過來搬會撞外鍵。
TABLE_ORDER = ("companies", "users", "chat_histories", "knowledge_documents")


def as_utc(value):
    """把 naive datetime 明確標記成 UTC。

    SQLite 不真的保存時區,讀出來是 naive 的。直接塞進 PostgreSQL 的
    timestamptz,PG 會拿「伺服器時區」去解讀 —— 容器裡是 UTC、筆電是
    UTC+8,整批時間默默差八小時,而且不會有任何錯誤訊息。

    標成 UTC 是對的、不是猜的:v1 決策 8 已經確立「時間一律存 UTC」,
    而 SQLite 的 CURRENT_TIMESTAMP 定義本來就是 UTC。
    """
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def copy_all(source_url: str, target_url: str, *,
             dry_run: bool = False, force: bool = False) -> dict[str, int]:
    """把四張表從來源搬到目標,回傳每張表搬了幾列。"""
    src = create_engine(source_url, future=True)
    dst = create_engine(target_url, future=True)

    try:
        inspector = inspect(dst)
        missing = [t for t in TABLE_ORDER if not inspector.has_table(t)]
        if missing:
            raise SystemExit(
                f"目標資料庫缺少這些表:{'、'.join(missing)}。\n"
                "這支腳本不建表。先跑 `docker compose up -d`,app 容器開機時的 "
                "`alembic upgrade head` 會把表建出來,再跑這支。")

        counts: dict[str, int] = {}
        with src.connect() as s, dst.begin() as d:
            # 先全部檢查完再寫任何一筆。寫到一半才發現第三張表不能覆寫,
            # 留下來的是「搬了一半」的資料庫,比完全沒搬更難處理。
            for name in TABLE_ORDER:
                table = Base.metadata.tables[name]
                existing = d.execute(
                    select(func.count()).select_from(table)).scalar_one()
                if existing and not force:
                    raise SystemExit(
                        f"目標的 {name} 已經有 {existing} 列,拒絕覆寫。"
                        "確定要清掉重搬請加 --force。")

            if force and not dry_run:
                # 反序刪除,不然會撞外鍵
                for name in reversed(TABLE_ORDER):
                    d.execute(Base.metadata.tables[name].delete())

            for name in TABLE_ORDER:
                table = Base.metadata.tables[name]
                rows = [dict(r._mapping) for r in s.execute(select(table))]
                counts[name] = len(rows)
                if dry_run or not rows:
                    continue
                d.execute(table.insert(), [
                    {k: as_utc(v) for k, v in row.items()} for row in rows
                ])
        return counts
    finally:
        src.dispose()
        dst.dispose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="migrate_sqlite_to_postgres")
    parser.add_argument("--source", default="sqlite:///./helpdesk.db")
    parser.add_argument(
        # 範例刻意寫 127.0.0.1 而不是 localhost。Windows 的 localhost 會先
        # 解析成 IPv6 的 ::1,但 Docker 發布的埠只綁 IPv4 —— 那次嘗試要等
        # 到逾時才退回,實測連線要多花兩分鐘,而且畫面上什麼都沒有。
        # 看起來像資料庫沒起來,其實只是位址挑錯。
        "--target", required=True,
        help="例如 postgresql+psycopg://helpdesk:<密碼>@127.0.0.1:15432/helpdesk")
    parser.add_argument("--dry-run", action="store_true",
                        help="只印出每張表幾列,不寫入任何東西。")
    parser.add_argument("--force", action="store_true",
                        help="目標已有資料時,先清空再搬。")
    args = parser.parse_args(argv)

    counts = copy_all(args.source, args.target,
                      dry_run=args.dry_run, force=args.force)

    label = "(dry-run,沒有寫入)" if args.dry_run else ""
    print(f"搬遷結果{label}:")
    for name in TABLE_ORDER:
        print(f"  {name:22} {counts[name]:>5} 列")
    return 0


if __name__ == "__main__":
    sys.exit(main())
