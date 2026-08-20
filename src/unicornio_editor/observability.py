"""Structured, secret-redacted processing telemetry."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

_VERSION = "0.1.0"
_SENSITIVE_PARTS = ("password", "token", "secret", "authorization", "cookie", "api_key")


def build_processing_markers(
    decision: str,
    confidence: float,
    *,
    processed_at: str | None = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    if decision not in {"process", "skip"}:
        raise ValueError("decision must be process or skip")
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    return {
        "_ai_editor_version": _VERSION,
        "_ai_editor_decision": decision,
        "_ai_editor_confidence": confidence,
        "_ai_editor_processed_at": processed_at or datetime.now(timezone.utc).isoformat(),
        "_ai_editor_correlation_id": correlation_id or str(uuid.uuid4()),
    }


def log_event(stream: TextIO, event: str, **fields: Any) -> None:
    safe_fields = {
        key: value for key, value in fields.items() if not _is_sensitive(key)
    }
    record = {"event": event, **safe_fields}
    stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def append_event(path: Path, event: str, **fields: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        log_event(stream, event, **fields)


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_PARTS)
