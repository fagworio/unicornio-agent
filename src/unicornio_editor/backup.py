"""Atomic post snapshots used for rollback and auditability."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


class SnapshotError(RuntimeError):
    """Raised for invalid or unreadable snapshots."""


def atomic_write_text(path: Path, content: str) -> None:
    """Durably replace a text file without exposing partial JSON to readers."""
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


class SnapshotStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.backup_root = self.root / "backups"

    def save(self, post_id: int, payload: dict[str, Any]) -> Path:
        self._validate_id(post_id)
        if not isinstance(payload, dict):
            raise SnapshotError("snapshot payload must be a JSON object")
        destination_dir = self.backup_root / str(post_id)
        destination_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{time.time_ns()}.json"
        destination = destination_dir / filename
        temporary = destination.with_suffix(".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        except (OSError, TypeError, ValueError) as exc:
            temporary.unlink(missing_ok=True)
            raise SnapshotError("could not write snapshot") from exc
        return destination

    def load(self, path: Path) -> dict[str, Any]:
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SnapshotError("could not read snapshot") from exc
        if not isinstance(value, dict):
            raise SnapshotError("snapshot must contain a JSON object")
        return value

    def latest(self, post_id: int) -> dict[str, Any]:
        self._validate_id(post_id)
        candidates = sorted((self.backup_root / str(post_id)).glob("*.json"))
        if not candidates:
            raise SnapshotError(f"no snapshot exists for post {post_id}")
        return self.load(candidates[-1])

    @staticmethod
    def _validate_id(post_id: int) -> None:
        if isinstance(post_id, bool) or not isinstance(post_id, int) or post_id < 1:
            raise SnapshotError("post id must be a positive integer")
