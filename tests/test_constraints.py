"""constraints.txt 的測試。

pyproject 只寫最低版本(>=),所以沒有鎖檔時 docker build 與 CI 都會抓
「當下最新版」。2026-10-05 的 build 就這樣抓到 SQLAlchemy 2.1、Starlette 1.7,
跟本機測過的 2.0、1.6 不一樣 —— 測試綠燈驗的不是正式環境跑的東西。

鎖檔壞掉的方式通常不是報錯,而是「寫了沒人用」或「漏鎖一個」,所以守的是這兩件事。
"""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _name(requirement: str) -> str:
    """'uvicorn[standard]>=0.32' → 'uvicorn';名稱照 PEP 503 正規化。"""
    raw = re.split(r"[\[<>=!~ ;]", requirement, maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", raw).lower()


def _pins() -> dict[str, str]:
    pins = {}
    for line in (ROOT / "constraints.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name, _, version = line.partition("==")
        assert version, f"constraints.txt 每一行都要是 名稱==版本:{line!r}"
        pins[_name(name)] = version
    return pins


def _declared() -> list[str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return project["dependencies"] + project["optional-dependencies"]["dev"]


def test_every_declared_dependency_is_pinned():
    missing = [d for d in _declared() if _name(d) not in _pins()]
    assert missing == []


def test_linux_only_runtime_dependencies_are_pinned_too():
    """uvicorn[standard] 只在非 Windows 裝 uvloop。鎖檔是在 Windows 的 venv
    產生的,pip freeze 不會列出它 —— 漏了的話,映像裡的 uvloop 一樣是浮動的。"""
    assert "uvloop" in _pins()


def test_the_docker_image_installs_with_the_constraints():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^COPY .*\bconstraints\.txt\b", dockerfile, re.M), \
        "constraints.txt 沒被 COPY 進映像,pip 的 -c 會找不到檔案"
    assert re.search(r"pip install .*-c constraints\.txt", dockerfile)


def test_ci_installs_with_the_constraints():
    """CI 抓最新版的話,綠燈驗的是跟正式映像不一樣的組合。"""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    installs = [line for line in ci.splitlines() if "pip install" in line]
    assert installs, "ci.yml 裡找不到 pip install"
    assert all("-c constraints.txt" in line for line in installs)
