# B 階段:部署 — 設計文件

日期:2026-09-22
狀態:設計完成,待實作
延續:[2026-09-21-design.md](2026-09-21-design.md)(v1 系統設計)

一句話:**讓這套系統跑在容器裡、資料在 PostgreSQL、對外有一個永遠不變的網址,當掉會自己起來。**

---

## 一、目標與範圍

### 要解決的問題

`docs/report.md` 第八節限制 1:

> **沒有部署。** 服務跑在開發者的筆電上,靠 Quick Tunnel 對外。
> 電腦關了、網路換了、程式當了都沒人重啟。

這一段消掉的是「當了沒人重啟」與「網址每次都變」,**不是**「電腦關了」——
見下面的刻意解耦。

### 做

1. **容器化** —— `Dockerfile` + `compose.yaml`,三個服務(app / db / tailscale)。
2. **真的跑 PostgreSQL** —— 不是「設定上支援」,是實際跑在上面,含既有資料搬遷。
3. **固定網址** —— Tailscale Funnel,`https://<hostname>.<tailnet>.ts.net`,
   填進 LINE Console 一次就不再碰。
4. **自動重啟** —— `restart: unless-stopped`,涵蓋範圍見第五節。
5. **雙資料庫 CI** —— 同一套測試在 SQLite 與 PostgreSQL 各跑一遍。

### 不做

| 項目 | 為什麼延後 |
|---|---|
| **上雲(Oracle Cloud Always Free)** | 見下面的解耦。註冊要信用卡驗證、home region 可能沒容量,是我們控制不了的變數 |
| **速率限制** | C 階段。`/webhook/{slug}` 仍是公開端點,擋得住偽造擋不住灌爆 |
| **「卡住但沒退出」的自動處理** | C 階段,見第五節 |
| **log 落地成檔案** | C 階段。容器化後 log 進 Docker 的 log driver,已比「關掉就沒了」好 |
| **`LineClient` 連線重用** | C 階段,與部署無關 |

### 一個刻意的解耦

**「容器化」與「跑在哪台機器」是兩件事,這份設計刻意不把它們綁在一起。**

compose 起得來的地方就跑得起來。本機跑通之後要搬上任何一台 Linux 機器,
`docker compose up -d` 就結束,設定檔一行不用改。

這樣做的代價是:B 階段結束時服務仍在這台筆電上,**「電腦關了就連不到」還在**。
換來的是免費方案那些我們控制不了的變數(容量、註冊審核、回收政策)卡住時,
容器化、PostgreSQL、固定網址、自動重啟這四項已經全部到手。

---

## 二、決策紀錄

| # | 決策 | 不這樣做會怎樣 |
|---|---|---|
| 1 | **容器化與託管解耦**,託管留到最後一步 | 免費層註冊卡住 → 整個 B 階段停擺,而其實跟容器化無關 |
| 2 | **Tailscale Funnel**,不是 Cloudflare Named Tunnel | Named Tunnel 要自有網域(約 NT$300-400/年)。免費前提下它出局 |
| 3 | **Tailscale 進 compose**,不裝在 Windows 主機 | 主機版簡單十分鐘,但搬上 Linux 時 tunnel 這段要整個重做,解耦就不成立 |
| 4 | **給節點打 tag**(`tag:helpdesk`),不手動關金鑰到期 | 節點金鑰預設 180 天到期。**標記過的節點不適用到期** —— 手動關要記得做,換機器要重做 |
| 5 | `TS_STATE_DIR` **掛 named volume** | 容器一重建就重新註冊,舊節點佔著名字 → 新的變 `helpdesk-1` → **網址變了** |
| 6 | **搬遷既有資料**,不重新 seed | 重新 seed 要再去 LINE Console 複製一次 channel secret / access token |
| 7 | 密文欄位**照搬不解密** | 解密再加密等於讓明文金鑰多在記憶體與 log 裡出現一次,沒有任何好處 |
| 8 | `/health` **加資料庫檢查** | 現在只證明行程活著。加了之後 demo 前 curl 一下就知道整條鏈通不通 |
| 9 | 「卡住但沒退出」**誠實不做** | 補它要多掛 autoheal sidecar 監看 healthcheck,那是 C 階段的事 |
| 10 | **CI 加 PostgreSQL job** | `database.py` 自己的註解就寫了「本機測試全過,上了 PostgreSQL 才發現一堆孤兒資料」 |
| 11 | app 只發布到 `127.0.0.1:8000`,不綁 `0.0.0.0` | 會議室 Wi-Fi 上同網段的人直接打得到 webhook 端點 |
| 12 | `psycopg[binary]` 從 `dev` extra **移到主依賴** | 正式映像裝了主依賴卻連不上 PostgreSQL,第一次連線才炸 |

