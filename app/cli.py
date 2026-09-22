"""validate 與 seed 的命令列進入點。

    python -m app.cli list                      # 有哪些行業、哪個資料夾是哪一家
    python -m app.cli validate --industry clinic

    # 新公司:憑證必填
    python -m app.cli seed --industry restaurant --slug bistro \n        --channel-secret <32位十六進位> --channel-token <約172字元>

    # 已存在的公司:憑證省略,只套用資料的改動
    python -m app.cli seed --industry clinic --slug bistro --reset-history
    python -m app.cli seed --industry restaurant --slug bistro \n        --destination U7ec...
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from sqlalchemy import select

from app.crypto import encrypt
from app.database import session_scope
from app.knowledge.loader import load_industry
from app.knowledge.render import render_system_prompt
from app.knowledge.validate import Finding, validate
from app.models import ChatHistory, Company

ROOT = Path(__file__).resolve().parents[1]
INDUSTRIES = ROOT / "industries"


def _load(industry: str):
    return load_industry(INDUSTRIES / industry)


def run_validate(industry: str) -> list[Finding]:
    return validate(_load(industry))


def run_list() -> list[dict]:
    """掃 industries/ 底下有哪些行業,連同各自的規模與 ERROR 數。

    刻意不放任何硬編碼的行業清單 —— 這個專案的主張是「行業是參數,不是
    寫死的邏輯」,如果連列出行業都要改程式碼,那句話就站不住了。加第四個
    行業就是丟一個資料夾進去,這裡自然會看到。
    """
    rows: list[dict] = []
    if not INDUSTRIES.is_dir():
        return rows
    for d in sorted(INDUSTRIES.iterdir()):
        if not d.is_dir():
            continue
        try:
            data = load_industry(d)
        except Exception as exc:  # noqa: BLE001 —— 壞掉的行業要列出來,不是讓整個指令掛掉
            rows.append({"industry": d.name, "error": str(exc)})
            continue
        company = data.get("company") or {}
        rows.append({
            "industry": d.name,
            "name": company.get("name", "(company.yaml 沒有 name)"),
            "faq": len(data.get("faq") or []),
            "policies": len(data.get("policies") or []),
            "escalation": len(data.get("escalation") or []),
            "glossary": len(data.get("glossary") or []),
            "errors": len([f for f in validate(data) if f.level == "ERROR"]),
        })
    return rows


def _report_list(rows: list[dict]) -> None:
    if not rows:
        print("industries/ 底下沒有任何行業資料夾。")
        return
    print("可用的行業(資料夾名稱就是 --industry 要填的值):\n")
    for r in rows:
        if "error" in r:
            print(f"  {r['industry']}  <- 讀不進來:{r['error']}\n")
            continue
        flag = "" if r["errors"] == 0 else f"  ** 有 {r['errors']} 個 ERROR,seed 會被擋 **"
        print(f"  --industry {r['industry']}")
        print(f"      {r['name']}{flag}")
        print(f"      FAQ {r['faq']} 筆 · 政策 {r['policies']} 條 · "
              f"升級規則 {r['escalation']} 條 · 術語 {r['glossary']} 個\n")


def _report(findings: list[Finding]) -> int:
    blocks = [f for f in findings if f.level == "BLOCK"]
    errors = [f for f in findings if f.level == "ERROR"]
    if blocks:
        print("\n【BLOCK】以下不是 bug,是導入會議要跟客戶確認的事項:")
        for f in blocks:
            print(f"  規則 {f.rule}:{f.message}")
    if errors:
        print("\n【ERROR】資料有問題,必須修好:")
        for f in errors:
            print(f"  規則 {f.rule}:{f.message}")
    if not findings:
        print("✓ 驗證通過,沒有任何問題。")
    return len(errors)


SECRET_RE = re.compile(r"[0-9a-fA-F]{32}")
MIN_TOKEN_LENGTH = 100


def _check_credentials(channel_secret: str | None,
                       channel_token: str | None) -> None:
    """憑證格式明顯不對就拒絕,不要加密後安靜地寫進去。

    兩種貼上失誤都會產生「短到不可能是真的」的字串:在 PowerShell 主控台
    按 Ctrl+V 不是貼上,是塞進一個字面上的控制字元 ^V;複製時漏選則會得到
    半截。兩種都不報錯,加密之後看起來也一樣正常 —— 症狀要到 LINE Console
    按 Verify 回 401 才出現,那時人會去查 webhook 網址或 tunnel,查不到
    真正原因。在入口擋掉,錯誤訊息才指得到該去看的地方。
    """
    if channel_secret is not None and not SECRET_RE.fullmatch(channel_secret):
        raise SystemExit(
            f"channel secret 格式不對:收到 {len(channel_secret)} 個字元,"
            "但 LINE 的 channel secret 固定是 32 位十六進位字元。\n"
            "常見原因:在 PowerShell 主控台用 Ctrl+V 貼上(那不是貼上),"
            "或複製時漏選。主控台請用滑鼠右鍵、Ctrl+Shift+V,"
            "或改用 $x = (Get-Clipboard).Trim()。")
    if channel_token is not None and len(channel_token) < MIN_TOKEN_LENGTH:
        raise SystemExit(
            f"channel access token 太短:收到 {len(channel_token)} 個字元,"
            f"但 LINE 的 access token 至少 {MIN_TOKEN_LENGTH} 個字元"
            "(長期 token 約 172)。原因與修法同上。")


def run_seed(industry: str, *, slug: str, channel_secret: str | None = None,
             channel_token: str | None = None, destination: str | None = None,
             reset_history: bool = False) -> str:
    """把一個行業資料夾套用到某個 slug。

    憑證只在「這家公司還不存在」時必填。已存在的公司省略即可 —— 改店名、
    換行業、補 destination 都只是改資料,不該逼人再把憑證從 LINE Console
    複製一次,而那一步是整個導入流程裡最容易出錯的地方。
    """
    data = _load(industry)
    findings = validate(data)
    if [f for f in findings if f.level == "ERROR"]:
        _report(findings)
        raise SystemExit(f"{industry} 有 ERROR,seed 中止。")

    # Global Constraint 3:從網頁複製很容易連尾端換行一起帶走,而含換行的
    # HTTP 標頭會被整個丟掉,上游只回「Missing Authentication header」。
    channel_secret = channel_secret.strip() if channel_secret else None
    channel_token = channel_token.strip() if channel_token else None
    _check_credentials(channel_secret, channel_token)

    with session_scope() as db:
        company = db.scalar(select(Company).where(Company.slug == slug))
        if company is None:
            missing = [name for name, value in
                       (("--channel-secret", channel_secret),
                        ("--channel-token", channel_token)) if not value]
            if missing:
                raise SystemExit(
                    f"slug「{slug}」這家公司還不存在,建立時必須提供 "
                    f"{'、'.join(missing)}。省略憑證只對已經存在的公司成立。")
            company = Company(slug=slug)
            db.add(company)
        company.name = data["company"]["name"]
        company.industry = data["company"]["industry"]
        company.tone = data["company"]["tone"]
        company.forbidden_phrases = list(data["company"]["forbidden_phrases"])
        company.system_prompt = render_system_prompt(data)
        company.vector_collection = f"kb_{slug}"
        # 只覆寫這次有給的東西。沒給是「不動」,不是「清空」。
        if channel_secret:
            company.line_channel_secret_enc = encrypt(channel_secret)
        if channel_token:
            company.line_channel_token_enc = encrypt(channel_token)
        # destination 是 bot 自己的 userId(決策 2:路徑說是 A 公司、body 的
        # destination 卻是 B 公司的 bot,就拒絕)。第一次串接時還拿不到,
        # 所以允許不帶 —— 但不帶就等於那道防線是關著的,main() 會警告。
        if destination:
            company.line_destination = destination.strip()
        db.flush()
        company_id = company.id
        if reset_history:
            # 換行業時舊對話會留著:人設換成診所,最近十則卻還在講餐廳的
            # 停車位,模型會被帶偏。刪除限定這一家 —— 多租戶系統裡誤刪
            # 別人的對話紀錄救不回來。
            removed = (db.query(ChatHistory)
                       .filter(ChatHistory.company_id == company_id)
                       .delete(synchronize_session=False))
            print(f"  已清掉 {removed} 則舊對話(--reset-history)")
        return company_id


def _ensure_utf8_stdout() -> None:
    """重新導向過的 stdout(存成記錄檔、被別的程式接手 pipe、排進 CI 步驟)
    在繁體中文 Windows 上預設編碼是系統的 ANSI code page(cp950)。只有
    真正連著 Windows 主控台時,Python 才會自動走 UTF-8 的主控台 API——
    一旦被重新導向就退回 cp950,印「✓」這類字元會直接 UnicodeEncodeError
    崩潰,而這是成功路徑,不是邊角案例。這個工具的目標機器就是客戶端的
    繁體中文 Windows,不能只在互動式主控台下才動。

    reconfigure() 不是每個 stream 物件都有(測試裡接管 stdout 的替身
    可能沒有這個方法),所以先檢查再呼叫;失敗就照舊使用原本的 stdout,
    不能讓「修編碼」這個附加動作本身變成新的崩潰點。
    """
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8")
        except Exception:
            pass


def main(argv=None) -> int:
    _ensure_utf8_stdout()
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate")
    v.add_argument("--industry", required=True)

    sub.add_parser("list")

    s = sub.add_parser("seed")
    s.add_argument("--industry", required=True)
    s.add_argument("--slug", required=True)
    # 憑證只有新公司必填。已存在的公司省略就保留原值 —— 改店名、換行業、
    # 補 destination 都不該逼人再去 LINE Console 複製一次憑證。
    s.add_argument("--channel-secret",
                   help="只有新公司必填;已存在的公司省略就保留原值。")
    s.add_argument("--channel-token",
                   help="只有新公司必填;已存在的公司省略就保留原值。")
    s.add_argument("--destination",
                   help="bot 自己的 userId。不帶就不做 destination 交叉比對。")
    s.add_argument("--reset-history", action="store_true",
                   help="清掉這家公司的對話紀錄。換行業時建議加 —— "
                        "否則新人設會讀到舊行業的對話。")

    args = parser.parse_args(argv)
    if args.cmd == "list":
        _report_list(run_list())
        return 0
    if args.cmd == "validate":
        return 1 if _report(run_validate(args.industry)) else 0

    cid = run_seed(args.industry, slug=args.slug,
                   channel_secret=args.channel_secret,
                   channel_token=args.channel_token,
                   destination=args.destination,
                   reset_history=args.reset_history)
    print(f"✓ 已寫入 company {cid}(slug={args.slug})")
    print(f"  webhook 路徑:/webhook/{args.slug}")
    # 看資料庫的實際狀態,不是看這次有沒有帶參數 —— 上次就設好的公司
    # 再 seed 一次(例如只是換行業)不該還被警告防線是關的,那是謊話。
    with session_scope() as db:
        has_destination = bool(db.get(Company, cid).line_destination)
    if not has_destination:
        # 安靜地少一道防線,比明講出來危險得多。
        print("⚠ 沒有設定 destination,destination 交叉比對是關著的。")
        print("  取得方式:用這家公司的 access token 打")
        print("  GET https://api.line.me/v2/bot/info,回應裡的 userId 就是。")
        print("  再跑一次 seed 加上 --destination <那串> 即可,不必再給憑證。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
