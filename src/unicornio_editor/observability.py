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
        "_ai_editor_confidence": str(confidence),
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


# ---------------------------------------------------------------------------
# Telemetria central da fila de publicacao (blocagens / resultados).
# ---------------------------------------------------------------------------
TELEMETRY_FILENAME = "telemetry.jsonl"


def telemetry_path(root: str | Path) -> Path:
    return Path(root) / "work" / TELEMETRY_FILENAME


def append_telemetry(root: str | Path, event: str, **fields: Any) -> None:
    """Registra um evento do pipeline no telemetry.jsonl central (fail-soft)."""
    append_event(
        telemetry_path(root),
        event,
        ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **fields,
    )


def read_telemetry_summary(root: str | Path) -> dict[str, Any]:
    """Resumo agregado do telemetry.jsonl (contadores por evento e motivo)."""
    path = telemetry_path(root)
    counts: dict[str, int] = {}
    reasons: dict[str, dict[str, int]] = {}
    last_ts: str | None = None
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            event = record.get("event")
            if not isinstance(event, str):
                continue
            counts[event] = counts.get(event, 0) + 1
            reason = record.get("reason")
            if isinstance(reason, str) and reason.strip():
                bucket = reasons.setdefault(event, {})
                bucket[reason] = bucket.get(reason, 0) + 1
            ts = record.get("ts")
            if isinstance(ts, str):
                last_ts = ts
    return {
        "file": str(path),
        "total_events": sum(counts.values()),
        "by_event": counts,
        "by_reason": reasons,
        "last_event_at": last_ts,
    }


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_PARTS)
