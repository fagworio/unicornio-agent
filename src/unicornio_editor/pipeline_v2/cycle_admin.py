"""Administrative, auditable V2 cycle transitions.

This module never touches WordPress.  It archives an interrupted cycle and
creates a fresh active checkpoint only after the caller supplies the exact
cycle and pending authorization identities.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from ..backup import atomic_write_text
from .cycle_state import CycleState, CycleStateStore, SCHEMA_VERSION
from .lock import RunSessionLock


TRANSITION_VERSION = 1
TRANSITION_NAME = "v2-cycle-abandon-transition.json"


class CycleAbandonError(RuntimeError):
    """The requested administrative transition is not safe to apply."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise CycleAbandonError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise CycleAbandonError(f"JSON object required: {path}")
    return value


def _transition_path(root: Path) -> Path:
    return Path(root) / "work" / "v2-cycle" / TRANSITION_NAME


def _validate_expected(
    state: CycleState,
    *,
    cycle_id: str,
    authorization_id: str,
    selected_ids: list[int],
    pending_round: int,
) -> None:
    pending = state.pending_round
    if state.cycle_id != cycle_id:
        raise CycleAbandonError("cycle_id does not match active checkpoint")
    if state.status != "running" or state.phase != "DRAINING":
        raise CycleAbandonError("active cycle is not the expected running DRAINING cycle")
    if not isinstance(pending, dict):
        raise CycleAbandonError("active checkpoint has no pending round")
    if int(pending.get("round_number", -1)) != int(pending_round):
        raise CycleAbandonError("pending round does not match")
    if str(pending.get("authorization_id") or "") != authorization_id:
        raise CycleAbandonError("authorization_id does not match active checkpoint")
    actual_ids = [int(value) for value in (pending.get("preview_selected_ids") or [])]
    if actual_ids != [int(value) for value in selected_ids]:
        raise CycleAbandonError("historical selection does not match active checkpoint")


def _archive_name(cycle_id: str, token: str) -> str:
    return f"{cycle_id}-{token}"