---

## 三、整體架構

```
                        網際網路
                           │  https://helpdesk-<tailnet>.ts.net
                           ▼
        ┌────────────── compose 網路 ──────────────┐
        │                                          │
   [tailscale] ──funnel──▶ [app] ─────────────▶ [db]
   ts-state vol            FastAPI              postgres:17
   TS_SERVE_CONFIG         alembic 開機自動      pgdata vol
                           127.0.0.1:8000        pg_isready
        └──────────────────────────────────────────┘
              三個服務都 restart: unless-stopped
```

三個服務,沒有第四個。

| 服務 | 映像 | 重點 |
|---|---|---|
| `app` | 本地 build | entrypoint 先 `alembic upgrade head` 再起 uvicorn。只發布到 `127.0.0.1:8000` |
| `db` | `postgres:17-alpine` | `pgdata` named volume;healthcheck 用 `pg_isready` |
| `tailscale` | `tailscale/tailscale` | `TS_SERVE_CONFIG` 宣告式開 Funnel;`ts-state` named volume |

**PostgreSQL 的主版本要釘死,不能寫 `postgres:latest`。** 資料目錄的格式跟主版本綁定 ——
映像哪天跳到下一個主版本,容器會對著既有的 `pgdata` volume 直接啟動失敗,
而且錯誤訊息講的是資料目錄版本不符,不會說「你換了映像」。要升級得跑 `pg_upgrade`,
那是有意識的動作,不該由 `docker compose pull` 順手觸發。

環境變數的分配:**機密走 `.env`,非機密直接寫在 `compose.yaml` 裡。**
`TS_EXTRA_ARGS=--advertise-tags=tag:helpdesk` 和 `TS_STATE_DIR=/var/lib/tailscale`
不是機密,寫進版控反而讓人看得到我們怎麼設定的;只有 `TS_AUTHKEY`、
`POSTGRES_PASSWORD`、`FERNET_KEY`、`OPENROUTER_API_KEY` 走 `.env`。

`app` 用 `depends_on: { db: { condition: service_healthy } }` 等資料庫。
沒有這個,開機第一件事 `alembic upgrade head` 會在 PostgreSQL 還沒 ready 時失敗,
log 塞滿指不到真正原因的錯誤。

### Funnel 設定(`deploy/funnel.json`)

```json
{
  "TCP": { "443": { "HTTPS": true } },
  "Web": {
    "${TS_CERT_DOMAIN}:443": {
      "Handlers": { "/": { "Proxy": "http://app:8000" } }
    }
  },
  "AllowFunnel": { "${TS_CERT_DOMAIN}:443": true }
}
```

`${TS_CERT_DOMAIN}` 由 Tailscale 自己展開成該節點的 FQDN,不用我們填。
用宣告式設定而不是每次重啟後下 `tailscale funnel` 指令 —— 後者總有一天會忘。

---

## 四、固定網址:四個會讓它悄悄失效的地方

網址的組成:

```
https://<TS_HOSTNAME>.<tailnet 名稱>.ts.net
         ↑ 我們設 helpdesk   ↑ 註冊時 Tailscale 給的,例如 tail9a2f.ts.net
```

