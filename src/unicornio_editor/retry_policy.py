"""Bounded HTTP ``Retry-After`` parsing and durable editorial cooldown state."""

from __future__ import annotations

import json
import fcntl
import math
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from contextlib import contextmanager
from typing import Any

MAX_RETRY_AFTER_SECONDS = 7 * 24 * 60 * 60
DEFAULT_EDITORIAL_RETRY_SECONDS = 5 * 60
EDITORIAL_BACKOFF_FILENAME = "editorial-provider-backoff.json"
_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


def parse_retry_after_seconds(
    value: str | None,
    *,
    now: datetime | None = None,
) -> int | None:
    """Parse RFC delay-seconds or HTTP-date without sleeping in the caller.

    A server-provided delay is bounded to seven days so a malformed or hostile
    response cannot indefinitely suppress the scheduled worker. ``None`` means
    no usable server delay was supplied; the caller chooses its own fallback.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if re.fullmatch(r"[0-9]+", raw):
        return min(int(raw), MAX_RETRY_AFTER_SECONDS)
    try:
        retry_at = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return None
    delay = math.ceil(
        (retry_at.astimezone(timezone.utc) - current.astimezone(timezone.utc)).total_seconds()
    )
    return min(MAX_RETRY_AFTER_SECONDS, max(0, delay))


def editorial_backoff_path(root: Path | str) -> Path:
    return Path(root) / "work" / EDITORIAL_BACKOFF_FILENAME


def record_editorial_backoff(
    root: Path | str,
    *,
    batch_id: str,
    http_status: int,
    retry_after_seconds: int | None,
    failure_class: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist one bounded provider cooldown so cron ticks cannot spin on 429."""
    if not isinstance(batch_id, str) or not _BATCH_ID_RE.fullmatch(batch_id):
        raise ValueError("batch_id is required to persist editorial backoff")
    if type(http_status) is not int or http_status != 429:
        raise ValueError("editorial backoff is only valid for HTTP 429")
    if failure_class != "rate_limited":
        raise ValueError("editorial backoff failure_class must be rate_limited")
    if retry_after_seconds is None:
        delay = DEFAULT_EDITORIAL_RETRY_SECONDS
    elif isinstance(retry_after_seconds, bool) or not isinstance(retry_after_seconds, int):
        raise ValueError("retry_after_seconds must be an integer or null")
    else:
        delay = min(MAX_RETRY_AFTER_SECONDS, max(0, retry_after_seconds))
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    recorded_at = current.astimezone(timezone.utc)
    state: dict[str, Any] = {
        "schema_version": 1,
        "provider": "editorial",
        "batch_id": batch_id,
        "http_status": 429,
        "failure_class": failure_class.strip()[:80],
        "retry_after_seconds": delay,
        "recorded_at": recorded_at.isoformat(timespec="seconds"),
        "not_before": (recorded_at + timedelta(seconds=delay)).isoformat(timespec="seconds"),
    }
    with _backoff_lock(root):
        _atomic_write_json(editorial_backoff_path(root), state)
    return state


def editorial_backoff_status(
    root: Path | str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read and validate the persisted editorial cooldown; corrupt state fails closed."""
    path = editorial_backoff_path(root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"status": "none", "active": False}
    except (OSError, json.JSONDecodeError, ValueError):
        return {"status": "invalid", "active": True, "remaining_seconds": None}
    if not isinstance(value, dict):
        return {"status": "invalid", "active": True, "remaining_seconds": None}
    batch_id = value.get("batch_id")
    try:
        not_before = datetime.fromisoformat(
            str(value.get("not_before") or "").replace("Z", "+00:00")
        )
        recorded_at = datetime.fromisoformat(
            str(value.get("recorded_at") or "").replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return {"status": "invalid", "active": True, "remaining_seconds": None}
    retry_after = value.get("retry_after_seconds")
    if (
        type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or type(value.get("http_status")) is not int
        or value.get("provider") != "editorial"
        or value.get("http_status") != 429
        or not isinstance(batch_id, str)
        or not _BATCH_ID_RE.fullmatch(batch_id)
        or not_before.tzinfo is None
        or value.get("failure_class") != "rate_limited"
        or isinstance(retry_after, bool)
        or not isinstance(retry_after, int)
        or not 0 <= retry_after <= MAX_RETRY_AFTER_SECONDS
        or recorded_at.tzinfo is None
    ):
        return {"status": "invalid", "active": True, "remaining_seconds": None}
    persisted_delay = (
        not_before.astimezone(timezone.utc) - recorded_at.astimezone(timezone.utc)
    ).total_seconds()
    if (
        persisted_delay < 0
        or persisted_delay > MAX_RETRY_AFTER_SECONDS
        or abs(persisted_delay - retry_after) > 1
    ):
        return {"status": "invalid", "active": True, "remaining_seconds": None}
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return {"status": "invalid", "active": True, "remaining_seconds": None}
    remaining = max(
        0,
        math.ceil((not_before.astimezone(timezone.utc) - current.astimezone(timezone.utc)).total_seconds()),
    )
    return {
        **value,
        "status": "active" if remaining else "expired",
        "active": bool(remaining),
        "remaining_seconds": remaining,
    }


def clear_editorial_backoff(root: Path | str) -> None:
    """Clear the rate-limit marker after the saved batch completes successfully."""
    with _backoff_lock(root):
        editorial_backoff_path(root).unlink(missing_ok=True)


def clear_expired_editorial_backoff(
    root: Path | str,
    *,
    batch_id: str,
) -> dict[str, Any] | None:
    """Atomically clear only this batch's expired marker, never a newer 429."""
    with _backoff_lock(root):
        state = editorial_backoff_status(root)
        if state.get("status") != "expired" or state.get("batch_id") != batch_id:
            return None
        editorial_backoff_path(root).unlink(missing_ok=True)
        return state


@contextmanager
def _backoff_lock(root: Path | str):
    with _editorial_lock(root, ".lock"):
        yield


@contextmanager
def editorial_provider_request_lock(root: Path | str):
    """Serialize provider admission, cache checks, requests, and output writes."""
    with _editorial_lock(root, ".request.lock"):
        yield


@contextmanager
def _editorial_lock(root: Path | str, suffix: str):
    path = editorial_backoff_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}{suffix}")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
