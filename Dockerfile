# syntax=docker/dockerfile:1

FROM python:3.13-slim

# psycopg[binary] 自帶 libpq,不必裝 postgresql-client。
# curl 留著是因為 compose 的 healthcheck 要用它打 /health。
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先只複製宣告相依的東西再裝。原始碼改動不會讓 pip 那一層失效,
# 重 build 從幾分鐘變幾秒。
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

# 執行期才需要、但不是 Python 套件的一部分
# (pyproject 的 include 只有 app*,理由見那個檔案的註解)
COPY alembic ./alembic
COPY alembic.ini ./
COPY industries ./industries
COPY prompts ./prompts
COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

# 不緩衝輸出。緩衝的話容器 log 會一片空白直到緩衝區滿,
# 而「看不到 log」在排查時跟「服務沒起來」長得一模一樣。
ENV PYTHONUNBUFFERED=1

EXPOSE 8000
ENTRYPOINT ["./docker-entrypoint.sh"]