**四項全部守住,它才是固定的。** 每一項失效的症狀都是「訊息傳出去沒人回,
但 LINE Console 看起來一切正常」。

### 1. 節點狀態沒持久化 → 網址直接變

`TS_STATE_DIR=/var/lib/tailscale` 必須掛 named volume(決策 5)。

### 2. 節點金鑰 180 天到期 → 半年後某天突然斷

Tailscale 節點金鑰預設 180 天到期,過期節點離線,Funnel 跟著死。
**這是最惡劣的一種壞法:在你完全沒改任何東西的某一天發生。**

解法是給節點打 tag(決策 4),`TS_EXTRA_ARGS=--advertise-tags=tag:helpdesk`。
標記過的節點不適用金鑰到期。

### 3. Funnel 沒在 tailnet 政策裡開 → 靜默不生效

Funnel 需要政策檔有 `funnel` node attribute。用 CLI 開 Funnel 時 Tailscale 會自動加,
但**我們走 `TS_SERVE_CONFIG` 宣告式設定,不經過 CLI 互動流程,所以要自己加**:

```json
"tagOwners": { "tag:helpdesk": ["autogroup:admin"] },
"nodeAttrs": [{ "target": ["tag:helpdesk"], "attr": ["funnel"] }]
```

### 4. tailnet 改名 → 網址跟著變

tailnet 名字是網址的一部分。**註冊完先決定好名字,再去填 LINE Console。**

### 不用擔心的:`TS_AUTHKEY` 過期

auth key 最長 90 天,但節點註冊完就不再需要它(身分在 volume 裡)。
它過期**不影響**已經在跑的服務,只有哪天砍掉 volume 要重建時才要再產一把。

寫在這裡是因為「金鑰有效期 90 天」很容易被誤解成「要定期換,不然會斷」。

---

## 五、自動重啟:它涵蓋什麼、不涵蓋什麼

### 先講一個很多人搞錯的地方

**Docker 的 healthcheck 不會重啟容器。** 它只影響顯示的狀態和 `depends_on` 的等待條件。
(會依 healthcheck 重啟的是 Swarm,不是單機 compose。)

自動重啟實際靠的是 `restart: unless-stopped`,而它只在容器**退出**時作用。

| 壞法 | 會不會自動救回來 |
|---|---|
| 程式未捕捉例外炸掉、OOM 被殺 | 會 |
| Windows 重開機(Docker Desktop 設成開機啟動) | 會 |
| PostgreSQL 先死 | 會。app 連不上會退出 → 重啟 → `depends_on` 等 db healthy |
| **卡住但沒退出**(例如連線池耗盡) | **不會** |

最後一項**刻意留著不做**(決策 9)。文件寫出這個缺口,比假裝沒有好。

### 那 healthcheck 還做嗎

做,但目的跟自動重啟無關:

- `db` 的 `pg_isready` 讓 `app` 的 `depends_on` 有東西可以等
- `app` 的 `/health` 加資料庫檢查,讓 **demo 前 `curl` 一下就知道整條鏈通不通**,
  而不是只知道行程沒死

回傳照舊只有 `ok` / `fail`,**不吐連線字串或任何設定值** —— 那個端點是公開的。

### Windows 特有的一步

`restart: unless-stopped` 要在重開機後生效,**Docker Desktop 本身必須設成開機啟動**。
不做這步,整個自動重啟等於沒有,而且平常測試完全看不出來。

---

## 六、資料搬遷

`helpdesk.db`(SQLite)→ PostgreSQL,一次性腳本 `scripts/migrate_sqlite_to_postgres.py`。

### 執行順序不能顛倒

1. `docker compose up -d` —— app 容器的 entrypoint 跑 `alembic upgrade head`,**在 PostgreSQL 裡建出空的表**
2. **確認 app 起來了**(`/health` 回 ok)
3. 才跑搬遷腳本

