# B 階段:部署 — 設計文件

日期:2026-09-22
狀態:已實作完成,十項驗收標準(第十節)全部通過。內容描述最後落地的
樣子(app / db / ngrok);過程中從 Tailscale Funnel 換成 ngrok 的判斷
記錄在第二節決策 2 與第四節,沒有刪掉重寫。
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

1. **容器化** —— `Dockerfile` + `compose.yaml`,三個服務(app / db / ngrok)。
2. **真的跑 PostgreSQL** —— 不是「設定上支援」,是實際跑在上面,含既有資料搬遷。
3. **固定網址** —— ngrok 固定網域,`https://unsaid-expend-eagle.ngrok-free.dev`,
   填進 LINE Console 一次就不再碰。**原本選的是 Tailscale Funnel,實作後換成
   ngrok —— 判斷過程見決策 2 與第四節,不是隨手換的。**
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
| 2 | **原本選 Tailscale Funnel,實作後改用 ngrok** | Funnel 的固定網址本身完全成立(驗收標準 6 —— `down`/`up` 網址不變 —— 是用它通過的),但**LINE 的 TLS 客戶端跟它談不攏**,而 LINE 是這個系統唯一重要的客戶端。換成 ngrok 之後用 LINE 官方測試端點驗證才真的通,驗收標準 8(重開機網址不變)也是換成 ngrok 之後才驗過的。詳細除錯軌跡見 [docs/report.md 第七節](../report.md) |
| 6 | **搬遷既有資料**,不重新 seed | 重新 seed 要再去 LINE Console 複製一次 channel secret / access token |
| 7 | 密文欄位**照搬不解密** | 解密再加密等於讓明文金鑰多在記憶體與 log 裡出現一次,沒有任何好處 |
| 8 | `/health` **加資料庫檢查** | 現在只證明行程活著。加了之後 demo 前 curl 一下就知道整條鏈通不通 |
| 9 | 「卡住但沒退出」**誠實不做** | 補它要多掛 autoheal sidecar 監看 healthcheck,那是 C 階段的事 |
| 10 | **CI 加 PostgreSQL job** | `database.py` 自己的註解就寫了「本機測試全過,上了 PostgreSQL 才發現一堆孤兒資料」 |
| 11 | app 只發布到 `127.0.0.1:8000`,不綁 `0.0.0.0` | 會議室 Wi-Fi 上同網段的人直接打得到 webhook 端點 |
| 12 | `psycopg[binary]` 從 `dev` extra **移到主依賴** | 正式映像裝了主依賴卻連不上 PostgreSQL,第一次連線才炸 |

> 編號 3-5 併入決策 2 了,不是刪掉:原本是「Tailscale 進 compose、給節點
> 打 tag、state volume 掛 named volume」三條 Tailscale 節點身分特有的
> 決策,細節見第四節。**編號留著缺口(2 之後跳到 6),沒有把 6-12 往前
> 移**——第五節提到的「決策 9」指的就是這份表,重排編號會讓那個引用
> 跟著錯位。

---

## 三、整體架構

```
                        網際網路
                           │  https://unsaid-expend-eagle.ngrok-free.dev
                           ▼
        ┌────────────── compose 網路 ──────────────┐
        │                                          │
      [ngrok] ────http────▶ [app] ────────────▶ [db]
    (無 state volume,        FastAPI              postgres:17
     固定網域綁帳號)          alembic 開機自動      pgdata vol
                             127.0.0.1:8000        pg_isready
        └──────────────────────────────────────────┘
              三個服務都 restart: unless-stopped
```

三個服務,沒有第四個。

| 服務 | 映像 | 重點 |
|---|---|---|
| `app` | 本地 build | entrypoint 先 `alembic upgrade head` 再起 uvicorn。只發布到 `127.0.0.1:8000` |
| `db` | `postgres:17-alpine` | `pgdata` named volume;healthcheck 用 `pg_isready` |
| `ngrok` | `ngrok/ngrok` | `command` 帶 `--url=<固定網域>` 指定域名;不需要 state volume ——固定網域綁在帳號上,不是節點身分,細節與 Tailscale 的取捨見第四節 |

**PostgreSQL 的主版本要釘死,不能寫 `postgres:latest`。** 資料目錄的格式跟主版本綁定 ——
映像哪天跳到下一個主版本,容器會對著既有的 `pgdata` volume 直接啟動失敗,
而且錯誤訊息講的是資料目錄版本不符,不會說「你換了映像」。要升級得跑 `pg_upgrade`,
那是有意識的動作,不該由 `docker compose pull` 順手觸發。

環境變數的分配:**機密走 `.env`,非機密直接寫在 `compose.yaml` 裡。**
ngrok 的固定網域本身(`unsaid-expend-eagle.ngrok-free.dev`)不是機密,
直接寫在 `command` 裡;只有 `NGROK_AUTHTOKEN`、`POSTGRES_PASSWORD`、
`FERNET_KEY`、`OPENROUTER_API_KEY` 走 `.env`。

