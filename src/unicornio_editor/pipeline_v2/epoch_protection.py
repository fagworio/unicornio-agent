"""Shared fail-closed protection for transactions owned by the active V2 epoch."""
from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any


class EpochProtectionError(RuntimeError):
    """Raised when epoch protection cannot be safely evaluated."""


@dataclass(frozen=True)
class EpochProtection:
    epoch_id: str | None
    protected_ids: frozenset[int]
    source: Path
    policy_fingerprint: str = ""
    valid: bool = True

    def is_protected(self, post_id: int) -> bool:
        return int(post_id) in self.protected_ids

    def assert_writable(self, post_id: int, *, effect: str = "external_write") -> None:
        if self.is_protected(post_id):
            raise EpochProtectionError(
                f"post {int(post_id)} is protected by active epoch "
                f"{self.epoch_id or '[unknown]'}; effect={effect} denied"
            )


def load_epoch_protection(root: str | Path) -> EpochProtection:
    path = Path(root) / "work" / "v2-epochs" / "active" / "epoch.json"
    if not path.exists():
        return EpochProtection(None, frozenset(), path)
    try:
        raw_bytes = path.read_bytes()
        payload: Any = json.loads(raw_bytes)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise EpochProtectionError(f"active epoch protection unreadable: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise EpochProtectionError("active epoch protection schema invalid")
    epoch_id = str(payload.get("epoch_id") or "").strip()
    raw_ids = payload.get("protected_transaction_ids")
    if not epoch_id or not isinstance(raw_ids, list):
        raise EpochProtectionError("active epoch protection identity or IDs missing")
    try:
        ids = frozenset(int(value) for value in raw_ids)
    except (TypeError, ValueError) as exc:
        raise EpochProtectionError("active epoch protected IDs invalid") from exc
    if any(value < 1 for value in ids):
        raise EpochProtectionError("active epoch protected IDs must be positive")
    return EpochProtection(epoch_id, ids, path, sha256(raw_bytes).hexdigest())


__all__ = ["EpochProtection", "EpochProtectionError", "load_epoch_protection"]