**腳本不自己建表。** 建表是 Alembic 的職責,讓搬遷腳本也能建,就等於同一件事有兩個
真相來源 —— 兩邊有一天會不一致,而且會在「搬完之後某個欄位莫名其妙不存在」的時候才發現。
腳本開頭要檢查目標的表在不在,不在就直接報錯並告訴人先做第 1 步。

### sequence 要修 —— 而且這一節原本寫錯了

> **2026-09-23 更正。** 本節原本寫「好消息:沒有 sequence 要修」,理由是主鍵都是
> `String(36)` 存 UUID(v1 決策 8)。**那是錯的,而且錯得很貴。**
>
> 四張表裡有三張是 UUID,但 **`chat_histories.id` 是
> `Integer, primary_key=True, autoincrement=True`**(見 `app/models/chat.py`)——
> 訊息是不可變的流水紀錄,用自增整數是刻意的設計,不是疏漏。
>
> 我寫這一節時只看了 `company.py`,看到 `String(36)` 就推論四張表都一樣,
> 從來沒打開 `chat_histories.py`。這段錯誤的文字後來被複製進實作計畫、
> 再被複製進 commit 訊息 —— **一個沒驗證的前提會一路傳播下去。**

所以 **SQLite → PostgreSQL 最經典的坑在這個專案是存在的**,只是只存在於一張表:

自增整數搬過去之後,PG 的序號還停在 1(`last_value=1, is_called=f`),
資料看起來全都在,**一寫新資料就 `duplicate key value violates unique constraint`**。
對這個系統而言,「一寫新資料」就是下一則進來的 LINE 訊息。

**對策:搬完之後把每一張真的有序號的表的序號推到 `MAX(id)`。**

```sql
SELECT setval(
    pg_get_serial_sequence('<表名>', 'id'),
    COALESCE((SELECT MAX(id) FROM <表名>), 1),
    (SELECT COUNT(*) FROM <表名>) > 0
);
```

三個刻意的細節:

1. **用 `pg_get_serial_sequence` 動態查,不要寫死「只有 chat_histories 需要」。**
   寫死等於把「我檢查過每一張表」這個假設再編碼一次 —— 而那正是本節原本犯的錯。
   UUID 主鍵的表會回 NULL,跳過即可。
2. **第三個參數 `is_called`。** 表是空的時候傳 `false`,否則序號會從 2 開始,平白跳過 1。
3. **只在 PostgreSQL 上執行。** `pg_get_serial_sequence` 在 SQLite 不存在,
   而測試是 SQLite → SQLite 跑的。

### 為什麼兩層測試都沒擋住這個

值得記下來,因為這是「測試全綠但東西是壞的」的標準形狀:

- **自動測試搬 SQLite → SQLite**,而 SQLite 根本沒有 PostgreSQL 那種 sequence 物件,
  所以這個 bug 在那裡不可能重現
- **手動驗收只查了 slug、token 解得開、時間戳沒位移**,**從來沒有「搬完之後插入一筆新資料」**

漏的不是某個斷言,是**整整一類操作**:驗證只做了「讀」,沒有做「寫」。

### 真正的坑:時間會整批位移,而且沒有錯誤訊息

`TimestampMixin` 用 `DateTime(timezone=True)`。SQLite 不真的保存時區,
讀出來是 naive datetime;直接塞進 PostgreSQL 的 `timestamptz`,
**PG 會拿伺服器時區去解讀它** —— 整批時間默默位移,容器裡是 UTC、
筆電是 UTC+8,差八小時。

**對策:讀出來明確標記成 UTC 再寫入,並且要有回歸測試。**

### 其餘設計

| | 做法 |
|---|---|
| 密文欄位 | 照搬不解密(`LargeBinary` → `BYTEA`)。`FERNET_KEY` 不變就解得開 |
| 讀寫方式 | 兩邊都用 SQLAlchemy Core + **同一份 metadata**,不寫原生 SQL —— `JSON`、`Boolean`、`LargeBinary` 的型別轉換交給 SQLAlchemy |
| 順序 | companies → users → chat_histories → knowledge_documents(外鍵) |
| 安全閥 | 目標非空就**拒絕**,要 `--force` 才覆寫;`--dry-run` 先印出每張表幾列 |

