"""Explicit, fail-closed migration of provably untouched V2 posts to V1.

This is an administrative bridge, not a retry or reset command.  It only
removes the V2 marker when the checkpoint proves that no editorial/media
effect was attempted and no local journal or MediaRegistry operation exists.
The original V2 checkpoint is archived before the WordPress metadata write and
the write is verified afterwards.  Content and attachments are never changed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .epoch_protection import load_epoch_protection
from .lock import RunSessionLock
from .model import BlockerCode, FeaturedStatus, LifecycleState, Phase, WorkState
from .quarantine import assert_post_not_quarantined

V1_MIGRATION_META = "_hermes_v1_migration"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _state_from_post(post: dict[str, Any]) -> WorkState | None:
    meta = post.get("meta") if isinstance(post.get("meta"), dict) else {}
    raw = meta.get("_hermes_work_state")
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        return WorkState.from_dict(value) if isinstance(value, dict) else None
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None


def _registry_has_operation(root: Path, post_id: int) -> tuple[bool, str]:
    database = root / "work" / "media_registry.sqlite"
    if not database.is_file():
        return False, "registry_absent"
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "media_operations" not in tables:
                return False, "operations_table_absent"
            row = connection.execute(
                "SELECT 1 FROM media_operations WHERE post_id=? LIMIT 1", (post_id,)
            ).fetchone()
            return bool(row), "operation_found" if row else "no_operation"
    except (OSError, sqlite3.Error) as exc:
        return True, f"registry_unreadable:{type(exc).__name__}"


def _journal_has_post(root: Path, post_id: int) -> tuple[bool, str]:
    journal_dir = root / "work" / "v2-journal"
    if not journal_dir.is_dir():
        return False, "journal_absent"
    try:
        paths = sorted(journal_dir.glob("*.json"))
        for path in paths:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                return True, "journal_unreadable"
            if isinstance(value, dict) and int(value.get("post_id") or 0) == post_id:
                return True, f"journal_found:{path.name}"
    except OSError:
        return True, "journal_unreadable"
    return False, "no_journal"


def _safe_candidate(post: dict[str, Any], root: Path) -> tuple[bool, dict[str, Any]]:
    post_id = post.get("id")
    checks: dict[str, Any] = {"post_id": post_id}
    if not isinstance(post_id, int):
        checks["reason"] = "invalid_post_id"
        return False, checks
    if post.get("status") != "pending":
        checks["reason"] = "status_not_pending"
        return False, checks
    meta = post.get("meta") if isinstance(post.get("meta"), dict) else {}
    if meta.get("_hermes_state") not in (None, ""):
        checks["reason"] = "legacy_state_already_present"
        return False, checks
    if meta.get(V1_MIGRATION_META):
        checks["reason"] = "already_migrated"
        return False, checks
    state = _state_from_post(post)
    if state is None:
        checks["reason"] = "invalid_or_missing_v2_state"
        return False, checks
    checks["v2_state"] = state.to_dict()
    # Relevance is the only phase before editorial/media work.  A clean,
    # untouched checkpoint can safely be handed to V1 for a fresh generation.
    if state.state is not LifecycleState.PENDING:
        checks["reason"] = "v2_state_not_pending"
        return False, checks
    if state.phase is not Phase.RELEVANCE or state.blocker not in (None, BlockerCode.RELEVANCE_UNCERTAIN):
        checks["reason"] = "phase_or_blocker_has_work"
        return False, checks
    if state.retry.attempts or state.retry.phase_attempts or state.retry.no_progress:
        checks["reason"] = "retry_effect_exists"
        return False, checks
    if state.media.inline or state.media.accepted or state.media.featured.status is not FeaturedStatus.MISSING:
        checks["reason"] = "media_effect_exists"
        return False, checks
    if state.media.search.queries_attempted or state.media.search.candidates_seen:
        checks["reason"] = "media_search_effect_exists"
        return False, checks
    registry_effect, registry_reason = _registry_has_operation(root, post_id)
    journal_effect, journal_reason = _journal_has_post(root, post_id)
    checks.update({"registry": registry_reason, "journal": journal_reason})
    if registry_effect:
        checks["reason"] = "media_registry_effect_or_unavailable"
        return False, checks
    if journal_effect:
        checks["reason"] = "v2_journal_effect_or_unavailable"
        return False, checks
    return True, checks


def _iter_posts(client: Any, post_ids: Iterable[int] | None) -> Iterable[dict[str, Any]]:
    if post_ids:
        for post_id in post_ids:
            post = client.get_post(int(post_id))
            if isinstance(post, dict):
                yield post
        return
    page = 1
    while True:
        posts = client.list_pending(page=page, per_page=100, status="pending") or []
        if not posts:
            return
        yield from (post for post in posts if isinstance(post, dict))
        if len(posts) < 100:
            return
        page += 1


def migrate_safe_v2_to_v1(
    client: Any,
    root: Path | str,
    *,
    post_ids: Iterable[int] | None = None,
    apply: bool = False,
    limit: int = 0,
) -> dict[str, Any]:
    """Preview/apply the narrow V2→V1 bridge with readback and audit archive."""

    root_path = Path(root)
    candidates: list[dict[str, Any]] = []
    scanned = 0
    for post in _iter_posts(client, post_ids):
        scanned += 1
        eligible, checks = _safe_candidate(post, root_path)
        if eligible:
            state_raw = (post.get("meta") or {}).get("_hermes_work_state")
            canonical = _json(json.loads(state_raw) if isinstance(state_raw, str) else state_raw)
            candidates.append({
                "post_id": int(post["id"]),
                "state_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
                "checks": checks,
            })
            if limit and len(candidates) >= limit:
                break

    migrated: list[dict[str, Any]] = []
    if apply and candidates:
        ids = [int(item["post_id"]) for item in candidates]
        protection = load_epoch_protection(root_path)
        for post_id in ids:
            protection.assert_writable(post_id, effect="v2-safe-v1-migration")
            assert_post_not_quarantined(root_path, post_id, operation="v2-safe-v1-migration")
        lock = RunSessionLock(root_path / "work" / "v2-run.lock")
        if not lock.acquire():
            raise RuntimeError("V2 run lock is held; refusing safe V2→V1 migration")
        try:
            for item in candidates:
                post_id = int(item["post_id"])
                latest = client.get_post(post_id)
                eligible, checks = _safe_candidate(latest, root_path)
                if not eligible:
                    item["skipped"] = checks.get("reason", "changed_after_preview")
                    continue
                raw_state = (latest.get("meta") or {}).get("_hermes_work_state")
                canonical = _json(json.loads(raw_state) if isinstance(raw_state, str) else raw_state)
                state_sha256 = hashlib.sha256(canonical.encode()).hexdigest()
                if state_sha256 != item["state_sha256"]:
                    item["skipped"] = "state_changed_after_preview"
                    continue
                migration_id = f"v2-to-v1-{post_id}-{state_sha256[:16]}"
                archive = root_path / "work" / "v1-migrations" / migration_id / "post.json"
                archive.parent.mkdir(parents=True, exist_ok=True)
                if not archive.exists():
                    archive.write_text(
                        json.dumps({
                            "migration_id": migration_id,
                            "created_at": _now(),
                            "post_id": post_id,
                            "state_sha256": state_sha256,
                            "v2_state": json.loads(canonical),
                            "wordpress_status": latest.get("status"),
                        }, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                client.update_post(post_id, {"meta": {
                    "_hermes_work_state": None,
                    "_hermes_state": "new",
                    "_hermes_attempts": "0",
                    "_hermes_next_retry_at": "",
                    V1_MIGRATION_META: _json({
                        "migration_id": migration_id,
                        "from": "v2",
                        "state_sha256": state_sha256,
                        "archive": str(archive),
                        "content_and_attachments_changed": False,
                    }),
                }})
                readback = client.get_post(post_id)
                readback_meta = readback.get("meta") if isinstance(readback.get("meta"), dict) else {}
                if "_hermes_work_state" in readback_meta and readback_meta.get("_hermes_work_state") not in (None, ""):
                    raise RuntimeError(f"V2 state readback remained present for post {post_id}")
                if readback_meta.get("_hermes_state") != "new":
                    raise RuntimeError(f"V1 state readback failed for post {post_id}")
                migrated.append({
                    "post_id": post_id,
                    "migration_id": migration_id,
                    "archive": str(archive),
                    "eligible_for_v1": True,
                    "content_and_attachments_changed": False,
                })
        finally:
            lock.release()

    return {
        "command": "v2-migrate-safe-v1",
        "apply": bool(apply),
        "scanned": scanned,
        "candidates": len(candidates),
        "migrated": len(migrated),
        "post_ids": [item["post_id"] for item in migrated] if apply else [item["post_id"] for item in candidates],
        "items": migrated if apply else candidates,
        "read_only": not apply,
        "writes": {"wordpress_metadata": len(migrated), "content": 0, "attachments": 0},
    }


__all__ = ["migrate_safe_v2_to_v1"]
