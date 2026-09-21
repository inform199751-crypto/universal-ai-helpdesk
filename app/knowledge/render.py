"""把五份 YAML 組成一段 system prompt。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


def render_system_prompt(data: dict[str, Any]) -> str:
    # StrictUndefined:樣板引用了資料裡沒有的欄位要當場報錯,
    # 不要默默算成空字串 —— 那會變成一個少了半段人設的客服。
    env = Environment(
        loader=FileSystemLoader(PROMPTS_DIR),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env.get_template("system.j2").render(**data).strip()