---

## 七、機密管理

### `.dockerignore` 必須擋掉 `.env`

`COPY . .` 會把它烤進映像層。**之後在 Dockerfile 裡刪掉也沒用 —— 它還在前一層的
歷史裡**,誰 `docker history` 都挖得出來。

同時要擋 `.venv/`、`*.db`、`.git/`。

### 其餘

- `compose.yaml` 進版控,裡面**只有 `${VAR}` 參照,沒有任何值**
- `.env` 新增 `POSTGRES_PASSWORD`、`TS_AUTHKEY`(只有機密;`TS_HOSTNAME`、
  `TS_EXTRA_ARGS`、`TS_STATE_DIR` 直接寫在 `compose.yaml`,見第三節)
- **`FERNET_KEY` 的重要性在這次之後升級了。** 以前 `helpdesk.db` 躺在桌面,
  隨手就能備份;搬進 Docker volume 之後比較難碰。金鑰弄丟 = 所有憑證解不開 =
  要回 LINE Console 全部重拿。README 要寫一行提醒作者自己保管

---

## 八、檔案清單

### 新增

| 檔案 | 做什麼 |
|---|---|
| `Dockerfile` | `python:3.13-slim`,`pip install .` |
| `docker-entrypoint.sh` | 先 `alembic upgrade head` 再 `exec uvicorn`。獨立成檔而不是塞進 `CMD`,是為了讓那兩步各自印一行看得懂的標題 —— 容器起不來時,光看 log 停在哪一行就知道是 migration 卡住還是服務卡住 |
| `compose.yaml` | 三個服務 |
| `.dockerignore` | 見第七節 |
| `deploy/funnel.json` | Tailscale serve config |
| `scripts/migrate_sqlite_to_postgres.py` | 一次性搬遷 |

### 修改

| 檔案 | 改什麼 |
|---|---|
| `pyproject.toml` | `psycopg[binary]` 從 `dev` extra 移到主依賴 |
| `.env.example` | 加 `POSTGRES_PASSWORD`、`TS_AUTHKEY`;`DATABASE_URL` 的正式範例改成指向 `db` 服務 |
| `app/main.py` | `/health` 加資料庫檢查 |
| `app/cli.py` | 加 `set-webhook` 指令(把 `scripts/dev.py` 的 `_register_webhook()` 搬進來) |
| `.github/workflows/ci.yml` | 加 PostgreSQL service container 的 job |
| `README.md` | 「怎麼跑起來」改成 compose;加 `FERNET_KEY` 保管提醒 |
| `docs/demo-checklist.md` | 拿掉「抄網址」「按 Verify」那幾條 |
| `docs/report.md` | 第八節限制 1 改寫;第九節下一步更新 |

`scripts/dev.py` **保留不動** —— 那是筆電開發流程,跟容器化是兩條並行的路。

---

## 九、測試策略

### 既有 142 個測試不受影響

`conftest.py` 用記憶體 SQLite,跟這次改動沒有交集。

### 新增:PostgreSQL 的 CI job

理由寫在 `app/database.py` 自己的註解裡:

> SQLite 預設不檢查外鍵。不開這個,ForeignKey 宣告形同虛設 ——
> **本機測試全過,上了 PostgreSQL 才發現一堆孤兒資料。**

那句話當初是預防性的,現在真的要上 PostgreSQL 了。GitHub Actions 的
service container 免費,而 `conftest.py` 已經是 `os.environ.setdefault("DATABASE_URL", ...)`
—— CI 設一個環境變數就能讓同一套測試在 PG 上再跑一遍。

