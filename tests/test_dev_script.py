"""scripts/dev.py 的測試。

這支腳本本身不是產品的一部分,但它是**面試前十分鐘唯一會跑的東西**,
壞掉的代價跟正式碼一樣高,所以該守的還是要守。

scripts/ 不是套件,所以用檔案路徑載入。
"""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_dev():
    spec = importlib.util.spec_from_file_location("dev", ROOT / "scripts" / "dev.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dev = _load_dev()


def test_finds_url_in_a_real_cloudflared_log_line():
    """cloudflared 把網址夾在一個表格框裡印在 stderr,不是單獨一行。"""
    line = ("2026-09-22T01:02:03Z INF |  "
            "https://jolly-mountain-fresh-1234.trycloudflare.com   |\n")
    assert (dev.URL_RE.search(line).group(0)
            == "https://jolly-mountain-fresh-1234.trycloudflare.com")


def test_banner_spells_out_the_webhook_url(capsys):
    """印出網址還不夠 —— 要填進 Console 的是網址加上 /webhook/<slug>。
    少印那一段,現場就會有人只貼了根網址然後 Verify 失敗。"""

    class FakeTunnel:
        stderr = iter(["INF https://abc-def-123.trycloudflare.com\n"])

    dev._watch_tunnel(FakeTunnel())
    assert "https://abc-def-123.trycloudflare.com/webhook/" in capsys.readouterr().out


def test_prefers_cloudflared_from_path(monkeypatch):
    monkeypatch.setattr(dev.shutil, "which", lambda name: r"C:\somewhere\cloudflared.exe")
    assert dev.find_cloudflared() == r"C:\somewhere\cloudflared.exe"


def test_falls_back_to_install_location_when_path_is_stale(monkeypatch):
    """真實踩過的坑:winget 裝完只更新登錄檔的 PATH,已經在執行的行程
    (以及它們開出來的子行程)拿到的還是舊的環境變數。結果是「明明裝好了
    卻說找不到命令」,而那句話會把人導去重裝。"""
    monkeypatch.setattr(dev.shutil, "which", lambda name: None)
    winget_default = r"C:\Program Files (x86)\cloudflared\cloudflared.exe"
    monkeypatch.setattr(dev.os.path, "isfile", lambda p: p == winget_default)
    assert dev.find_cloudflared() == winget_default


def test_returns_none_when_really_not_installed(monkeypatch):
    monkeypatch.setattr(dev.shutil, "which", lambda name: None)
    monkeypatch.setattr(dev.os.path, "isfile", lambda p: False)
    assert dev.find_cloudflared() is None


def test_missing_cloudflared_never_starts_the_api(monkeypatch, capsys):
    """找不到就不要先起 uvicorn —— 起了又收不乾淨,8000 埠會被一個沒人管的
    行程佔住,下次重跑的症狀是「連不上但埠被佔」,指不到真正原因。"""
    started = []
    monkeypatch.setattr(dev, "find_cloudflared", lambda: None)
    monkeypatch.setattr(dev.subprocess, "Popen",
                        lambda *a, **kw: started.append(a) or None)
    assert dev.main() == 1
    assert started == []
    assert "winget install" in capsys.readouterr().err
