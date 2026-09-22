"""validate 與 seed 的命令列進入點。

    python -m app.cli validate --industry clinic
    python -m app.cli seed --industry clinic --slug my-clinic \
        --channel-secret xxx --channel-token yyy
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import select

from app.crypto import encrypt
from app.database import session_scope
from app.knowledge.loader import load_industry
from app.knowledge.render import render_system_prompt
from app.knowledge.validate import Finding, validate
from app.models import Company

ROOT = Path(__file__).resolve().parents[1]
INDUSTRIES = ROOT / "industries"


def _load(industry: str):
    return load_industry(INDUSTRIES / industry)


def run_validate(industry: str) -> list[Finding]:
    return validate(_load(industry))


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


def run_seed(industry: str, *, slug: str, channel_secret: str,
             channel_token: str, destination: str | None = None) -> str:
    data = _load(industry)
    findings = validate(data)
    if [f for f in findings if f.level == "ERROR"]:
        _report(findings)
        raise SystemExit(f"{industry} 有 ERROR,seed 中止。")

    with session_scope() as db:
        company = db.scalar(select(Company).where(Company.slug == slug))
        if company is None:
            company = Company(slug=slug)
            db.add(company)
        company.name = data["company"]["name"]
        company.industry = data["company"]["industry"]
        company.tone = data["company"]["tone"]
        company.forbidden_phrases = list(data["company"]["forbidden_phrases"])
        company.system_prompt = render_system_prompt(data)
        company.vector_collection = f"kb_{slug}"
        company.line_channel_secret_enc = encrypt(channel_secret)
        company.line_channel_token_enc = encrypt(channel_token)
        # destination 是 bot 自己的 userId(決策 2:路徑說是 A 公司、body 的
        # destination 卻是 B 公司的 bot,就拒絕)。第一次串接時還拿不到,
        # 所以允許不帶 —— 但不帶就等於那道防線是關著的,main() 會警告。
        # 只在有值時覆寫:重跑 seed 更新 FAQ 時沒帶這個參數,不該把已經
        # 設好的值清掉。
        if destination:
            company.line_destination = destination.strip()
        db.flush()
        return company.id


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

    s = sub.add_parser("seed")
    s.add_argument("--industry", required=True)
    s.add_argument("--slug", required=True)
    s.add_argument("--channel-secret", required=True)
    s.add_argument("--channel-token", required=True)
    s.add_argument("--destination",
                   help="bot 自己的 userId。不帶就不做 destination 交叉比對。")

    args = parser.parse_args(argv)
    if args.cmd == "validate":
        return 1 if _report(run_validate(args.industry)) else 0

    cid = run_seed(args.industry, slug=args.slug,
                   channel_secret=args.channel_secret,
                   channel_token=args.channel_token,
                   destination=args.destination)
    print(f"✓ 已寫入 company {cid}(slug={args.slug})")
    print(f"  webhook 路徑:/webhook/{args.slug}")
    if not args.destination:
        # 安靜地少一道防線,比明講出來危險得多。
        print("⚠ 沒有帶 --destination,destination 交叉比對是關著的。")
        print("  取得方式:先接上 webhook 讓官方帳號收一則訊息,伺服器 log 會印出")
        print("  收到的 destination,再用同一道指令加上 --destination 重跑一次。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