def _copy_verified(source: Path, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    source_hash = _sha256(source)
    if _sha256(destination) != source_hash:
        raise CycleAbandonError(f"archive hash mismatch for {source.name}")
    return source_hash


def _quarantine_payload(root: Path, *, apply_post_id: int | None) -> tuple[dict[str, Any], dict[str, Any]]:
    path = Path(root) / "config" / "v2-quarantine.json"
    if not path.is_file():
        raise CycleAbandonError("existing V2 exclusion policy is absent")
    payload = _read_json(path)
    if payload.get("version") != 1 or not isinstance(payload.get("posts"), list) or not isinstance(payload.get("release_history"), list):
        raise CycleAbandonError("existing V2 exclusion policy is invalid")
    before = json.loads(json.dumps(payload))
    if apply_post_id is not None:
        rows = [row for row in payload["posts"] if isinstance(row, dict)]
        existing = {int(row.get("post_id")) for row in rows if str(row.get("post_id", "")).isdigit()}
        if apply_post_id not in existing:
            payload["posts"].append({
                "post_id": int(apply_post_id),
                "reason_code": "orphaned_transaction",
                "reason": "Post has an incomplete V2 journal without cycle/round/authorization identity; exclude automatic writes until independently resolved.",
            })
    return before, payload


def _apply_exclusion(root: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(Path(root) / "config" / "v2-quarantine.json", _json(payload))


def _new_active_payload(cycle_id: str, config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
    state = CycleState(cycle_id=cycle_id, config=dict(config), started_at=now, updated_at=now)
    payload = state.to_dict()
    event = {
        "schema_version": SCHEMA_VERSION,
        "cycle_id": cycle_id,
        "event": "cycle_started",
        "at": now,
        "administrative_transition": "abandon_previous_cycle",
    }
    return payload, event


def _load_transition(root: Path) -> dict[str, Any] | None:
    path = _transition_path(root)
    return _read_json(path) if path.exists() else None


def _write_transition(root: Path, value: dict[str, Any]) -> None:
    path = _transition_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, _json(value))


def _apply_from_transition(root: Path, transition: dict[str, Any]) -> dict[str, Any]:
    if transition.get("version") != TRANSITION_VERSION:
        raise CycleAbandonError("unsupported transition version")
    archive = Path(str(transition["archive_dir"]))
    active = Path(root) / "work" / "v2-cycle"
    state_path = active / "state.json"
    journal_path = active / "journal.jsonl"
    new_state_path = archive / "new-state.json"
    new_journal_path = archive / "new-journal.jsonl"
    if not new_state_path.is_file() or not new_journal_path.is_file():
        raise CycleAbandonError("transition is missing prepared new checkpoint")
    atomic_write_text(state_path, new_state_path.read_text(encoding="utf-8"))
    atomic_write_text(journal_path, new_journal_path.read_text(encoding="utf-8"))
    if transition.get("post_115282_excluded"):
        exclusion_path = archive / "quarantine-after.json"
        if not exclusion_path.is_file():
            raise CycleAbandonError("transition is missing prepared post exclusion")
        _apply_exclusion(root, _read_json(exclusion_path))
    transition = dict(transition)
    transition["phase"] = "completed"
    transition["completed_at"] = time.time()
    transition["new_state_sha256"] = _sha256(state_path)
    transition["new_journal_sha256"] = _sha256(journal_path)
    _write_transition(root, transition)
    return {
        "status": "applied",
        "old_cycle_id": transition["old_cycle_id"],
        "old_authorization_id": transition["old_authorization_id"],
        "new_cycle_id": transition["new_cycle_id"],
        "archive_dir": str(archive),
        "post_115282_excluded": bool(transition.get("post_115282_excluded")),
        "transition_phase": "completed",
    }


def abandon_cycle(
    root: Path,
    *,
    cycle_id: str,
    authorization_id: str,
    selected_ids: list[int],
    pending_round: int,
    new_cycle_id: str | None = None,
    exclude_post_id: int | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Preview or apply an idempotent administrative cycle abandonment."""
    root = Path(root)
    store = CycleStateStore(root)
    lock = RunSessionLock(root / "work" / "v2-run.lock")
    if not lock.acquire():
        return {"status": "blocked", "reason": "global_lock_unavailable", "wordpress_writes": 0}
    try:
        transition = _load_transition(root)
        if transition and transition.get("phase") == "completed":
            if transition.get("old_cycle_id") != cycle_id:
                raise CycleAbandonError("a different completed transition is active")
            return {
                "status": "already_applied",
                "old_cycle_id": cycle_id,
                "new_cycle_id": transition.get("new_cycle_id"),
                "archive_dir": transition.get("archive_dir"),
                "post_115282_excluded": bool(transition.get("post_115282_excluded")),
                "wordpress_writes": 0,
            }
        if transition and transition.get("phase") != "completed":
            if transition.get("old_cycle_id") != cycle_id:
                raise CycleAbandonError("unfinished transition belongs to another cycle")
            return _apply_from_transition(root, transition) if apply else {
                "status": "pending_transition",
                "phase": transition.get("phase"),
                "old_cycle_id": cycle_id,
                "new_cycle_id": transition.get("new_cycle_id"),
                "archive_dir": transition.get("archive_dir"),
                "wordpress_writes": 0,
            }

        state = store.load()
        if state is None:
            raise CycleAbandonError("active checkpoint is absent")
        _validate_expected(
            state,
            cycle_id=cycle_id,
            authorization_id=authorization_id,
            selected_ids=selected_ids,
            pending_round=pending_round,
        )
        if new_cycle_id is None:
            new_cycle_id = uuid.uuid4().hex
        if new_cycle_id == cycle_id:
            raise CycleAbandonError("new cycle_id must differ from old cycle_id")
        if exclude_post_id is not None and int(exclude_post_id) != 115282:
            raise CycleAbandonError("only post 115282 may be excluded by this transition")

        checkpoint = root / "work" / "v2-cycle" / "state.json"
        journal = root / "work" / "v2-cycle" / "journal.jsonl"
        if not checkpoint.is_file() or not journal.is_file():
            raise CycleAbandonError("checkpoint/journal files are incomplete")
        token = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
        archive = root / "work" / "v2-cycle" / "archives" / _archive_name(cycle_id, token)
        if archive.exists():
            raise CycleAbandonError("archive destination already exists")
        old_state_hash = _sha256(checkpoint)
        old_journal_hash = _sha256(journal)
        exclusion_before, exclusion_after = _quarantine_payload(
            root, apply_post_id=exclude_post_id
        )
        preview = {
            "status": "preview" if not apply else "preparing",
            "old_cycle_id": cycle_id,
            "old_authorization_id": authorization_id,
            "pending_round": pending_round,
            "selected_ids": [int(value) for value in selected_ids],
            "new_cycle_id": new_cycle_id,
            "post_115282_excluded": exclude_post_id == 115282,
            "old_state_sha256": old_state_hash,
            "old_journal_sha256": old_journal_hash,
            "global_lock": "available",
            "historical_evidence": "preserved",
            "wordpress_writes": 0,
        }
        if not apply:
            return preview

        archive.mkdir(parents=True, exist_ok=False)
        archived_state_hash = _copy_verified(checkpoint, archive / "state.json")
        archived_journal_hash = _copy_verified(journal, archive / "journal.jsonl")
        manifest = {
            "version": TRANSITION_VERSION,
            "archival_status": "ABANDONED_BY_OPERATOR",
            "old_cycle_id": cycle_id,
            "old_authorization_id": authorization_id,
            "pending_round": pending_round,
            "selected_ids": [int(value) for value in selected_ids],
            "post_115282_excluded": exclude_post_id == 115282,
            "round_4_not_falsified": True,
            "old_state_sha256": archived_state_hash,
            "old_journal_sha256": archived_journal_hash,
            "archived_at": time.time(),
            "reason": "operator_requested_cycle_abandonment",
        }
        atomic_write_text(archive / "archive-manifest.json", _json(manifest))
        atomic_write_text(archive / "quarantine-before.json", _json(exclusion_before))
        atomic_write_text(archive / "quarantine-after.json", _json(exclusion_after))
        new_state, new_event = _new_active_payload(new_cycle_id, state.config)
        atomic_write_text(archive / "new-state.json", _json(new_state))
        atomic_write_text(archive / "new-journal.jsonl", _json(new_event))
        transition = {
            "version": TRANSITION_VERSION,
            "phase": "prepared",
            "old_cycle_id": cycle_id,
            "old_authorization_id": authorization_id,
            "new_cycle_id": new_cycle_id,
            "archive_dir": str(archive),
            "post_115282_excluded": exclude_post_id == 115282,
            "created_at": time.time(),
        }
        _write_transition(root, transition)
        if not apply:
            return preview
        return _apply_from_transition(root, transition)
    finally:
        lock.release()
