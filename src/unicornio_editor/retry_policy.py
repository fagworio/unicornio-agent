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
from typing import Any, Callable

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
def _editorial_lock(root: Path | str, suffix: str, *, blocking: bool = True):
    path = editorial_backoff_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}{suffix}")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
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


_V1_LANES = ("new", "partial", "recovery")


def _v1_rotation_path(root: Path) -> Path:
    return root / "work" / "v1-editorial-rotation.json"


def v1_rotation(root: Path) -> dict[str, dict[str, Any]]:
    try:
        state = json.loads(_v1_rotation_path(root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(state, dict) or any(
        lane not in _V1_LANES or not isinstance(row, dict)
        or type(row.get("post_id")) is not int or type(row.get("turn")) is not int
        for lane, row in state.items()
    ):
        raise ValueError("invalid V1 editorial rotation")
    return state


def v1_lane(state: str | None) -> str | None:
    if state in ("new", "partial"):
        return state
    return "recovery" if state in ("blocked", "uncertain") else None


def record_v1_prepared(root: Path, lane: str | None, post_id: int, batch_id: str) -> None:
    if lane is None:
        return
    with _editorial_lock(root, ".v1-rotation.lock"):
        state = v1_rotation(root)
        previous = state.get(lane, {})
        if (previous.get("post_id"), previous.get("batch_id")) == (post_id, batch_id):
            return
        turn = 1 + max((row["turn"] for row in state.values()), default=0)
        state[lane] = {"post_id": post_id, "turn": turn, "batch_id": batch_id}
        _atomic_write_json(_v1_rotation_path(root), state)


# Advisory pagination only: never grants eligibility or changes the preparation
# rotation, and never makes the selector wait for another process.
V1_CARDS_MAX_REST_PAGE = 10_000


def _v1_cards_cursor_path(root: Path) -> Path:
    return root / "work" / "v1-cards-scan.json"


def v1_cards_scan_cursor(root: Path) -> tuple[int, bool]:
    """Return next page and whether only the rework lane was missing."""
    try:
        data = json.loads(_v1_cards_cursor_path(root).read_text(encoding="utf-8"))
        page = data.get("next_page") if isinstance(data, dict) else None
        missing_rework = data.get("missing_rework_only") if isinstance(data, dict) else None
    except (OSError, ValueError):
        return 1, False
    if (
        type(page) is int and 1 <= page <= V1_CARDS_MAX_REST_PAGE
        and type(missing_rework) is bool
    ):
        return page, missing_rework
    return 1, False


def checkpoint_v1_cards_scan(
    root: Path, *, previous: tuple[int, bool], cursor: tuple[int, bool],
) -> None:
    if previous == cursor:
        return
    try:
        with _editorial_lock(root, ".v1-cards-scan.lock", blocking=False):
            if v1_cards_scan_cursor(root) == previous:
                _atomic_write_json(_v1_cards_cursor_path(root), {
                    "next_page": cursor[0], "missing_rework_only": cursor[1],
                })
    except OSError:
        # An unavailable discovery hint must not suppress otherwise safe cards.
        pass


def _v1_defer_path(root: Path, post_id: int) -> Path:
    return root / "backups" / str(post_id) / "editorial.defer.json"


def _read_v1_defer(root: Path, post_id: int) -> dict[str, Any]:
    try:
        row = json.loads(_v1_defer_path(root, post_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(row, dict) or row.get("post_id") != post_id:
        raise ValueError(f"invalid V1 deferred retry for post {post_id}")
    try:
        marked = datetime.fromisoformat(row["recorded_at"])
        until = datetime.fromisoformat(row["not_before"])
        delay = (until - marked).total_seconds()
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError(f"invalid V1 deferred retry for post {post_id}") from exc
    if (
        marked.tzinfo is None or until.tzinfo is None or not 0 < delay <= 720 * 60
        or marked > datetime.now(timezone.utc) + timedelta(minutes=5)
    ):
        raise ValueError(f"invalid V1 deferred retry boundary for post {post_id}")
    return row


def potential_v1_deferral(root: Path, post: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """A cheap revision hint for discovery; never veto an unhydrated card."""
    revision = post.get("modified_gmt")
    if not isinstance(revision, str) or not revision.strip():
        return {}
    row = _read_v1_defer(root, int(post["id"]))
    if not row or revision != row.get("modified_gmt"):
        return {}
    until = datetime.fromisoformat(row["not_before"])
    return row if until > (now or datetime.now(timezone.utc)) else {}


def v1_deferral(
    root: Path, post: dict[str, Any], *, digest: Callable[[dict[str, Any]], str],
    now: datetime | None = None,
) -> dict[str, Any]:
    row = potential_v1_deferral(root, post, now=now)
    return row if row and row.get("signature") == digest(post) else {}


def record_v1_deferral(
    root: Path, post: dict[str, Any], *, batch_id: str, signature: str, reason_code: str,
    delay_minutes: int, now: datetime | None = None,
) -> dict[str, Any]:
    """The caller must hold this post's established lock and verify its revision."""
    post_id = int(post["id"])
    previous = _read_v1_defer(root, post_id)
    if (previous.get("batch_id"), previous.get("signature")) == (batch_id, signature):
        return previous
    recorded = now or datetime.now(timezone.utc)
    available = recorded + timedelta(minutes=max(1, min(720, delay_minutes)))
    row = {
        "post_id": post_id, "batch_id": batch_id, "signature": signature,
        "modified_gmt": post.get("modified_gmt") or "", "reason_code": reason_code,
        "recorded_at": recorded.isoformat(timespec="seconds"),
        "not_before": available.isoformat(timespec="seconds"),
    }
    path = _v1_defer_path(root, post_id)
    _atomic_write_json(path, row)
    if _read_v1_defer(root, post_id) != row:
        raise ValueError(f"V1 deferred retry readback mismatch for post {post_id}")
    return row