`app` 用 `depends_on: { db: { condition: service_healthy } }` 等資料庫。
沒有這個,開機第一件事 `alembic upgrade head` 會在 PostgreSQL 還沒 ready 時失敗,
log 塞滿指不到真正原因的錯誤。

---

## 四、固定網址:走過 Tailscale,最後落在 ngrok

> **2026-09-24 更正。** 本節原本整節都是 Tailscale Funnel 的坑 —— 節點
> 狀態沒持久化網址會變、節點金鑰 180 天到期、政策檔與 tailnet HTTPS 憑證
> 兩個開關都要開才生效、tailnet 改名網址跟著變。**這四個坑當時全部是
> 真的,而且都靠實作驗證過**,但它們描述的是一個後來被拆掉的架構。
>
> **Task 6 的驗收標準全部通過了,系統卻是壞的。** 網址固定(驗收 6)、
> 從外網打得到,兩件事都是真的通過 —— 但打的人是 curl,不是 LINE。
> LINE 的 TLS 客戶端跟 Funnel 談判到一半就自己斷線(`docker compose
> logs tailscale` 是 `TLS handshake error ... EOF`),而**驗收清單上
> 沒有一項是拿 LINE 本人去測的**。換成 ngrok 之後,用 LINE 官方
> webhook 測試端點(`/v2/bot/channel/webhook/test`)驗證才真的通
> (`success: true`),完整除錯過程見
> [docs/report.md 第七節](../report.md)。
>
> **教訓,而且要避免再犯:驗收標準要指名真正的客戶端,不能用「從外網
> 打得到」這種代理指標。** curl 能代表網路層通不通,不能代表 LINE 連
> 不連得上,這兩件事在當時的驗收清單裡被默認當成同一件事。這個系統
> 只有一個客戶端重要,下一次任何「對外是否真的通」的驗收,第一項就該
> 是問那個客戶端本人,不是除錯到山窮水盡才想到。

### 現在:ngrok 固定網域

固定網址是 `https://unsaid-expend-eagle.ngrok-free.dev`,填進 LINE
Console 一次就不再碰。**下面 Tailscale 那四個坑,對 ngrok 全部不
適用,理由是同一個:ngrok 的固定網域綁在帳號上,不是綁在節點身分
上。**

- 「節點狀態沒持久化,容器一重建就換名字」—— 不存在。沒有 state
  volume,固定網域也不會因為容器重建而變。
- 「節點金鑰 180 天到期」—— 不存在。沒有節點身分這個概念,自然沒有
  節點金鑰。
- 「政策檔 + tailnet HTTPS 憑證,兩個開關都要開才生效」—— 不存在。
  固定網域在 ngrok dashboard 保留一次就完成,沒有第二個容易漏掉的
  開關。
- 「tailnet 改名網址跟著變」—— 不存在。網域是使用者自己選的字串,
  不會因為帳號設定變動而改變。

ngrok 這邊真正該注意的,是完全不同的兩件事:

### 1. 免費層有硬上限

