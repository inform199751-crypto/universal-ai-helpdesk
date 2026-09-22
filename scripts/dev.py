"""同時起 FastAPI 與 cloudflared,並把當次的公開網址印大一點。

    python scripts/dev.py

Quick Tunnel 的網址每次重啟都會變,而 LINE 的 webhook 網址是填在
Console 裡的 —— 所以這支腳本唯一重要的事,就是讓你一眼看到網址,
不必去 log 裡撈。
"""

from __future__ import annotations

import re
import subprocess
import sys
import threading

PORT = 8000
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
CLOUDFLARED_MISSING = (
    "找不到 cloudflared。先安裝再跑這支腳本:\n"
    "    winget install --id Cloudflare.cloudflared\n"
    "裝完要重開一個終端機,PATH 才會更新。"
)


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
    api = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)]
    )
    try:
        tunnel = subprocess.Popen(
            ["cloudflared", "tunnel", "--url", f"http://localhost:{PORT}"],
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        )
    except OSError:
        # 沒有這段的話,uvicorn 會變成沒人管的孤兒行程繼續佔著 8000,
        # 下一次重跑就變成「服務連不上、埠卻被佔住」—— 而這支腳本存在的
        # 場合正是面試前十分鐘,那是最不該花時間查這種事的十分鐘。
        api.terminate()
        api.wait()
        print(CLOUDFLARED_MISSING, file=sys.stderr)
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
