#!/usr/bin/env python3
"""Scan the staged Git tree for protected files and credential assignments."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_PROTECTED_NAMES = {".env", ".env.local", ".env.production"}
_SECRET_ASSIGNMENT = re.compile(
    r"^\s*[A-Z][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE_KEY)\s*[:=]\s*(.*?)\s*$",
)
_PLACEHOLDERS = {"", "...", "changeme", "change-me", "example", "your-value"}


def main() -> int:
    tracked = subprocess.check_output(["git", "ls-files", "--cached", "-z"], cwd=ROOT).decode().split("\0")
    violations: list[str] = []
    for name in tracked:
        if not name:
            continue
        path = Path(name)
        if path.name in _PROTECTED_NAMES:
            violations.append(f"tracked secret file: {name}")
            continue
        try:
            raw = subprocess.check_output(["git", "show", f":{name}"], cwd=ROOT)
        except subprocess.CalledProcessError:
            continue
        if b"\0" in raw:
            continue
        text = raw.decode("utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), 1):
            match = _SECRET_ASSIGNMENT.match(line)
            if not match:
                continue
            value = match.group(1).strip().strip("'\"").strip()
            if value and value.lower() not in _PLACEHOLDERS and not value.startswith(("${", "<")):
                violations.append(f"non-empty credential assignment: {name}:{line_no}")
    if violations:
        print("SECRET_SCAN_FAILED")
        print("\n".join(violations))
        return 1
    print("SECRET_SCAN_PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
