#!/bin/sh
set -e

# 建表與改表一律走 Alembic。只有一個 app 容器,不會有兩個同時跑 migration。
echo "== alembic upgrade head =="
alembic upgrade head

# 容器內部必須聽 0.0.0.0,否則 compose 的 port publish 轉不進來。
# 「只綁 127.0.0.1」是 compose 那一層的事(見 Global Constraint 4),
# 不是這一層。
echo "== uvicorn =="
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
