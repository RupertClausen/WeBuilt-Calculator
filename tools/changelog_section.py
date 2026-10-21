"""Print one version's section of CHANGELOG.md (used as release notes).

    python tools/changelog_section.py 1.2.3
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def section(version: str, text: str) -> str:
    m = re.search(rf"^## \[?{re.escape(version)}\]?.*?$(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not m:
        raise SystemExit(f"CHANGELOG.md has no section for {version}")
    body = m.group(1).strip()
    if not body:
        raise SystemExit(f"CHANGELOG.md section for {version} is empty")
    return body


if __name__ == "__main__":
    print(section(sys.argv[1], (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")))
