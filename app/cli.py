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
             channel_token: str) -> str:
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
        db.flush()
        return company.id


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate")
    v.add_argument("--industry", required=True)

    s = sub.add_parser("seed")
    s.add_argument("--industry", required=True)
    s.add_argument("--slug", required=True)
    s.add_argument("--channel-secret", required=True)
    s.add_argument("--channel-token", required=True)

    args = parser.parse_args(argv)
    if args.cmd == "validate":
        return 1 if _report(run_validate(args.industry)) else 0

    cid = run_seed(args.industry, slug=args.slug,
                   channel_secret=args.channel_secret,
                   channel_token=args.channel_token)
    print(f"✓ 已寫入 company {cid}(slug={args.slug})")
    print(f"  webhook 路徑:/webhook/{args.slug}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
