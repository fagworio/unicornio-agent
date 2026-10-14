"""Give isolated workflow tests an explicit, empty safety policy by default."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest


def _write_empty_quarantine(root: Path) -> None:
    policy = root / "config" / "v2-quarantine.json"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text(
        json.dumps({"version": 1, "posts": [], "release_history": []}),
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def explicit_test_quarantine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _write_empty_quarantine(tmp_path)
    original = tempfile.TemporaryDirectory

    def temporary_directory(*args, **kwargs):
        directory = original(*args, **kwargs)
        _write_empty_quarantine(Path(directory.name))
        return directory

    monkeypatch.setattr(tempfile, "TemporaryDirectory", temporary_directory)