**測試碼預期幾乎不用改,但這是預期不是保證**,實作時要實際驗
(`tmp_engine` fixture 的 `drop_all`、記憶體 SQLite 專用的 `StaticPool` 分支)。

### 新增的測試

| 測什麼 | 重點 |
|---|---|
| 搬遷腳本 | **含時間位移那條的回歸測試** |
| `/health` 含資料庫 | 通了回 ok、斷了回 fail,且不吐設定值 |
| `set-webhook` CLI | httpx 照既有做法 mock 掉 |

### 沒有單元測試的部分

`Dockerfile` 與 `compose.yaml` 只能靠實際起起來驗。第十節那份清單就是它們的測試。

---

## 十、驗收標準

| # | 驗什麼 | 怎麼算過 |
|---|---|---|
| 1 | 三個容器起得來 | `docker compose up -d`,`db` 顯示 healthy |
| 2 | 內部通 | `curl http://127.0.0.1:8000/health` → `ok`(含資料庫) |
| 3 | 外部通 | `curl https://<fqdn>/health` 從外網打得到 |
| 4 | 搬遷正確 | PG 裡看得到那家公司,**access token 解得開**,時間戳沒位移 |
| 5 | 整條鏈通 | 手機傳訊息 → 收到該行業的答案 |
| 6 | **網址真的固定** | `docker compose down && docker compose up -d` → **網址不變** |
| 7 | 當掉會回來 | `docker kill` app 容器 → 自己起來 |
| 8 | **重開機會回來** | Windows 重開機 → 服務自己回來,**而且網址不變** |
| 9 | 換行業照舊 | `docker compose exec app python -m app.cli seed --industry clinic ...` |
| 10 | 測試全綠 | pytest 在 SQLite 與 PostgreSQL 兩組都過 |

**第 6 與第 8 是這整段的核心,其他都是手段。** 網址會變的話,前面所有工作等於沒做。

---

## 十一、一次性手動步驟(要人做,不是程式做)

1. 註冊 Tailscale 免費帳號(Google / GitHub 登入,不用信用卡)
2. tailnet 政策檔加上第四節第 3 點那兩段 JSON
3. 產一把帶 `tag:helpdesk` 的 auth key,貼進 `.env`
4. Docker Desktop 設成開機啟動
5. 服務起來之後,把固定網址填進 LINE Console 一次(或跑 `app.cli set-webhook`)

---

## 十二、開放問題

| # | 問題 | 現在怎麼處理 |
|---|---|---|
| 1 | **userspace 模式能不能跑 Funnel** | 官方文件說 userspace「works everywhere」、只有 kernel 模式需要 `NET_ADMIN` + `/dev/net/tun`,但**沒有白紙黑字寫 userspace 支援 Funnel**。實作時先試零特權版,不通才加回那兩項。計畫裡兩條路都要寫 |
| 2 | **要不要上 Oracle Cloud Always Free** | 暫不排進 B 階段(見第一節解耦)。要做的話是獨立一段,compose 檔不用改。注意 2026-06-15 起 Always Free 的 Ampere A1 從 4 OCPU/24GB 降為 2 OCPU/12GB —— 對這個服務仍綽綽有餘 |
| 3 | **`conftest.py` 在 PostgreSQL 上要不要改** | 預期不用,實作時驗(見第九節) |

---

## 參考

- [ngrok 免費方案限制](https://ngrok.com/docs/pricing-limits/free-plan-limits) —— 評估過,20,000 requests/月、1GB/月的硬上限,且同樣離不開筆電
- [Tailscale Funnel](https://tailscale.com/docs/features/tailscale-funnel)
- [Tailscale 金鑰到期](https://tailscale.com/docs/features/access-control/key-expiry)
- [標記過的節點不需金鑰續期](https://tailscale.com/blog/tagged-key-expiry)
- [Tailscale Docker 參數](https://tailscale.com/docs/features/containers/docker/docker-params)
- [Tailscale × Docker 指南](https://tailscale.com/blog/docker-tailscale-guide)
