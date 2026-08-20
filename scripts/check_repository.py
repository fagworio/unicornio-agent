#!/usr/bin/env python3
"""Reject tracked local secret files and non-empty credential env assignments."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_PROTECTED_NAMES = {".env", ".env.local", ".env.production"}
_SECRET_ASSIGNMENT = re.compile(
    r"^(?:WORDPRESS_APP_PASSWORD|UH_WEBHOOK_SECRET|OPENAI_API_KEY)\s*=\s*(?!$).+",
    re.IGNORECASE,
)


def main() -> int:
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    violations: list[str] = []
    for name in tracked:
        if not name:
            continue
        path = ROOT / name
        if path.name in _PROTECTED_NAMES:
            violations.append(f"tracked secret file: {name}")
            continue
        if path.suffix not in {".env", ".example", ".md", ".toml", ".yml", ".yaml", ".py", ".sh"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            if _SECRET_ASSIGNMENT.match(line.strip()):
                violations.append(f"non-empty credential assignment: {name}:{line_no}")
    if violations:
        print("SECRET_SCAN_FAILED")
        print("\n".join(violations))
        return 1
    print("SECRET_SCAN_PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
