"""同時起 FastAPI 與 cloudflared,並把當次的公開網址印大一點。

    python scripts/dev.py                 # 只起服務,自己去填 webhook 網址
    python scripts/dev.py --slug bistro   # 起完自動把網址寫回 LINE Console

Quick Tunnel 的網址每次重啟都會變,而 LINE 的 webhook 網址是填在 Console
裡的 —— 忘了更新的症狀是 530,長得完全不像「網址過期」。帶 --slug 就讓
這支腳本自己用該公司的 access token 把新網址 PUT 回去,重開幾次都無所謂。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import httpx

PORT = 8000
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

# winget 的預設安裝位置。PATH 查不到時的第二順位。
CLOUDFLARED_FALLBACKS = (
    r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
    r"C:\Program Files\cloudflared\cloudflared.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\cloudflared.exe"),
)

CLOUDFLARED_MISSING = (
    "找不到 cloudflared,PATH 與已知的安裝位置都沒有。\n"
    "    winget install --id Cloudflare.cloudflared\n"
    "裝完要重開一個終端機,PATH 才會更新。"
)

LINE_ENDPOINT_API = "https://api.line.me/v2/bot/channel/webhook/endpoint"
TUNNEL_READY_TIMEOUT = 60.0


def find_cloudflared() -> str | None:
    """先查 PATH,再查已知的安裝位置。

    winget 裝完只更新登錄檔裡的 PATH,**已經在執行的行程,以及它們之後
    開出來的子行程,拿到的都還是舊的環境變數**。所以會出現「明明裝好了卻
    說找不到命令」—— 而那句話會把人導去重裝,查不到真正原因。
    這裡多看一眼安裝位置,就不必要求使用者理解 Windows 的 PATH 傳播規則。
    """
    found = shutil.which("cloudflared")
    if found:
        return found
    for path in CLOUDFLARED_FALLBACKS:
        if os.path.isfile(path):
            return path
    return None


def _wait_until_live(base_url: str, timeout: float = TUNNEL_READY_TIMEOUT) -> bool:
    """等 tunnel 真的開始轉送,再去跟 LINE 註冊。

    cloudflared 在 stderr 印出網址的那一刻,Cloudflare 的邊緣還不一定開始
    路由;而 LINE 的 PUT 會實際去打這個網址驗證,太早打就回 400
    「Invalid webhook endpoint URL」—— 那句話會讓人去檢查網址是不是打錯,
    但網址是對的,只是還沒活過來。實測:同一個網址早幾秒 400,通了之後 200。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/health", timeout=5).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1.0)
    return False


def _register_webhook(slug: str, base_url: str, webhook_url: str) -> str:
    """把當次的 tunnel 網址寫回 LINE Console,回傳一句要印給人看的話。

    app.* 是在函式裡才 import 的:不帶 --slug 的人不需要資料庫連得上、
    也不需要 .env 填好就能起服務,而這支腳本的價值就在於少一個出錯點。

    任何失敗都只回一句話,不往外丟 —— 自動更新失敗只是「要自己去貼」,
    不該連服務都起不來。
    """
    from app.crypto import decrypt
    from app.database import session_scope
    from app.models import Company
    from sqlalchemy import select

    with session_scope() as db:
        company = db.scalar(select(Company).where(Company.slug == slug))
        if company is None:
            return f"找不到 slug「{slug}」,沒有自動更新。先跑一次 app.cli seed。"
        token = decrypt(company.line_channel_token_enc)

    if not _wait_until_live(base_url):
        return (f"等了 {TUNNEL_READY_TIMEOUT:.0f} 秒 tunnel 還沒通,沒有自動更新。"
                "請手動貼上,或重跑一次。")

    r = httpx.put(LINE_ENDPOINT_API, json={"endpoint": webhook_url},
                  headers={"Authorization": f"Bearer {token}"}, timeout=20)
    if r.status_code != 200:
        return f"自動更新失敗(HTTP {r.status_code}:{r.text[:200]}),請手動貼上。"
    return "已自動寫回 LINE Console,不必手動貼、也不必按 Verify。"


def _watch_tunnel(proc, slug: str | None = None) -> None:
    for line in proc.stderr:  # cloudflared 把網址印在 stderr
        sys.stderr.write(line)
        m = URL_RE.search(line)
        if not m:
            continue
        url = m.group(0)
        webhook = f"{url}/webhook/{slug}" if slug else f"{url}/webhook/<你的 slug>"
        note = ""
        if slug:
            try:
                note = _register_webhook(slug, url, webhook)
            except Exception as exc:  # noqa: BLE001
                # 自動更新是便利功能,炸掉不能讓 tunnel 監看整個停掉 ——
                # 那樣連網址都印不出來,比沒有這個功能還糟。
                note = f"自動更新出錯({type(exc).__name__}: {exc}),請手動貼上。"
        else:
            note = "沒帶 --slug,要自己把上面那串填進 Console 並按 Verify。"
        bar = "=" * 72
        print(f"\n{bar}\n  公開網址:{url}"
              f"\n  LINE webhook:{webhook}"
              f"\n  {note}\n{bar}\n", flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/dev.py")
    parser.add_argument("--slug",
                        help="帶了就自動把當次的 tunnel 網址寫回 LINE Console。")
    args = parser.parse_args(argv)

    # 先確認 cloudflared 在,再起 uvicorn。順序相反的話,這裡失敗就會留下
    # 一個沒人管的 uvicorn 佔著 8000,下次重跑的症狀是「服務連不上、埠卻
    # 被佔住」—— 而這支腳本存在的場合正是面試前十分鐘。
    exe = find_cloudflared()
    if exe is None:
        print(CLOUDFLARED_MISSING, file=sys.stderr)
        return 1

    api = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)]
    )
    try:
        tunnel = subprocess.Popen(
            [exe, "tunnel", "--url", f"http://localhost:{PORT}"],
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        )
    except OSError as exc:
        api.terminate()
        api.wait()
        print(f"cloudflared 在 {exe} 但起不來:{exc}", file=sys.stderr)
        return 1

    threading.Thread(target=_watch_tunnel, args=(tunnel, args.slug),
                     daemon=True).start()
    try:
        api.wait()
    except KeyboardInterrupt:
        pass
    finally:
        tunnel.terminate()
        api.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
