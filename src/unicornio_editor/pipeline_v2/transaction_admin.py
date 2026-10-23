"""Auditable isolation of one uncertain V2 writer transaction.

This module changes only local V2 admission/checkpoint policy.  It never writes
WordPress and never turns an uncertain journal into a committed journal.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping

from ..backup import atomic_write_text
from .cycle_state import CycleStateStore
from .lock import RunSessionLock


ISOLATION_VERSION = 1
ISOLATION_EVENT = "transaction_uncertain_isolated"
ISOLATION_REASON = "uncertain_transaction"


class TransactionIsolationError(RuntimeError):
    """The requested uncertain-transaction isolation is unsafe."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hash_file(path: Path) -> str:
    return _hash_bytes(path.read_bytes())


def _hash_value(value: Any) -> str:
    return _hash_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise TransactionIsolationError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise TransactionIsolationError(f"JSON object required: {path}")
    return value


def _copy_verified(source: Path, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    digest = _hash_file(source)
    if _hash_file(destination) != digest:
        raise TransactionIsolationError(f"archive hash mismatch: {source}")
    return digest


def _registry_snapshot(root: Path, *, post_id: int, run_id: str) -> dict[str, Any]:
    path = Path(root) / "work" / "media_registry.sqlite"
    if not path.is_file():
        return {"present": False, "run": [], "decisions": [], "usages": []}
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            run = [dict(row) for row in db.execute(
                "SELECT * FROM media_runs WHERE run_id=? AND post_id=? ORDER BY rowid",
                (run_id, int(post_id)),
            )]
            decisions = [dict(row) for row in db.execute(
                "SELECT * FROM media_decisions WHERE run_id=? AND post_id=? ORDER BY rowid",
                (run_id, int(post_id)),
            )]
            usages = [dict(row) for row in db.execute(
                "SELECT * FROM media_usage WHERE post_id=? ORDER BY rowid",
                (int(post_id),),
            )]
    except (OSError, sqlite3.Error) as exc:
        raise TransactionIsolationError(f"MediaRegistry read failed: {type(exc).__name__}") from exc
    return {"present": True, "run": run, "decisions": decisions, "usages": usages}


def _linked_evidence(journal: Mapping[str, Any], evidence: Mapping[str, Any] | None) -> bool:
    if not isinstance(evidence, Mapping):
        return False
    for key in ("run_id", "cycle_id", "cycle_round", "authorization_id", "post_id"):
        if str(evidence.get(key)) != str(journal.get(key)):
            return False
    return True


def classify_transaction(
    journal: Mapping[str, Any] | None,
    *,
    external_evidence: Mapping[str, Any] | None = None,
) -> str:
    """Return a conservative classification only when its proof contract holds."""
    if journal is None:
        return "NOT_EXECUTED" if isinstance(external_evidence, Mapping) and external_evidence.get("effects_absent") is True else "UNCERTAIN_ISOLATED"
    if (
        journal.get("status") == "committed"
        and journal.get("readback") is True
        and external_evidence is not None
        and _linked_evidence(journal, external_evidence)
        and external_evidence.get("readback_confirmed") is True
    ):
        return "COMMITTED_NO_REPLAY"
    if (
        journal.get("status") in {"interrupted_pre_writer", "failed_pre_writer"}
        and journal.get("writer_started") is False
        and journal.get("resumable") is True
        and journal.get("readback") is not True
        and external_evidence is not None
        and _linked_evidence(journal, external_evidence)
        and external_evidence.get("effects_absent") is True
    ):
        return "PRE_WRITER_RECOVERABLE"
    return "UNCERTAIN_ISOLATED"


def _validate_checkpoint(
    root: Path,
    *,
    cycle_id: str,
    authorization_id: str,
    selected_ids: list[int],
    pending_round: int,
) -> dict[str, Any]:
    state_path = Path(root) / "work" / "v2-cycle" / "state.json"
    journal_path = Path(root) / "work" / "v2-cycle" / "journal.jsonl"
    target_path = Path(root) / "work" / "v2-journal" / "115331.json"
    policy_path = Path(root) / "config" / "v2-quarantine.json"
    if not all(path.is_file() for path in (state_path, journal_path, target_path, policy_path)):
        raise TransactionIsolationError("checkpoint, aggregate journal, target journal, or quarantine is missing")
    state = _read_json(state_path)
    pending = state.get("pending_round")
    if state.get("cycle_id") != cycle_id or state.get("status") != "running":
        raise TransactionIsolationError("cycle identity/status diverged")
    if state.get("phase") != "ADMISSION":
        raise TransactionIsolationError("isolation requires an ADMISSION cycle")
    if not isinstance(pending, dict) or int(pending.get("round_number", -1)) != int(pending_round):
        raise TransactionIsolationError("pending round diverged")
    if str(pending.get("authorization_id") or "") != authorization_id:
        raise TransactionIsolationError("authorization_id diverged")
    actual = [int(value) for value in (pending.get("preview_selected_ids") or [])]
    if actual != [int(value) for value in selected_ids]:
        raise TransactionIsolationError("selection diverged")
    journal = _read_json(target_path)
    expected = {
        "post_id": 115331,
        "run_id": str(journal.get("run_id") or ""),
        "cycle_id": str(journal.get("cycle_id") or ""),
        "cycle_round": journal.get("cycle_round"),
        "authorization_id": str(journal.get("authorization_id") or ""),
    }
    if expected["cycle_id"] != cycle_id or expected["cycle_round"] != pending_round or expected["authorization_id"] != authorization_id:
        raise TransactionIsolationError("target journal identity diverged")
    return {
        "state": state,
        "journal": journal,
        "state_sha256": _hash_file(state_path),
        "journal_sha256": _hash_file(journal_path),
        "target_journal_sha256": _hash_file(target_path),
        "quarantine_sha256": _hash_file(policy_path),
        "paths": {"state": str(state_path), "journal": str(journal_path), "target_journal": str(target_path)},
    }


def _validate_uncertain(journal: Mapping[str, Any], *, run_id: str) -> None:
    if str(journal.get("run_id") or "") != run_id:
        raise TransactionIsolationError("run_id diverged")
    if journal.get("status") != "committing":
        raise TransactionIsolationError("target journal is not committing")
    if journal.get("readback") is True:
        raise TransactionIsolationError("transactional readback is already present; use committed reconciliation")
    if journal.get("media_state") != "apply_not_attempted":
        raise TransactionIsolationError("media_state is not apply_not_attempted")
    if journal.get("writer_started") is False:
        raise TransactionIsolationError("journal is pre-writer recoverable, not uncertain isolation")


def _transition_path(root: Path, isolation_id: str) -> Path:
    return Path(root) / "work" / "v2-cycle" / "transaction-isolations" / f"{isolation_id}.json"


def _event_exists(root: Path, isolation_id: str) -> bool:
    path = Path(root) / "work" / "v2-cycle" / "journal.jsonl"
    if not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if value.get("event") == ISOLATION_EVENT and value.get("isolation_id") == isolation_id:
            return True
    return False


def _add_quarantine(payload: dict[str, Any], *, post_id: int, reason: str) -> dict[str, Any]:
    if payload.get("version") != 1 or not isinstance(payload.get("posts"), list) or not isinstance(payload.get("release_history"), list):
        raise TransactionIsolationError("quarantine schema is invalid")
    result = json.loads(json.dumps(payload))
    rows = [row for row in result["posts"] if isinstance(row, dict)]
    existing = {int(row["post_id"]) for row in rows if isinstance(row.get("post_id"), int)}
    if post_id not in existing:
        result["posts"].append({"post_id": post_id, "reason_code": ISOLATION_REASON, "reason": reason})
    else:
        row = next(row for row in result["posts"] if row.get("post_id") == post_id)
        if row.get("reason_code") != ISOLATION_REASON:
            raise TransactionIsolationError("post already has a different quarantine reason")
    return result


def _apply_transition(root: Path, transition: dict[str, Any]) -> dict[str, Any]:
    root = Path(root)
    path = _transition_path(root, str(transition["isolation_id"]))
    if transition.get("phase") == "completed":
        return {"status": "already_applied", **{k: transition[k] for k in ("isolation_id", "archive_dir", "classification")}}
    archive = Path(transition["archive_dir"])
    quarantine_after = _read_json(archive / "quarantine-after.json")
    atomic_write_text(root / "config" / "v2-quarantine.json", _json(quarantine_after))
    transition["phase"] = "quarantine_applied"
    atomic_write_text(path, _json(transition))

    store = CycleStateStore(root)
    state = store.load()
    if state is None or state.cycle_id != transition["cycle_id"]:
        raise TransactionIsolationError("checkpoint changed during isolation")
    pending = state.pending_round
    expected = transition["selected_ids"]
    fields = {
        "isolation_id": transition["isolation_id"],
        "authorization_id": transition["authorization_id"],
        "round_number": transition["pending_round"],
        "selected_ids": expected,
        "isolated_post_id": transition["post_id"],
        "classification": transition["classification"],
        "reconciled_post_ids": transition["reconciled_post_ids"],
        "not_executed_post_ids": transition["not_executed_post_ids"],
        "executed_ids": transition["executed_ids"],
        "write_replayed": False,
        "operator_authorization": transition["operator_authorization"],
        "archive_dir": transition["archive_dir"],
    }
    if pending is not None:
        if str(pending.get("authorization_id") or "") != transition["authorization_id"] or [int(v) for v in (pending.get("preview_selected_ids") or [])] != expected:
            raise TransactionIsolationError("checkpoint changed before isolation apply")
        state.pending_round = None
        store.save(state, event=ISOLATION_EVENT, **fields)
    elif not _event_exists(root, str(transition["isolation_id"])):
        # CycleStateStore writes the checkpoint before its append-only event.
        # A crash in that gap is repaired by appending the same idempotent
        # administrative event; the state is already safely out of admission.
        store.save(state, event=ISOLATION_EVENT, **fields)
    transition["phase"] = "checkpoint_applied"
    atomic_write_text(path, _json(transition))
    transition["phase"] = "completed"
    transition["completed_at"] = time.time()
    atomic_write_text(path, _json(transition))
    return {"status": "applied", **{k: transition[k] for k in ("isolation_id", "archive_dir", "classification")}, "wordpress_writes": 0}


def isolate_uncertain_transaction(
    root: Path,
    client,
    *,
    cycle_id: str,
    authorization_id: str,
    selected_ids: list[int],
    pending_round: int,
    post_id: int,
    run_id: str,
    apply: bool = False,
    preview: Mapping[str, Any] | None = None,
    operator_authorization: str | None = None,
) -> dict[str, Any]:
    """Preview/apply an idempotent, no-WordPress-write isolation transition."""
    root = Path(root)
    if int(post_id) != 115331:
        raise TransactionIsolationError("only the known uncertain post is supported by this transition")
    selected_ids = [int(value) for value in selected_ids]
    lock = RunSessionLock(root / "work" / "v2-run.lock")
    if not lock.acquire():
        return {"status": "blocked", "reason": "global_lock_unavailable", "wordpress_writes": 0}
    try:
        isolation_id = _hash_value({"cycle_id": cycle_id, "authorization_id": authorization_id, "round": pending_round, "post_id": post_id, "run_id": run_id})[:24]
        transition_path = _transition_path(root, isolation_id)
        if transition_path.is_file():
            transition = _read_json(transition_path)
            if transition.get("phase") == "completed":
                return {"status": "already_applied", "isolation_id": isolation_id, "archive_dir": transition["archive_dir"], "classification": transition["classification"], "wordpress_writes": 0}
        checkpoint = _validate_checkpoint(root, cycle_id=cycle_id, authorization_id=authorization_id, selected_ids=selected_ids, pending_round=pending_round)
        journal = checkpoint["journal"]
        if str(journal.get("post_id") or "") not in {str(post_id), ""}:
            raise TransactionIsolationError("post_id diverged")
        _validate_uncertain(journal, run_id=run_id)
        post = client.get_post(int(post_id))
        if not isinstance(post, dict) or int(post.get("id") or post_id) != post_id:
            raise TransactionIsolationError("WordPress post identity diverged")
        registry = _registry_snapshot(root, post_id=post_id, run_id=run_id)
        evidence = {
            "classification": classify_transaction(journal),
            "checkpoint": checkpoint,
            "wordpress": {"status": post.get("status"), "modified": post.get("modified"), "featured_media": post.get("featured_media"), "meta": post.get("meta") or {}},
            "registry": registry,
        }
        snapshot_hash = _hash_value(evidence)
        if apply:
            if not operator_authorization or not str(operator_authorization).strip():
                raise TransactionIsolationError("explicit operator authorization is required")
            existing_transition = _read_json(transition_path) if transition_path.is_file() else None
            if existing_transition is None:
                if not isinstance(preview, Mapping) or preview.get("snapshot_hash") != snapshot_hash or preview.get("isolation_id") != isolation_id:
                    raise TransactionIsolationError("checkpoint, journal, WordPress, or Registry changed since preview")
            else:
                record_path = Path(existing_transition["archive_dir"]) / "isolation-record.json"
                record = _read_json(record_path)
                expected = record["evidence"]
                current = json.loads(json.dumps(evidence))
                previous = json.loads(json.dumps(expected))
                # Quarantine and the append-only checkpoint journal are the
                # expected local mutations of a resumable transition.  The
                # target journal, WordPress and Registry evidence may not move.
                current["checkpoint"].pop("quarantine_sha256", None)
                previous["checkpoint"].pop("quarantine_sha256", None)
                if _hash_value(current["checkpoint"]["target_journal_sha256"]) != _hash_value(previous["checkpoint"]["target_journal_sha256"]):
                    raise TransactionIsolationError("target journal changed during isolation recovery")
                if current["wordpress"] != previous["wordpress"] or current["registry"] != previous["registry"]:
                    raise TransactionIsolationError("WordPress or MediaRegistry changed during isolation recovery")
                if existing_transition.get("phase") == "prepared" and current["checkpoint"]["state_sha256"] != previous["checkpoint"]["state_sha256"]:
                    raise TransactionIsolationError("checkpoint changed before quarantine was applied")
            archive = root / "work" / "v2-cycle" / "archives" / f"uncertain-{isolation_id}"
            if not archive.exists():
                archive.mkdir(parents=True)
                archived = {
                    "state": _copy_verified(Path(checkpoint["paths"]["state"]), archive / "state.json"),
                    "journal": _copy_verified(Path(checkpoint["paths"]["journal"]), archive / "journal.jsonl"),
                    "target_journal": _copy_verified(Path(checkpoint["paths"]["target_journal"]), archive / f"journal-{post_id}.json"),
                    "quarantine_before": _copy_verified(root / "config" / "v2-quarantine.json", archive / "quarantine-before.json"),
                }
                quarantine_after = _add_quarantine(
                    _read_json(root / "config" / "v2-quarantine.json"),
                    post_id=post_id,
                    reason=f"UNCERTAIN_ISOLATED {isolation_id}; no replay until independently resolved",
                )
                atomic_write_text(archive / "quarantine-after.json", _json(quarantine_after))
                record = {
                    "version": ISOLATION_VERSION,
                    "isolation_id": isolation_id,
                    "classification": "UNCERTAIN_ISOLATED",
                    "cycle_id": cycle_id,
                    "authorization_id": authorization_id,
                    "pending_round": pending_round,
                    "selected_ids": selected_ids,
                    "post_id": post_id,
                    "run_id": run_id,
                    "snapshot_hash": snapshot_hash,
                    "evidence": evidence,
                    "archive_hashes": archived,
                    "reason": "single writer transaction remains uncertain; isolate without replay or commit claim",
                    "operator_authorization": str(operator_authorization),
                }
                atomic_write_text(archive / "isolation-record.json", _json(record))
                transition_path.parent.mkdir(parents=True, exist_ok=True)
                transition = {
                    "version": ISOLATION_VERSION,
                    "phase": "prepared",
                    "isolation_id": isolation_id,
                    "archive_dir": str(archive),
                    "cycle_id": cycle_id,
                    "authorization_id": authorization_id,
                    "pending_round": pending_round,
                    "selected_ids": selected_ids,
                    "post_id": post_id,
                    "run_id": run_id,
                    "classification": "UNCERTAIN_ISOLATED",
                    "reconciled_post_ids": [115524, 115215, 115512, 115338],
                    "not_executed_post_ids": [115215, 115512, 115338],
                    "executed_ids": [115524],
                    "operator_authorization": str(operator_authorization),
                }
                atomic_write_text(transition_path, _json(transition))
            else:
                transition = _read_json(transition_path)
            return _apply_transition(root, transition)
        return {
            "status": "preview",
            "classification": evidence["classification"],
            "isolation_id": isolation_id,
            "snapshot_hash": snapshot_hash,
            "evidence": evidence,
            "selected_ids": selected_ids,
            "wordpress_writes": 0,
        }
    finally:
        lock.release()


__all__ = ["TransactionIsolationError", "classify_transaction", "isolate_uncertain_transaction"]