**20,000 requests/月、1GB/月。** demo 與個人使用碰不到,但這是真的
限制,不是「還沒撞到所以當作沒有」——超過會斷線,而且錯誤不會指向
「額度用完」這個真正原因(見
[ngrok 免費方案限制](https://ngrok.com/docs/pricing-limits/free-plan-limits))。

### 2. `--log` 預設是字面上的 `"false"`,容器完全靜默

ngrok agent 的 `--log` 預設值是 `"false"`(完全不輸出),不是常見 CLI
那種「預設印到 stderr」。第一次把 `ngrok` 服務起起來時,
`docker compose logs ngrok` 是完全空的,連「有沒有連上、綁到哪個網域」
都看不出來——而這正是除錯時要看的第一個地方(比照上面 Tailscale 那次
靠 log 抓到 `TLS handshake error` 的做法)。

**對策:`command` 裡加 `--log=stdout`。**

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
| **`docker stop` / `docker kill`(人手動停掉)** | **不會 —— 這是 `unless-stopped` 的定義,不是缺陷** |

「卡住但沒退出」**刻意留著不做**(決策 9)。「人手動停掉不會自己回來」
不是同一類缺口——那是 `unless-stopped` 本來的定義,不是留著沒做的事,
說明見下一小節。文件把兩者都寫出來,比只寫看起來像 bug 的那一個更誠實。

### 為什麼是 `unless-stopped`,不是 `always`

`docker stop` 與 `docker kill` 都會讓容器進入「人手動停過」的狀態。容器
還活著的當下,`always` 跟 `unless-stopped` 在這件事上**行為一樣**——都
不會因為你剛剛手動停過就自動把它拉回來。兩者真正的差別只在**下一次
Docker daemon 重啟**的那一刻:`always` 不管你手動停過沒有,daemon 一
重啟就把它拉回來;`unless-stopped` 記得你停過,daemon 重啟後仍然讓它
躺著。

這裡選 `unless-stopped`,是因為「人叫它停」應該被尊重:demo 前想暫停
服務、或除錯時想讓容器維持在「死掉」的狀態方便檢查,不該跟一個會自己
復活的容器搏鬥。真正在意的失敗模式——程式未捕捉例外炸掉、OOM 被殺、
行程收到終止信號後自己結束——走的是「容器自己退出」這條路,不是「人
手動叫它停」,兩個 policy 在這條路上的行為完全一樣:都會自動重啟。

實測(Task 8):對 `app` 容器執行 `docker kill`,`RestartCount` 兩分鐘
內維持不動;改成讓容器內的 uvicorn 收到 SIGTERM 後自己結束
(`docker compose exec app sh -c "kill 1"`,模擬程式收到終止信號而不是
被外部強制停止),`RestartCount` 立刻從 0 變成 1,`FinishedAt` 到新的
`StartedAt` 差不到一秒,幾秒內 `docker compose ps` 就顯示 `healthy`。

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
| `scripts/migrate_sqlite_to_postgres.py` | 一次性搬遷 |

`deploy/funnel.json`(Tailscale serve config)原本也在這份清單裡,拆掉
Tailscale 時一併刪除了 —— ngrok 不需要對應的設定檔,`--url` 那個旗標
直接寫在 `compose.yaml` 的 `command` 裡就夠。

### 修改

| 檔案 | 改什麼 |
|---|---|
| `pyproject.toml` | `psycopg[binary]` 從 `dev` extra 移到主依賴 |
| `.env.example` | 加 `POSTGRES_PASSWORD`、`NGROK_AUTHTOKEN`(原本是 `TS_AUTHKEY`,拆 Tailscale 時換掉);`DATABASE_URL` 的正式範例改成指向 `db` 服務 |
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
| 3 | 外部通 | `curl https://<fqdn>/health` 從外網打得到 —— **這一項對 Tailscale Funnel 也通過,但它驗的是 curl 通不通,不是 LINE 通不通。真正決定性的是第 5 項與第四節開頭那段教訓** |
| 4 | 搬遷正確 | PG 裡看得到那家公司,**access token 解得開**,時間戳沒位移 |
| 5 | 整條鏈通 | 手機傳訊息 → 收到該行業的答案 |
| 6 | **網址真的固定** | `docker compose down && docker compose up -d` → **網址不變** |
| 7 | 當掉會回來 | 讓容器內的行程非正常結束(例如 `docker compose exec app sh -c "kill 1"`),容器自己重啟 —— **不是 `docker kill`,那算人手動停止,`unless-stopped` 定義上不會回來,見第五節** |
| 8 | **重開機會回來** | Windows 重開機 → 服務自己回來,**而且網址不變** |
| 9 | 換行業照舊 | `docker compose exec app python -m app.cli seed --industry clinic ...` |
| 10 | 測試全綠 | pytest 在 SQLite 與 PostgreSQL 兩組都過 |

**第 6 與第 8 是這整段的核心,其他都是手段。** 網址會變的話,前面所有工作等於沒做。

---

## 十一、一次性手動步驟(要人做,不是程式做)

1. 註冊 ngrok 免費帳號,到
   [dashboard.ngrok.com/get-started/your-authtoken](https://dashboard.ngrok.com/get-started/your-authtoken)
   拿 authtoken,貼進 `.env` 的 `NGROK_AUTHTOKEN`
2. Dashboard **Domains** 頁保留一個固定網域,寫進 `compose.yaml` 的
   `ngrok` 服務 `command`(`--url=https://<你的網域>`)。**這一步只做
   一次**——網域綁在帳號上,不會因為容器重建或重開機而變
3. Docker Desktop 設成開機啟動
4. 服務起來之後,把固定網址填進 LINE Console 一次(或跑 `app.cli set-webhook`)

原本 Tailscale 版本的這份清單有 6 步:註冊帳號、**Access controls** 頁貼
政策檔、**DNS** 頁啟用 HTTPS Certificates、產帶 `tag:helpdesk` 的 auth
key、Docker Desktop 開機啟動、填 LINE Console——中間 3 步(政策檔、
HTTPS 憑證、auth key)都是「節點身分」這個概念特有的設定。ngrok 沒有
節點身分,一次性步驟少了一半,細節見第四節。

---

## 十二、開放問題

| # | 問題 | 現在怎麼處理 |
|---|---|---|
| 1 | ~~userspace 模式能不能跑 Funnel~~ | **已回答,但問題本身失去意義。** 答案是能——userspace 模式沒有擋到 Funnel,Task 6 驗收全過。但 Funnel 本身後來因為 LINE 的 TLS 客戶端不相容被整個換成 ngrok(決策 2、第四節),這個問題也就跟著失效了 |
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
