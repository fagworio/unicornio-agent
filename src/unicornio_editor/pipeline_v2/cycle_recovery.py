"""Fail-closed administrative recovery for a blocked V2 cycle.

The transition archives the complete local evidence for one interrupted cycle,
quarantines only transactions whose effects are uncertain, and starts a new
cycle without touching WordPress.  It is intentionally separate from the
legacy ``v2-cycle-abandon`` contract.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..backup import atomic_write_text
from .cycle_state import CycleState, CycleStateStore, SCHEMA_VERSION
from .lock import RunSessionLock


class CycleRecoveryError(RuntimeError):
    """The requested recovery cannot be proven safe."""


TRANSITION_VERSION = 1
TRANSITION_NAME = "v2-cycle-recovery-transition.json"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise CycleRecoveryError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise CycleRecoveryError(f"JSON object required: {path}")
    return value


def _transition_path(root: Path) -> Path:
    return root / "work" / "v2-cycle" / TRANSITION_NAME


def _copy_verified(source: Path, destination: Path, files: dict[str, str]) -> None:
    if source.is_symlink() or not source.is_file():
        raise CycleRecoveryError(f"evidence source is not a regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    digest = _sha256(source)
    if _sha256(destination) != digest:
        raise CycleRecoveryError(f"archive hash mismatch: {source}")
    files[str(destination)] = digest


def _post_files(root: Path, post_id: int) -> list[Path]:
    """Return post-scoped evidence without following symlinks."""
    candidates: list[Path] = []
    for base in (root / "work" / "v2-editorial", root / "backups"):
        directory = base / str(post_id)
        if directory.is_dir():
            candidates.extend(p for p in directory.rglob("*") if p.is_file() and not p.is_symlink())
    for base in (root / "work" / "v2-journal", root / "work" / "locks"):
        path = base / f"{post_id}.json" if base.name == "v2-journal" else base / f"{post_id}.lock"
        if path.is_file() and not path.is_symlink():
            candidates.append(path)
    batches = root / "work" / "batches"
    if batches.is_dir():
        for directory in batches.iterdir():
            if directory.is_dir() and str(post_id) in directory.name:
                candidates.extend(p for p in directory.rglob("*") if p.is_file() and not p.is_symlink())
    return sorted(set(candidates))


def _validate_policy(root: Path) -> dict[str, Any]:
    path = root / "config" / "v2-quarantine.json"
    if path.is_symlink() or not path.is_file():
        raise CycleRecoveryError("quarantine policy is absent or symlinked")
    payload = _read_json(path)
    if payload.get("version") != 1 or not isinstance(payload.get("posts"), list) or not isinstance(payload.get("release_history"), list):
        raise CycleRecoveryError("quarantine policy schema is invalid")
    seen: set[int] = set()
    for row in payload["posts"]:
        if not isinstance(row, dict) or type(row.get("post_id")) is not int:
            raise CycleRecoveryError("quarantine policy contains an invalid post")
        if row["post_id"] in seen:
            raise CycleRecoveryError("quarantine policy contains duplicate post IDs")
        seen.add(row["post_id"])
    return payload


def _build_new_state(cycle_id: str, config: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
    state = CycleState(cycle_id=cycle_id, config=dict(config), started_at=now, updated_at=now)
    return state.to_dict(), {
        "schema_version": SCHEMA_VERSION,
        "cycle_id": cycle_id,
        "event": "cycle_started",
        "at": now,
        "administrative_transition": "recover_blocked_cycle_without_replay",
    }


def _validate_inputs(
    state: CycleState,
    *,
    cycle_id: str,
    authorization_id: str,
    pending_round: int,
    selected_ids: list[int],
    uncertain_ids: list[int],
    not_executed_ids: list[int],
    run_ids: Mapping[int, str],
) -> None:
    if state.cycle_id != cycle_id or state.status != "running" or state.phase != "ADMISSION":
        raise CycleRecoveryError("active checkpoint is not the expected running ADMISSION cycle")
    pending = state.pending_round
    if not isinstance(pending, dict) or int(pending.get("round_number", -1)) != int(pending_round):
        raise CycleRecoveryError("pending round does not match active checkpoint")
    if str(pending.get("authorization_id") or "") != authorization_id:
        raise CycleRecoveryError("authorization identity does not match active checkpoint")
    expected = [int(v) for v in (pending.get("preview_selected_ids") or [])]
    if expected != selected_ids or len(expected) != len(set(expected)):
        raise CycleRecoveryError("selected IDs do not match the authorized round")
    if set(uncertain_ids) & set(not_executed_ids) or set(uncertain_ids) | set(not_executed_ids) != set(selected_ids):
        raise CycleRecoveryError("every selected post must have exactly one reconciliation class")
    for post_id in uncertain_ids:
        if not run_ids.get(post_id):
            raise CycleRecoveryError(f"uncertain post {post_id} requires its exact run_id")


def recover_blocked_cycle(
    root: Path,
    *,
    cycle_id: str,
    authorization_id: str,
    pending_round: int,
    selected_ids: Iterable[int],
    uncertain_post_ids: Iterable[int],
    not_executed_post_ids: Iterable[int],
    run_ids: Mapping[int, str],
    operator_authorization: str,
    new_cycle_id: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Preview/apply a no-WordPress-write recovery transition.

    ``uncertain_post_ids`` are archived and quarantined. ``not_executed`` IDs
    are archived as historical non-execution evidence and remain eligible for
    a future cycle. No post journal or WordPress state is rewritten.
    """
    root = Path(root)
    selected = [int(v) for v in selected_ids]
    uncertain = sorted({int(v) for v in uncertain_post_ids})
    not_executed = sorted({int(v) for v in not_executed_post_ids})
    new_cycle_id = str(new_cycle_id or uuid.uuid4().hex)
    if not operator_authorization.strip():
        raise CycleRecoveryError("explicit operator authorization is required")
    if new_cycle_id == cycle_id:
        raise CycleRecoveryError("new cycle ID must differ from old cycle ID")
    lock = RunSessionLock(root / "work" / "v2-run.lock")
    if not lock.acquire():
        return {"status": "blocked", "reason": "global_lock_unavailable", "wordpress_writes": 0}
    try:
        state_store = CycleStateStore(root)
        state = state_store.load()
        if state is None:
            raise CycleRecoveryError("active checkpoint is absent")
        _validate_inputs(state, cycle_id=cycle_id, authorization_id=authorization_id,
                         pending_round=pending_round, selected_ids=selected,
                         uncertain_ids=uncertain, not_executed_ids=not_executed,
                         run_ids=run_ids)
        for post_id in uncertain:
            journal = root / "work" / "v2-journal" / f"{post_id}.json"
            if not journal.is_file() or journal.is_symlink():
                raise CycleRecoveryError(f"uncertain post {post_id} has no immutable journal")
            data = _read_json(journal)
            expected = {"post_id": post_id, "run_id": str(run_ids[post_id]),
                        "cycle_id": cycle_id, "cycle_round": int(pending_round),
                        "authorization_id": authorization_id}
            for key, value in expected.items():
                if data.get(key) != value:
                    raise CycleRecoveryError(f"uncertain post {post_id} journal identity mismatch: {key}")
            if data.get("readback") is True and data.get("status") == "committed" and data.get("writer_started") is not True:
                raise CycleRecoveryError(f"uncertain post {post_id} has contradictory commit evidence")
        policy = _validate_policy(root)
        existing = {int(row["post_id"]): row for row in policy["posts"]}
        for post_id in uncertain:
            row = existing.get(post_id)
            reason = ("uncertain_effects_preserved; cycle=" + cycle_id + "; round=" + str(pending_round)
                      + "; run=" + str(run_ids[post_id]) + "; authorization=" + authorization_id)
            if row and (row.get("reason_code") != "recovery_uncertain_effects" or reason not in str(row.get("reason"))):
                raise CycleRecoveryError(f"post {post_id} already has a different quarantine entry")
        preview = {
            "status": "preview",
            "old_cycle_id": cycle_id,
            "old_authorization_id": authorization_id,
            "pending_round": int(pending_round),
            "selected_ids": selected,
            "uncertain_post_ids": uncertain,
            "not_executed_post_ids": not_executed,
            "new_cycle_id": new_cycle_id,
            "operator_authorization": operator_authorization,
            "wordpress_writes": 0,
            "replay": False,
        }
        if not apply:
            return preview
        archive = root / "work" / "v2-cycle" / "archives" / f"recovery-{cycle_id}-{new_cycle_id}"
        if archive.exists():
            raise CycleRecoveryError("archive destination already exists")
        archive.mkdir(parents=True, exist_ok=False)
        files: dict[str, str] = {}
        sources = [root / "work" / "v2-cycle" / "state.json", root / "work" / "v2-cycle" / "journal.jsonl",
                   root / "config" / "v2-quarantine.json"]
        for source in sources:
            _copy_verified(source, archive / "cycle" / source.name, files)
        for post_id in selected:
            for source in _post_files(root, post_id):
                relative = source.relative_to(root)
                _copy_verified(source, archive / "evidence" / relative, files)
        for source in sorted((root / "work").glob("media_registry.sqlite*")):
            if source.is_file() and not source.is_symlink():
                _copy_verified(source, archive / "evidence" / "work" / source.name, files)
        old_state_hash = _sha256(root / "work" / "v2-cycle" / "state.json")
        old_journal_hash = _sha256(root / "work" / "v2-cycle" / "journal.jsonl")
        for post_id in uncertain:
            if post_id not in existing:
                policy["posts"].append({
                    "post_id": post_id,
                    "reason_code": "recovery_uncertain_effects",
                    "reason": "uncertain effects preserved; administrative recovery required before any automated write; cycle=" + cycle_id + "; run=" + str(run_ids[post_id]) + "; authorization=" + authorization_id,
                })
        policy["posts"] = sorted(policy["posts"], key=lambda row: int(row["post_id"]))
        new_state, new_event = _build_new_state(new_cycle_id, state.config)
        atomic_write_text(archive / "new-state.json", _json(new_state))
        atomic_write_text(archive / "new-journal.jsonl", _json(new_event))
        manifest = {**preview, "status": "prepared", "archive_dir": str(archive),
                    "old_state_sha256": old_state_hash, "old_journal_sha256": old_journal_hash,
                    "archived_files": files, "quarantine_after": policy,
                    "created_at": time.time()}
        atomic_write_text(archive / "archive-manifest.json", _json(manifest))
        atomic_write_text(archive / "quarantine-after.json", _json(policy))
        atomic_write_text(root / "work" / "v2-cycle" / "state.json", _json(new_state))
        atomic_write_text(root / "work" / "v2-cycle" / "journal.jsonl", _json(new_event))
        atomic_write_text(root / "config" / "v2-quarantine.json", _json(policy))
        transition = {**manifest, "status": "completed", "completed_at": time.time(),
                      "new_state_sha256": _sha256(root / "work" / "v2-cycle" / "state.json"),
                      "new_journal_sha256": _sha256(root / "work" / "v2-cycle" / "journal.jsonl")}
        atomic_write_text(_transition_path(root), _json(transition))
        return {"status": "applied", "old_cycle_id": cycle_id, "new_cycle_id": new_cycle_id,
                "archive_dir": str(archive), "quarantined_post_ids": uncertain,
                "not_executed_post_ids": not_executed, "wordpress_writes": 0,
                "replay": False}
    finally:
        lock.release()


__all__ = ["CycleRecoveryError", "recover_blocked_cycle"]
