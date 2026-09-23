# syntax=docker/dockerfile:1

FROM python:3.13-slim

# psycopg[binary] 自帶 libpq,不必裝 postgresql-client。
# curl 留著是因為 compose 的 healthcheck 要用它打 /health。
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# pyproject 的 [tool.setuptools.packages.find] 只收 app*,所以 pip install .
# 必須在 app/ 已經存在時才找得到套件 —— 這個順序改不了,而代價是改任何一行
# app/ 的原始碼都會讓下面這層失效,pip 跟著重跑。
#
# 所以不關 pip 的快取,改用 BuildKit 的 cache mount:重跑時直接吃本機已經
# 下載過的 wheel,不必再連網抓 fastapi / sqlalchemy / cryptography 那一整串。
# (原本寫 --no-cache-dir 是反效果 —— 它關掉的正是這裡唯一能省時間的東西。
#  cache mount 不會留在映像層裡,所以映像不會因此變大。)
COPY pyproject.toml ./
COPY app ./app
RUN --mount=type=cache,target=/root/.cache/pip pip install .

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
