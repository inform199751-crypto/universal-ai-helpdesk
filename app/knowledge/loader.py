"""把一個行業資料夾的 YAML 讀成一個 dict。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

FILES = ("company", "faq", "policies", "escalation", "glossary")

# 選填:沒有這份檔案就不提供那項功能,dict 裡也不會有這個鍵(一律用 .get 讀)。
# booking —— 預約(電商沒有)。
OPTIONAL_FILES = ("booking",)


def load_industry(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = {}
    missing = []
    for name in FILES:
        f = path / f"{name}.yaml"
        if not f.exists():
            missing.append(f"{name}.yaml")
            continue
        # utf-8-sig:Windows 上用記事本存的 YAML 會帶 BOM,PyYAML 會當成內容
        data[name] = yaml.safe_load(f.read_text(encoding="utf-8-sig"))
    if missing:
        raise FileNotFoundError(f"{path} 缺少:{'、'.join(missing)}")
    for name in OPTIONAL_FILES:
        f = path / f"{name}.yaml"
        if f.exists():
            data[name] = yaml.safe_load(f.read_text(encoding="utf-8-sig"))
    return data
