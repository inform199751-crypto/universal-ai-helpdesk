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


def test_missing_cloudflared_does_not_leave_the_api_orphaned(monkeypatch, capsys):
    """cloudflared 沒裝時,uvicorn 已經起來了。不收掉它,8000 埠會被一個
    沒人管的行程佔住,下一次重跑的症狀是「連不上但埠被佔」—— 指不到真正原因。"""
    events = []

    class FakeApi:
        def terminate(self):
            events.append("terminate")

        def wait(self, *args, **kwargs):
            events.append("wait")
            return 0

    def fake_popen(cmd, **kwargs):
        if cmd[0] == "cloudflared":
            raise FileNotFoundError(2, "no such file", "cloudflared")
        return FakeApi()

    monkeypatch.setattr(dev.subprocess, "Popen", fake_popen)
    assert dev.main() == 1
    assert events == ["terminate", "wait"]
    assert "winget install" in capsys.readouterr().err
