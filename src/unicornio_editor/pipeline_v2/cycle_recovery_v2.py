"""Evidence-gated, resumable recovery of an interrupted V2 cycle.

Separate from ``v2-cycle-abandon`` and single-transaction isolation. Reads
WordPress and MediaRegistry, archives evidence, quarantines only unresolved
effects, and activates a fresh cycle without replaying Writer or uploads.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..backup import atomic_write_text
from .cycle_state import CycleState, SCHEMA_VERSION
from .lock import RunSessionLock
from .transaction_admin import _is_identityless_terminal_journal, _round_post_evidence


class CycleRecoveryError(RuntimeError):
    """The requested recovery cannot be proven safe."""


TRANSITION_VERSION = 3
CLASSIFICATIONS = {"COMMITTED_NO_REPLAY", "NOT_EXECUTED", "UNCERTAIN_ISOLATED", "PRE_WRITER_RECOVERABLE"}


def _not_executed_evidence_is_complete(item: Mapping[str, Any]) -> bool:
    """Accept no-write proof for a legacy terminal journal without identity."""
    if item.get("classification") != "NOT_EXECUTED":
        return False
    journal = item.get("journal")
    if not isinstance(journal, Mapping) or journal.get("present") is not True:
        return True
    return (
        _is_identityless_terminal_journal(journal)
        and isinstance(item.get("no_write_evidence"), Mapping)
        and item["no_write_evidence"].get("proof_of_absence") is True
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_value(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


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


def _operation_id(*, cycle_id: str, authorization_id: str, pending_round: int, selected: list[int], uncertain: list[int], new_cycle_id: str) -> str:
    return _hash_value({
        "cycle_id": cycle_id,
        "authorization_id": authorization_id,
        "pending_round": int(pending_round),
        "selected_ids": selected,
        "uncertain_post_ids": uncertain,
        "new_cycle_id": new_cycle_id,
    })[:24]


def _transition_path(root: Path, isolation_id: str) -> Path:
    return root / "work" / "v2-cycle" / "transitions" / f"{isolation_id}.json"


def _write_transition(path: Path, payload: Mapping[str, Any]) -> None:
    if path.is_symlink():
        raise CycleRecoveryError(f"transition marker is a symlink: {path}")
    if path.exists():
        existing = _read_json(path)
        for key in ("isolation_id", "cycle_id", "authorization_id", "pending_round", "selected_ids", "new_cycle_id"):
            if key in existing and existing.get(key) != payload.get(key):
                raise CycleRecoveryError(f"transition identity diverged for {key}")
    atomic_write_text(path, _json(dict(payload)))


def _write_once(path: Path, content: str) -> None:
    if path.is_symlink():
        raise CycleRecoveryError(f"prepared path is a symlink: {path}")
    if path.exists():
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            raise CycleRecoveryError(f"prepared artifact diverged: {path}")
        return
    atomic_write_text(path, content)

def _copy_verified(source: Path, destination: Path, hashes: dict[str, str]) -> None:
    if source.is_symlink() or not source.is_file():
        raise CycleRecoveryError(f"evidence source is not a regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = _sha256(source)
    if destination.exists():
        if destination.is_symlink() or not destination.is_file() or _sha256(destination) != digest:
            raise CycleRecoveryError(f"archive artifact diverged: {destination}")
    else:
        shutil.copy2(source, destination)
    if _sha256(destination) != digest:
        raise CycleRecoveryError(f"archive hash mismatch: {source}")
    hashes[str(destination)] = digest


def _post_files(root: Path, post_id: int) -> list[Path]:
    paths: set[Path] = set()
    for base in (root / "work" / "v2-editorial", root / "backups"):
        directory = base / str(post_id)
        if directory.is_dir():
            paths.update(p for p in directory.rglob("*") if p.is_file() and not p.is_symlink())
    journal = root / "work" / "v2-journal" / f"{post_id}.json"
    if journal.is_file() and not journal.is_symlink():
        paths.add(journal)
    batches = root / "work" / "batches"
    if batches.is_dir():
        for directory in batches.iterdir():
            if directory.is_dir() and str(post_id) in directory.name:
                paths.update(p for p in directory.rglob("*") if p.is_file() and not p.is_symlink())
    return sorted(paths)


def _registry_snapshot(root: Path, archive: Path) -> dict[str, Any]:
    """Make a consistent SQLite backup; unavailable is never reported as zero."""
    source = root / "work" / "media_registry.sqlite"
    if not source.is_file() or source.is_symlink():
        return {"status": "UNAVAILABLE", "reason": "registry_file_missing", "logical_fingerprint": None}
    destination = archive / "registry" / "media_registry.sqlite"
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_db:
            if source_db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return {"status": "UNAVAILABLE", "reason": "registry_integrity_failed", "logical_fingerprint": None}
            if not destination.exists():
                with sqlite3.connect(destination) as destination_db:
                    source_db.backup(destination_db)
            else:
                with sqlite3.connect(f"file:{destination}?mode=ro", uri=True) as archived_db:
                    if archived_db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        return {"status": "UNAVAILABLE", "reason": "archived_registry_integrity_failed", "logical_fingerprint": None}
            rows = []
            for table in ("media_runs", "media_operations", "media_decisions", "media_usage"):
                try:
                    columns = [row[1] for row in source_db.execute(f"PRAGMA table_info({table})")]
                    data = [dict(zip(columns, row)) for row in source_db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                except sqlite3.Error as exc:
                    return {"status": "UNAVAILABLE", "reason": f"registry_schema_error:{table}:{type(exc).__name__}", "logical_fingerprint": None}
                rows.append({"table": table, "rows": data})
            logical_fingerprint = _hash_value(rows)
            if destination.exists():
                with sqlite3.connect(f"file:{destination}?mode=ro", uri=True) as archived_db:
                    archived_rows = []
                    for table in ("media_runs", "media_operations", "media_decisions", "media_usage"):
                        columns = [row[1] for row in archived_db.execute(f"PRAGMA table_info({table})")]
                        data = [dict(zip(columns, row)) for row in archived_db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                        archived_rows.append({"table": table, "rows": data})
                if _hash_value(archived_rows) != logical_fingerprint:
                    return {"status": "UNAVAILABLE", "reason": "archived_registry_logical_hash_mismatch", "logical_fingerprint": None}
            return {"status": "AVAILABLE", "integrity": "ok", "logical_fingerprint": logical_fingerprint,
                    "backup": str(destination), "row_counts": {row["table"]: len(row["rows"]) for row in rows}}
    except (OSError, sqlite3.Error) as exc:
        return {"status": "UNAVAILABLE", "reason": f"registry_read_error:{type(exc).__name__}", "logical_fingerprint": None}


def _policy(root: Path) -> dict[str, Any]:
    path = root / "config" / "v2-quarantine.json"
    if path.is_symlink() or not path.is_file():
        raise CycleRecoveryError("quarantine policy is absent or symlinked")
    payload = _read_json(path)
    if payload.get("version") != 1 or not isinstance(payload.get("posts"), list) or not isinstance(payload.get("release_history"), list):
        raise CycleRecoveryError("quarantine policy schema is invalid")
    ids: set[int] = set()
    for row in payload["posts"]:
        if not isinstance(row, dict) or type(row.get("post_id")) is not int or row["post_id"] in ids:
            raise CycleRecoveryError("quarantine policy contains an invalid or duplicate post")
        if not isinstance(row.get("reason_code"), str) or not isinstance(row.get("reason"), str):
            raise CycleRecoveryError("quarantine policy contains an invalid reason")
        ids.add(row["post_id"])
    return payload


def _ids(values: Iterable[int]) -> list[int]:
    result = [int(value) for value in values]
    if len(result) != len(set(result)):
        raise CycleRecoveryError("post IDs must be unique")
    return result


def _new_state(cycle_id: str, config: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
    state = CycleState(cycle_id=cycle_id, config=dict(config), started_at=now, updated_at=now)
    return state.to_dict(), {"schema_version": SCHEMA_VERSION, "cycle_id": cycle_id, "event": "cycle_started", "at": now,
                             "administrative_transition": "recover_blocked_cycle_without_replay"}


def _class_sets(evidence: list[Mapping[str, Any]]) -> dict[str, list[int]]:
    result = {name: [] for name in CLASSIFICATIONS}
    for item in evidence:
        classification = str(item.get("classification") or "UNCERTAIN_ISOLATED")
        if classification not in CLASSIFICATIONS:
            raise CycleRecoveryError(f"unknown transaction classification: {classification}")
        result[classification].append(int(item["post_id"]))
    return {key: sorted(value) for key, value in result.items()}


def _validate_partition(actual: Mapping[str, list[int]], declared: Mapping[str, list[int]], selected: list[int]) -> None:
    for name in CLASSIFICATIONS:
        if sorted(actual.get(name, [])) != sorted(declared.get(name, [])):
            raise CycleRecoveryError(f"declared {name} does not match live evidence")
    flattened = [post_id for values in declared.values() for post_id in values]
    if sorted(flattened) != sorted(selected):
        raise CycleRecoveryError("classification partition does not cover the selection exactly")


def _quarantine_after(before: Mapping[str, Any], uncertain: list[int], isolation_id: str) -> dict[str, Any]:
    after = deepcopy(before)
    existing = {int(row["post_id"]): row for row in after["posts"]}
    for post_id in uncertain:
        expected = {"post_id": post_id, "reason_code": "uncertain_transaction", "reason": f"UNCERTAIN_ISOLATED {isolation_id}; no replay until independently resolved"}
        if post_id in existing and existing[post_id] != expected:
            raise CycleRecoveryError(f"post {post_id} already has a different quarantine reason")
        if post_id not in existing:
            after["posts"].append(expected)
    return after


def _validate_active(root: Path, cycle_id: str, authorization_id: str, pending_round: int, selected: list[int]) -> tuple[dict[str, Any], str, str, str]:
    state_path = root / "work" / "v2-cycle" / "state.json"
    journal_path = root / "work" / "v2-cycle" / "journal.jsonl"
    state = _read_json(state_path)
    pending = state.get("pending_round")
    if state.get("cycle_id") != cycle_id or state.get("status") != "running" or state.get("phase") != "ADMISSION":
        raise CycleRecoveryError("active checkpoint is not the expected running ADMISSION cycle")
    if not isinstance(pending, Mapping) or int(pending.get("round_number", -1)) != pending_round:
        raise CycleRecoveryError("pending round does not match active checkpoint")
    if str(pending.get("authorization_id") or "") != authorization_id:
        raise CycleRecoveryError("authorization identity does not match active checkpoint")
    if [int(v) for v in (pending.get("preview_selected_ids") or [])] != selected:
        raise CycleRecoveryError("selected IDs do not match active checkpoint")
    return state, _sha256(state_path), _sha256(journal_path), _hash_value(_read_json(root / "config" / "v2-quarantine.json"))


def _verify_completed(root: Path, transition: Mapping[str, Any]) -> None:
    _validate_prepared_archive(root, transition)
    archive = Path(str(transition.get("archive_dir") or ""))
    if transition.get("new_state_sha256") != _file_hash(archive / "new-state.json") or transition.get("new_journal_sha256") != _file_hash(archive / "new-journal.jsonl"):
        raise CycleRecoveryError("completed transition snapshot hash diverged")


def _file_hash(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise CycleRecoveryError(f"required snapshot is missing or symlinked: {path}")
    return _sha256(path)


def _validate_prepared_archive(root: Path, transition: Mapping[str, Any]) -> tuple[str, str, str, str, str, str]:
    archive = Path(str(transition.get("archive_dir") or ""))
    manifest_path = archive / "archive-manifest.json"
    manifest = _read_json(manifest_path)
    if transition.get("archive_manifest_sha256") and _file_hash(manifest_path) != transition["archive_manifest_sha256"]:
        raise CycleRecoveryError("archive manifest hash diverged")
    after_path = archive / "quarantine-after.json"
    new_state_path = archive / "new-state.json"
    new_journal_path = archive / "new-journal.jsonl"
    after_hash = _hash_value(_read_json(after_path))
    if after_hash != transition.get("quarantine_after_hash") or after_hash != _hash_value(manifest.get("quarantine_after")):
        raise CycleRecoveryError("quarantine snapshot hash diverged")
    new_state_hash = _file_hash(new_state_path)
    new_journal_hash = _file_hash(new_journal_path)
    if new_state_hash != transition.get("new_state_prepared_sha256") or new_journal_hash != transition.get("new_journal_prepared_sha256"):
        raise CycleRecoveryError("prepared checkpoint snapshot hash diverged")
    for name, expected in (manifest.get("archive_file_hashes") or {}).items():
        path = Path(name)
        if not path.is_absolute():
            path = archive / name
        if _file_hash(path) != expected:
            raise CycleRecoveryError(f"archived evidence hash diverged: {path}")
    return (str(transition["old_state_sha256"]), str(transition["old_journal_sha256"]), str(transition["quarantine_before_sha256"]), after_hash, new_state_hash, new_journal_hash)


def _validate_resume_phase(root: Path, transition: Mapping[str, Any]) -> tuple[str, str, str, str, str, str]:
    old_state, old_journal, old_quarantine, after, new_state, new_journal = _validate_prepared_archive(root, transition)
    current_state = _file_hash(root / "work" / "v2-cycle" / "state.json")
    current_journal = _file_hash(root / "work" / "v2-cycle" / "journal.jsonl")
    current_quarantine = _hash_value(_policy(root))
    phase = transition.get("phase")
    allowed = {
        "prepared": ({old_state}, {old_journal}, {old_quarantine, after}),
        "quarantine_applied": ({old_state, new_state}, {old_journal}, {after}),
        "checkpoint_applied": ({new_state}, {old_journal, new_journal}, {after}),
        "journal_applied": ({new_state}, {new_journal}, {after}),
    }
    if phase not in allowed:
        raise CycleRecoveryError(f"unsupported resumable transition phase: {phase}")
    states, journals, quarantines = allowed[phase]
    if current_state not in states or current_journal not in journals or current_quarantine not in quarantines:
        raise CycleRecoveryError("active state diverged from the allowed hashes for the recorded phase")
    return old_state, old_journal, current_quarantine, after, new_state, new_journal

def _resume_transition(root: Path, transition: Mapping[str, Any]) -> dict[str, Any]:
    """Finish a prepared transition after a crash without recapturing evidence."""
    archive = Path(str(transition.get("archive_dir") or ""))
    _validate_resume_phase(root, transition)
    after_path = archive / "quarantine-after.json"
    current = dict(transition)
    phase = str(current.get("phase") or "")
    old_state, old_journal, current_quarantine, after, new_state, new_journal = _validate_resume_phase(root, current)
    state_path = root / "work" / "v2-cycle" / "state.json"
    journal_path = root / "work" / "v2-cycle" / "journal.jsonl"
    quarantine_path = root / "config" / "v2-quarantine.json"
    if phase == "prepared":
        if current_quarantine != after:
            atomic_write_text(quarantine_path, after_path.read_text(encoding="utf-8"))
        current["phase"] = "quarantine_applied"
        _write_transition(_transition_path(root, str(current["isolation_id"])), current)
        phase = "quarantine_applied"
    if phase == "quarantine_applied":
        state_hash = _file_hash(state_path)
        if state_hash == old_state:
            atomic_write_text(state_path, (archive / "new-state.json").read_text(encoding="utf-8"))
        elif state_hash != new_state:
            raise CycleRecoveryError("active checkpoint diverged before resume")
        current["phase"] = "checkpoint_applied"
        _write_transition(_transition_path(root, str(current["isolation_id"])), current)
        phase = "checkpoint_applied"
    if phase == "checkpoint_applied":
        journal_hash = _file_hash(journal_path)
        if journal_hash == old_journal:
            atomic_write_text(journal_path, (archive / "new-journal.jsonl").read_text(encoding="utf-8"))
        elif journal_hash != new_journal:
            raise CycleRecoveryError("active journal diverged before resume")
        current["phase"] = "journal_applied"
        _write_transition(_transition_path(root, str(current["isolation_id"])), current)
        phase = "journal_applied"
    if phase == "journal_applied":
        current["phase"] = "completed"
        current["new_state_sha256"] = new_state
        current["new_journal_sha256"] = new_journal
        _write_transition(_transition_path(root, str(current["isolation_id"])), current)
    _verify_completed(root, current)
    return {"status": "applied", "isolation_id": current["isolation_id"], "new_cycle_id": current["new_cycle_id"], "archive_dir": str(archive), "wordpress_writes": 0, "replay": False}

def recover_blocked_cycle(
    root: Path,
    client,
    *,
    cycle_id: str,
    authorization_id: str,
    pending_round: int,
    selected_ids: Iterable[int],
    committed_post_ids: Iterable[int],
    uncertain_post_ids: Iterable[int],
    not_executed_post_ids: Iterable[int],
    pre_writer_recoverable_post_ids: Iterable[int] = (),
    run_ids: Mapping[int, str],
    operator_authorization: str,
    new_cycle_id: str | None = None,
    preview: Mapping[str, Any] | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Preview/apply a five-way evidence-gated, no-WordPress-write recovery."""
    root = Path(root)
    selected = _ids(selected_ids)
    declared = {"COMMITTED_NO_REPLAY": _ids(committed_post_ids), "UNCERTAIN_ISOLATED": _ids(uncertain_post_ids),
                "NOT_EXECUTED": _ids(not_executed_post_ids), "PRE_WRITER_RECOVERABLE": _ids(pre_writer_recoverable_post_ids)}
    new_cycle_id = str(new_cycle_id or uuid.uuid4().hex)
    if not operator_authorization.strip() or new_cycle_id == cycle_id:
        raise CycleRecoveryError("explicit operator authorization and a distinct new cycle are required")
    isolation_id = _operation_id(cycle_id=cycle_id, authorization_id=authorization_id, pending_round=int(pending_round), selected=selected, uncertain=declared["UNCERTAIN_ISOLATED"], new_cycle_id=new_cycle_id)
    lock = RunSessionLock(root / "work" / "v2-run.lock")
    if not lock.acquire():
        return {"status": "blocked", "reason": "global_lock_unavailable", "wordpress_writes": 0}
    try:
        transition_path = _transition_path(root, isolation_id)
        existing = _read_json(transition_path) if transition_path.is_file() else None
        if existing and existing.get("phase") == "completed":
            if (existing.get("cycle_id") != cycle_id or existing.get("authorization_id") != authorization_id
                    or int(existing.get("pending_round", -1)) != int(pending_round)
                    or existing.get("selected_ids") != selected or existing.get("new_cycle_id") != new_cycle_id
                    or existing.get("isolation_id") != isolation_id):
                raise CycleRecoveryError("a completed transition exists for a different recovery operation")
            _verify_completed(root, existing)
            return {"status": "already_applied", "isolation_id": existing["isolation_id"], "new_cycle_id": existing["new_cycle_id"], "archive_dir": existing["archive_dir"], "wordpress_writes": 0, "replay": False}
        if existing and existing.get("phase") in {"prepared", "quarantine_applied", "checkpoint_applied", "journal_applied"}:
            if (existing.get("cycle_id") != cycle_id or existing.get("authorization_id") != authorization_id
                    or int(existing.get("pending_round", -1)) != int(pending_round)
                    or existing.get("selected_ids") != selected):
                raise CycleRecoveryError("unfinished transition identity diverged")
            return _resume_transition(root, existing)
        state, state_hash, journal_hash, quarantine_hash = _validate_active(root, cycle_id, authorization_id, int(pending_round), selected)
        policy = _policy(root)
        evidence: list[dict[str, Any]] = []
        for post_id in selected:
            run_id = str(run_ids.get(post_id) or "")
            if post_id not in declared["NOT_EXECUTED"] and not run_id:
                raise CycleRecoveryError(f"post {post_id} requires its exact run_id")
            item = dict(_round_post_evidence(root, client, post_id=post_id, cycle_id=cycle_id, authorization_id=authorization_id, pending_round=int(pending_round), capture_media_artifacts=post_id in declared["UNCERTAIN_ISOLATED"]))
            item["run_id_declared"] = run_id
            observed_run_id = str((item.get("journal") or {}).get("run_id") or "")
            if post_id in declared["NOT_EXECUTED"]:
                if run_id or not _not_executed_evidence_is_complete(item):
                    raise CycleRecoveryError(f"post {post_id} is not proven not-executed")
            elif not run_id or run_id != observed_run_id:
                raise CycleRecoveryError(f"declared run_id diverges from journal for post {post_id}")
            evidence.append(item)
        actual = _class_sets(evidence)
        _validate_partition(actual, declared, selected)
        if any(item.get("registry", {}).get("present") is not True for item in evidence):
            raise CycleRecoveryError("MediaRegistry evidence unavailable; refusing classification")
        classes = {str(item["post_id"]): item["classification"] for item in evidence}
        isolation_id = _operation_id(cycle_id=cycle_id, authorization_id=authorization_id, pending_round=int(pending_round), selected=selected, uncertain=declared["UNCERTAIN_ISOLATED"], new_cycle_id=new_cycle_id)
        snapshot = {"cycle_id": cycle_id, "authorization_id": authorization_id, "pending_round": int(pending_round), "selected_ids": selected, "classes": classes, "evidence": evidence, "state_sha256": state_hash, "journal_sha256": journal_hash, "quarantine_sha256": quarantine_hash, "new_cycle_id": new_cycle_id, "isolation_id": isolation_id}
        snapshot_hash = _hash_value(snapshot)
        result = {"status": "preview", "classification": classes, "round_classifications": actual, "snapshot_hash": snapshot_hash, "isolation_id": isolation_id, "selected_ids": selected, "wordpress_writes": 0, "replay": False, "eligible_for_apply": True, "evidence": evidence, "archive_dir": str(root / "work" / "v2-cycle" / "archives" / f"recovery-{cycle_id}-{new_cycle_id}")}
        if not apply:
            return result
        if not isinstance(preview, Mapping) or preview.get("snapshot_hash") != snapshot_hash or preview.get("evidence") != evidence or preview.get("round_classifications") != actual:
            raise CycleRecoveryError("apply requires the exact fresh preview evidence")
        if existing and existing.get("isolation_id") != isolation_id:
            raise CycleRecoveryError("unfinished transition belongs to another recovery")
        transition_path.parent.mkdir(parents=True, exist_ok=True)
        transition = dict(existing or {})
        transition.update({"version": TRANSITION_VERSION, "phase": transition.get("phase", "intent"), "isolation_id": isolation_id, "cycle_id": cycle_id, "authorization_id": authorization_id, "pending_round": int(pending_round), "selected_ids": selected, "new_cycle_id": new_cycle_id, "archive_dir": result["archive_dir"], "snapshot_hash": snapshot_hash, "classes": classes, "operator_authorization": operator_authorization, "created_at": transition.get("created_at", time.time())})
        _write_transition(transition_path, transition)
        archive = Path(result["archive_dir"]); archive.mkdir(parents=True, exist_ok=True)
        hashes: dict[str, str] = {}
        for source in (root / "work" / "v2-cycle" / "state.json", root / "work" / "v2-cycle" / "journal.jsonl", root / "config" / "v2-quarantine.json"):
            _copy_verified(source, archive / "cycle" / source.name, hashes)
        for post_id in selected:
            for source in _post_files(root, post_id):
                _copy_verified(source, archive / "evidence" / source.relative_to(root), hashes)
        registry_info = _registry_snapshot(root, archive)
        if registry_info.get("status") != "AVAILABLE":
            raise CycleRecoveryError("MediaRegistry snapshot unavailable; refusing transition")
        after = _quarantine_after(policy, declared["UNCERTAIN_ISOLATED"], isolation_id)
        new_state, new_event = _new_state(new_cycle_id, state.get("config") or {})
        if (archive / "new-state.json").is_file():
            new_state = _read_json(archive / "new-state.json")
        if (archive / "new-journal.jsonl").is_file():
            new_event = _read_json(archive / "new-journal.jsonl")
        _write_once(archive / "new-state.json", _json(new_state))
        _write_once(archive / "new-journal.jsonl", _json(new_event))
        _write_once(archive / "quarantine-after.json", _json(after))
        archive_files = {str(path): _sha256(path) for path in archive.rglob("*") if path.is_file() and path.name != "archive-manifest.json"}
        manifest = {**transition, "phase": "prepared", "archive_hashes": hashes, "archive_file_hashes": archive_files, "registry_snapshot": registry_info, "reconciliation_evidence": evidence, "quarantine_before": policy, "quarantine_after": after, "old_state_sha256": state_hash, "old_journal_sha256": journal_hash, "quarantine_before_sha256": quarantine_hash, "quarantine_after_hash": _hash_value(after), "new_state_prepared_sha256": _sha256(archive / "new-state.json"), "new_journal_prepared_sha256": _sha256(archive / "new-journal.jsonl")}
        _write_once(archive / "archive-manifest.json", _json(manifest))
        transition = manifest
        transition["archive_manifest_sha256"] = _sha256(archive / "archive-manifest.json")
        _write_transition(transition_path, transition)
        if transition["phase"] == "prepared":
            _validate_resume_phase(root, transition)
            if _hash_value(_policy(root)) != transition["quarantine_after_hash"]:
                atomic_write_text(root / "config" / "v2-quarantine.json", _json(after))
            transition["phase"] = "quarantine_applied"; _write_transition(transition_path, transition)
        if transition["phase"] == "quarantine_applied":
            _validate_resume_phase(root, transition)
            if _sha256(root / "work" / "v2-cycle" / "state.json") != transition["new_state_prepared_sha256"]:
                atomic_write_text(root / "work" / "v2-cycle" / "state.json", _json(new_state))
            transition["phase"] = "checkpoint_applied"; _write_transition(transition_path, transition)
        if transition["phase"] == "checkpoint_applied":
            _validate_resume_phase(root, transition)
            if _sha256(root / "work" / "v2-cycle" / "journal.jsonl") != transition["new_journal_prepared_sha256"]:
                atomic_write_text(root / "work" / "v2-cycle" / "journal.jsonl", _json(new_event))
            transition["phase"] = "journal_applied"; _write_transition(transition_path, transition)
        if transition["phase"] == "journal_applied":
            _validate_resume_phase(root, transition)
            transition["phase"] = "completed"; transition["new_state_sha256"] = transition["new_state_prepared_sha256"]; transition["new_journal_sha256"] = transition["new_journal_prepared_sha256"]; _write_transition(transition_path, transition)
        _verify_completed(root, transition)
        return {"status": "applied", "isolation_id": isolation_id, "new_cycle_id": new_cycle_id, "archive_dir": str(archive), "classes": classes, "wordpress_writes": 0, "replay": False}
    finally:
        lock.release()


__all__ = ["CycleRecoveryError", "recover_blocked_cycle"]
