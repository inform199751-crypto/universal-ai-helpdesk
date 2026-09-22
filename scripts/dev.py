"""同時起 FastAPI 與 cloudflared,並把當次的公開網址印大一點。

    python scripts/dev.py

Quick Tunnel 的網址每次重啟都會變,而 LINE 的 webhook 網址是填在
Console 裡的 —— 所以這支腳本唯一重要的事,就是讓你一眼看到網址,
不必去 log 裡撈。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading

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


def _watch_tunnel(proc) -> None:
    for line in proc.stderr:  # cloudflared 把網址印在 stderr
        sys.stderr.write(line)
        m = URL_RE.search(line)
        if m:
            url = m.group(0)
            bar = "=" * 72
            print(f"\n{bar}\n  公開網址:{url}"
                  f"\n  LINE webhook 請填:{url}/webhook/<你的 slug>"
                  f"\n  填完記得按 Verify\n{bar}\n", flush=True)


def main() -> int:
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

    threading.Thread(target=_watch_tunnel, args=(tunnel,), daemon=True).start()
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
