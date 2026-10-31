"""Auditable isolation of one uncertain V2 writer transaction.

This module changes only local V2 admission/checkpoint policy.  It never writes
WordPress and never turns an uncertain journal into a committed journal.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from ..backup import atomic_write_text
from .cycle_state import CycleStateStore
from .cycle_runtime import (
    _historical_journal_evidence,
    _journal_correlation,
    _post_no_write_evidence,
    _round_authorized_at,
)
from .lock import RunSessionLock


ISOLATION_VERSION = 1
ISOLATION_EVENT = "transaction_uncertain_isolated"
ISOLATION_REASON = "uncertain_transaction"
PREWRITER_MEDIA_STATUSES = {
    "running", "validated", "committing", "failed_pre_writer", "interrupted_pre_writer",
}
IDENTITYLESS_TERMINAL_JOURNAL_STATUSES = {"committed"}


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


def _is_identityless_terminal_journal(journal: Mapping[str, Any] | None) -> bool:
    """Recognize a legacy terminal journal without current-round identity.

    A committed/read-back journal with no run or round identity is historical
    evidence, not proof that the current authorization executed. It can only
    be closed as ``NOT_EXECUTED`` when the independent no-write proof also
    succeeds; active or pre-writer journals remain unresolved.
    """
    if not isinstance(journal, Mapping):
        return False
    if journal.get("status") not in IDENTITYLESS_TERMINAL_JOURNAL_STATUSES:
        return False
    if journal.get("readback") is not True:
        return False
    return not any(
        str(journal.get(key) or "").strip()
        for key in ("run_id", "cycle_id", "cycle_round", "authorization_id")
    )


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
    digest = _hash_file(source)
    if destination.is_symlink():
        raise TransactionIsolationError(f"archive path must not be a symlink: {destination.name}")
    if destination.exists():
        if not destination.is_file() or _hash_file(destination) != digest:
            raise TransactionIsolationError(f"archive evidence conflict: {destination}")
        return digest
    shutil.copy2(source, destination)
    if _hash_file(destination) != digest:
        raise TransactionIsolationError(f"archive hash mismatch: {source}")
    return digest


def _registry_snapshot(
    root: Path, *, post_id: int, run_id: str | None = None
) -> dict[str, Any]:
    path = Path(root) / "work" / "media_registry.sqlite"
    if not path.is_file():
        return {"present": False, "schema_complete": False, "run": [], "decisions": [], "usages": [], "operations": []}
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            tables = {
                str(row[0])
                for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            required_columns = {
                "media_runs": {"run_id", "post_id", "created_at"},
                "media_decisions": {"run_id", "post_id", "created_at"},
                "media_usage": {
                    "post_id", "status", "applied_at", "run_id", "cycle_id",
                    "cycle_round", "authorization_id", "accepted_at",
                },
                "media_operations": {
                    "post_id", "run_id", "cycle_id", "cycle_round",
                    "authorization_id", "started_at",
                },
            }
            if set(required_columns) - tables:
                return {"present": False, "schema_complete": False, "run": [], "decisions": [], "usages": [], "operations": []}
            for table, required in required_columns.items():
                columns = {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}
                if required - columns:
                    return {"present": False, "schema_complete": False, "run": [], "decisions": [], "usages": [], "operations": []}
            if run_id:
                run_query = "SELECT * FROM media_runs WHERE run_id=? AND post_id=? ORDER BY rowid"
                run_args: tuple[Any, ...] = (run_id, int(post_id))
                decision_query = "SELECT * FROM media_decisions WHERE run_id=? AND post_id=? ORDER BY rowid"
                decision_args: tuple[Any, ...] = (run_id, int(post_id))
            else:
                run_query = "SELECT * FROM media_runs WHERE post_id=? ORDER BY rowid"
                run_args = (int(post_id),)
                decision_query = "SELECT * FROM media_decisions WHERE post_id=? ORDER BY rowid"
                decision_args = (int(post_id),)
            run = [dict(row) for row in db.execute(
                run_query,
                run_args,
            )]
            decisions = [dict(row) for row in db.execute(
                decision_query,
                decision_args,
            )]
            usages = [dict(row) for row in db.execute(
                "SELECT * FROM media_usage WHERE post_id=? ORDER BY rowid",
                (int(post_id),),
            )]
            operations = [dict(row) for row in db.execute(
                "SELECT * FROM media_operations WHERE post_id=? ORDER BY rowid",
                (int(post_id),),
            )]
    except (OSError, sqlite3.Error) as exc:
        raise TransactionIsolationError(f"MediaRegistry read failed: {type(exc).__name__}") from exc
    return {
        "present": True,
        "schema_complete": True,
        "run": run,
        "decisions": decisions,
        "usages": usages,
        "operations": operations,
    }


def _linked_evidence(journal: Mapping[str, Any], evidence: Mapping[str, Any] | None) -> bool:
    if not isinstance(evidence, Mapping):
        return False
    for key in ("run_id", "cycle_id", "cycle_round", "authorization_id", "post_id"):
        if str(evidence.get(key)) != str(journal.get(key)):
            return False
    return True


def _journal_matches_authorized_round(
    journal: Mapping[str, Any],
    *,
    post_id: int,
    cycle_id: str,
    authorization_id: str,
    pending_round: int,
) -> bool:
    try:
        round_number = journal.get("cycle_round")
        if isinstance(round_number, bool):
            return False
        round_number = int(round_number)
    except (TypeError, ValueError):
        return False
    run_id = str(journal.get("run_id") or "").strip()
    return bool(
        run_id
        and str(journal.get("post_id") or "") == str(int(post_id))
        and str(journal.get("cycle_id") or "") == str(cycle_id)
        and round_number == int(pending_round)
        and str(journal.get("authorization_id") or "") == str(authorization_id)
    )


def _registry_has_operation_run(
    registry: Mapping[str, Any], *, post_id: int, run_id: str
) -> bool:
    runs = registry.get("run")
    if not isinstance(runs, list) or len(runs) != 1 or not isinstance(runs[0], Mapping):
        return False
    run = runs[0]
    try:
        return (
            str(run.get("run_id") or "") == str(run_id)
            and int(run.get("post_id")) == int(post_id)
        )
    except (TypeError, ValueError):
        return False


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
    target_post_id: int,
) -> dict[str, Any]:
    state_path = Path(root) / "work" / "v2-cycle" / "state.json"
    journal_path = Path(root) / "work" / "v2-cycle" / "journal.jsonl"
    target_path = Path(root) / "work" / "v2-journal" / f"{int(target_post_id)}.json"
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
        "post_id": int(target_post_id),
        "run_id": str(journal.get("run_id") or ""),
        "cycle_id": str(journal.get("cycle_id") or ""),
        "cycle_round": journal.get("cycle_round"),
        "authorization_id": str(journal.get("authorization_id") or ""),
    }
    if expected["post_id"] not in {int(value) for value in selected_ids}:
        raise TransactionIsolationError("target post is not in the authorized selection")
    if not expected["run_id"]:
        raise TransactionIsolationError("target journal has no run_id")
    target_is_current = _journal_matches_authorized_round(
        journal,
        post_id=int(target_post_id),
        cycle_id=cycle_id,
        authorization_id=authorization_id,
        pending_round=int(pending_round),
    )
    if target_is_current:
        if journal.get("writer_started") is True:
            _validate_uncertain(journal, run_id=expected["run_id"])
        else:
            _validate_pre_writer_uncertain_journal(journal, run_id=expected["run_id"])
    else:
        _validate_historical_target_journal(
            journal,
            post_id=int(target_post_id),
            run_id=expected["run_id"],
            current_authorization_id=authorization_id,
        )
    return {
        "state": state,
        "journal": journal,
        "state_sha256": _hash_file(state_path),
        "journal_sha256": _hash_file(journal_path),
        "target_journal_sha256": _hash_file(target_path),
        "quarantine_sha256": _hash_file(policy_path),
        "target_journal_is_current": target_is_current,
        "paths": {"state": str(state_path), "journal": str(journal_path), "target_journal": str(target_path)},
    }


def _validate_quarantine_payload(payload: Mapping[str, Any]) -> None:
    if (
        payload.get("version") != 1
        or not isinstance(payload.get("posts"), list)
        or not isinstance(payload.get("release_history"), list)
    ):
        raise TransactionIsolationError("quarantine schema is invalid")
    post_ids: set[int] = set()
    for row in payload["posts"]:
        if not isinstance(row, Mapping) or type(row.get("post_id")) is not int:
            raise TransactionIsolationError("quarantine contains an invalid post entry")
        if row["post_id"] < 1 or row["post_id"] in post_ids:
            raise TransactionIsolationError("quarantine contains duplicate post entries")
        if (
            not isinstance(row.get("reason_code"), str)
            or not row.get("reason_code", "").strip()
            or not isinstance(row.get("reason"), str)
            or not row.get("reason", "").strip()
        ):
            raise TransactionIsolationError("quarantine contains an invalid reason")
        post_ids.add(row["post_id"])


def _assert_quarantine_additions_only(
    before: Mapping[str, Any], current: Mapping[str, Any]
) -> None:
    """Allow append-only additions while rejecting edits/removals of policy evidence."""
    _validate_quarantine_payload(before)
    _validate_quarantine_payload(current)
    for key, value in before.items():
        if key in {"posts", "release_history"}:
            continue
        if current.get(key) != value:
            raise TransactionIsolationError(f"quarantine policy changed existing field: {key}")
    if current["posts"][: len(before["posts"])] != before["posts"]:
        raise TransactionIsolationError("quarantine post entries were reordered, changed, or truncated")
    if current["release_history"][: len(before["release_history"])] != before["release_history"]:
        raise TransactionIsolationError("quarantine release history was changed or truncated")


def _quarantine_with_target(
    payload: dict[str, Any], *, post_id: int, reason: str
) -> dict[str, Any]:
    _validate_quarantine_payload(payload)
    result = deepcopy(payload)
    existing = next((row for row in result["posts"] if row["post_id"] == post_id), None)
    if existing is None:
        result["posts"].append({
            "post_id": post_id,
            "reason_code": ISOLATION_REASON,
            "reason": reason,
        })
    elif existing.get("reason_code") != ISOLATION_REASON:
        raise TransactionIsolationError("post already has a different quarantine reason")
    return result


def _write_quarantine_additively(
    root: Path,
    *,
    baseline: Mapping[str, Any],
    post_id: int | None = None,
    post_ids: list[int] | None = None,
    reason: str,
) -> None:
    """Apply only target entries, preserving additions made after preparation."""
    targets = [int(value) for value in (post_ids or ([post_id] if post_id is not None else []))]
    if not targets:
        raise TransactionIsolationError("quarantine target is missing")
    path = Path(root) / "config" / "v2-quarantine.json"
    if path.is_symlink():
        raise TransactionIsolationError("quarantine policy path must not be a symlink")
    for _attempt in range(3):
        current = _read_json(path)
        _assert_quarantine_additions_only(baseline, current)
        merged = current
        for target in targets:
            merged = _quarantine_with_target(merged, post_id=target, reason=reason)
        if merged == current:
            return
        before_write = _hash_file(path)
        latest = _read_json(path)
        if _hash_value(latest) != _hash_value(current) or _hash_file(path) != before_write:
            continue
        atomic_write_text(path, _json(merged))
        written = _read_json(path)
        _assert_quarantine_additions_only(baseline, written)
        if written.get("posts") != merged.get("posts") or written.get("release_history") != merged.get("release_history"):
            raise TransactionIsolationError("quarantine changed concurrently during isolation")
        return
    raise TransactionIsolationError("quarantine changed concurrently during isolation")


def _event_records(root: Path, isolation_id: str) -> list[dict[str, Any]]:
    path = Path(root) / "work" / "v2-cycle" / "journal.jsonl"
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if (
            isinstance(value, dict)
            and value.get("event") == ISOLATION_EVENT
            and value.get("isolation_id") == isolation_id
        ):
            records.append(value)
    return records


def _ensure_journal_append_boundary(path: Path) -> None:
    """Keep a torn final JSONL record from absorbing the recovery event."""
    if not path.is_file() or path.stat().st_size == 0:
        return
    with path.open("rb") as handle:
        handle.seek(-1, os.SEEK_END)
        last_byte = handle.read(1)
    if last_byte != b"\n":
        with path.open("ab") as handle:
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())


def _wp_state(post: Mapping[str, Any]) -> dict[str, Any] | None:
    value = (post.get("meta") or {}).get("_hermes_work_state")
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        return dict(parsed) if isinstance(parsed, Mapping) else None
    return None


def _wp_summary(post: Mapping[str, Any]) -> dict[str, Any]:
    state = _wp_state(post)
    content = post.get("content") or {}
    title = post.get("title") or {}
    return {
        "id": int(post.get("id") or 0),
        "status": post.get("status"),
        "modified": post.get("modified"),
        "featured_media": post.get("featured_media"),
        "work_state_sha256": _hash_value(state) if state is not None else "",
        "ready_hash": str((post.get("meta") or {}).get("_hermes_ready_hash") or ""),
        "content_sha256": _hash_value(str(content.get("raw") or "")),
        "title_sha256": _hash_value(str(title.get("raw") or "")),
    }


def _registry_audit_rows(registry: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    fields = {
        "run": (
            "run_id", "post_id", "created_at", "started_at", "completed_at",
            "cycle_id", "cycle_round", "authorization_id", "acquisition_outcome",
            "operation_status", "outcome",
        ),
        "decisions": (
            "run_id", "post_id", "candidate_id", "disposition", "reason_code",
            "scope", "cycle_id", "cycle_round", "authorization_id", "created_at",
        ),
        "usages": (
            "post_id", "asset_id", "wordpress_media_id", "role", "status",
            "applied_at", "run_id", "cycle_id", "cycle_round",
            "authorization_id", "accepted_at", "readback_confirmed_at",
        ),
        "operations": (
            "post_id", "run_id", "cycle_id", "cycle_round", "authorization_id",
            "started_at", "operation_status", "write_started",
            "readback_confirmed", "completed_at", "outcome",
        ),
    }
    return {
        section: [
            {key: row.get(key) for key in allowed if key in row}
            for row in (registry.get(section) or [])
            if isinstance(row, Mapping)
        ]
        for section, allowed in fields.items()
    }


def _registry_summary(
    registry: Mapping[str, Any],
    *,
    include_audit_rows: bool = False,
) -> dict[str, Any]:
    result = {
        "present": registry.get("present") is True,
        "schema_complete": registry.get("schema_complete") is True,
        "run_count": len(registry.get("run") or []),
        "decision_count": len(registry.get("decisions") or []),
        "usage_count": len(registry.get("usages") or []),
        "operation_count": len(registry.get("operations") or []),
        "snapshot_sha256": _hash_value(dict(registry)),
    }
    if include_audit_rows:
        result["audit_rows"] = _registry_audit_rows(registry)
    return result


def _media_artifact_path(root: Path, post_id: int, kind: str) -> Path:
    if kind not in {"partial", "sidecar"}:
        raise TransactionIsolationError("unsupported media artifact kind")
    root = Path(root)
    backups_root = root / "backups"
    post_dir = backups_root / str(int(post_id))
    if backups_root.is_symlink() or post_dir.is_symlink():
        raise TransactionIsolationError("media artifact directory must not be a symlink")
    path = post_dir / (
        "editorial.partial.json" if kind == "partial" else "editorial.partial.v2.json"
    )
    try:
        path.resolve().relative_to(backups_root.resolve())
    except (OSError, ValueError) as exc:
        raise TransactionIsolationError("media artifact path escapes the backup directory") from exc
    return path


def _media_artifact_snapshot(root: Path, post_id: int) -> dict[str, Any]:
    """Describe the exact partial/sidecar inputs without normalizing their bytes."""
    paths = {
        "partial": (
            _media_artifact_path(root, post_id, "partial"),
            f"editorial-partial-{int(post_id)}.json",
        ),
        "sidecar": (
            _media_artifact_path(root, post_id, "sidecar"),
            f"editorial-partial-v2-{int(post_id)}.json",
        ),
    }
    result: dict[str, Any] = {}
    parsed_partial: Mapping[str, Any] | None = None
    parsed_sidecar: Mapping[str, Any] | None = None
    for name, (path, archive_name) in paths.items():
        if path.is_symlink():
            result[name] = {
                "present": True,
                "sha256": "",
                "size_bytes": None,
                "parse_status": "symlink_rejected",
                "archive_name": archive_name,
            }
            continue
        if not path.exists():
            result[name] = {
                "present": False,
                "sha256": "",
                "size_bytes": 0,
                "parse_status": "absent",
                "archive_name": archive_name,
            }
            continue
        if not path.is_file():
            result[name] = {
                "present": True,
                "sha256": "",
                "size_bytes": None,
                "parse_status": "not_a_file",
                "archive_name": archive_name,
            }
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            result[name] = {
                "present": True,
                "sha256": "",
                "size_bytes": None,
                "parse_status": f"read_error:{type(exc).__name__}",
                "archive_name": archive_name,
            }
            continue
        parsed: Mapping[str, Any] | None = None
        parse_status = "valid"
        try:
            value = json.loads(data.decode("utf-8"))
            if isinstance(value, Mapping):
                parsed = value
            else:
                parse_status = "invalid_json_object"
        except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
            parse_status = "invalid_json"
        result[name] = {
            "present": True,
            "sha256": _hash_bytes(data),
            "size_bytes": len(data),
            "parse_status": parse_status,
            "archive_name": archive_name,
        }
        if name == "partial":
            parsed_partial = parsed
        else:
            parsed_sidecar = parsed

    partial_ids: set[int] = set()
    partial_parse_errors: list[str] = []
    if result["partial"]["present"] and parsed_partial is not None:
        inline = parsed_partial.get("inline")
        accepted = inline.get("accepted", []) if isinstance(inline, Mapping) else []
        if inline is not None and not isinstance(inline, Mapping):
            partial_parse_errors.append("inline_not_object")
        if not isinstance(accepted, list):
            partial_parse_errors.append("inline_accepted_not_list")
            accepted = []
        for index, item in enumerate(accepted):
            if not isinstance(item, Mapping):
                partial_parse_errors.append(f"inline_item_{index}_not_object")
                continue
            value = item.get("media_id")
            if isinstance(value, bool) or not str(value or "").isdigit():
                partial_parse_errors.append(f"inline_item_{index}_media_id_invalid")
                continue
            if int(value) > 0:
                partial_ids.add(int(value))
        featured = parsed_partial.get("featured")
        if featured is not None and not isinstance(featured, Mapping):
            partial_parse_errors.append("featured_not_object")
        elif isinstance(featured, Mapping):
            value = featured.get("media_id")
            if value not in (None, "", 0, "0"):
                if isinstance(value, bool) or not str(value).isdigit():
                    partial_parse_errors.append("featured_media_id_invalid")
                elif int(value) > 0:
                    partial_ids.add(int(value))

    sidecar_summary: dict[str, Any] = {"parse_status": result["sidecar"]["parse_status"]}
    sidecar_parse_errors: list[str] = []
    if parsed_sidecar is not None:
        schema_version = parsed_sidecar.get("schema_version")
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != 1
        ):
            sidecar_parse_errors.append("schema_version_unsupported")
        for key in ("post_id", "run_id", "cycle_id", "cycle_round", "authorization_id", "checkpoint_sha256", "media_ids"):
            if key not in parsed_sidecar:
                sidecar_parse_errors.append(f"missing_{key}")
        sidecar_media_ids = parsed_sidecar.get("media_ids")
        normalized_sidecar_ids: list[int] = []
        if not isinstance(sidecar_media_ids, list):
            sidecar_parse_errors.append("media_ids_not_list")
        else:
            for index, value in enumerate(sidecar_media_ids):
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    sidecar_parse_errors.append(f"media_id_{index}_invalid")
                    continue
                normalized_sidecar_ids.append(value)
            if len(normalized_sidecar_ids) != len(set(normalized_sidecar_ids)):
                sidecar_parse_errors.append("media_ids_duplicate")
        sidecar_summary.update({
            key: parsed_sidecar.get(key)
            for key in (
                "schema_version", "post_id", "run_id", "cycle_id",
                "cycle_round", "authorization_id", "checkpoint_sha256",
            )
        })
        sidecar_summary["media_ids"] = normalized_sidecar_ids
    return {
        **result,
        "partial_sha256": result["partial"].get("sha256", ""),
        "partial_media_ids": sorted(partial_ids),
        "partial_parse_errors": partial_parse_errors,
        "sidecar_parse_errors": sidecar_parse_errors,
        "sidecar_summary": sidecar_summary,
    }


def _attachment_snapshot(
    client,
    *,
    media_ids: set[int],
) -> dict[str, Any]:
    if not media_ids:
        return {"status": "not_required", "items": []}
    getter = getattr(client, "get_media", None)
    if not callable(getter):
        return {
            "status": "unavailable",
            "items": [{"id": media_id, "status": "reader_unavailable"} for media_id in sorted(media_ids)],
        }
    items: list[dict[str, Any]] = []
    for media_id in sorted(media_ids):
        try:
            attachment = getter(int(media_id))
        except Exception as exc:  # noqa: BLE001 - preserve uncertainty on REST failure
            items.append({
                "id": media_id,
                "status": "read_error",
                "error_type": type(exc).__name__,
            })
            continue
        try:
            attachment_id = int(attachment.get("id") or 0) if isinstance(attachment, Mapping) else 0
        except (TypeError, ValueError):
            attachment_id = 0
        if not isinstance(attachment, Mapping) or attachment_id != media_id:
            items.append({"id": media_id, "status": "identity_mismatch"})
            continue
        source_url = str(attachment.get("source_url") or "")
        try:
            response_sha256 = _hash_value(dict(attachment))
        except (TypeError, ValueError):
            items.append({"id": media_id, "status": "invalid_response"})
            continue
        items.append({
            "id": media_id,
            "status": "read",
            "response_sha256": response_sha256,
            "source_url_sha256": _hash_bytes(source_url.encode("utf-8")) if source_url else "",
            "mime_type": attachment.get("mime_type"),
            "media_type": attachment.get("media_type"),
            "parent": attachment.get("post"),
            "alt_text_sha256": _hash_value(str(attachment.get("alt_text") or "")),
        })
    return {
        "status": "complete" if all(item.get("status") == "read" for item in items) else "unavailable",
        "items": items,
    }


def _prewriter_media_isolation_evidence(
    journal: Mapping[str, Any] | None,
    *,
    run_id: str | None,
    current_identity: bool,
    registry: Mapping[str, Any],
    artifacts: Mapping[str, Any],
    attachments: Mapping[str, Any],
) -> dict[str, Any]:
    initial_state = journal.get("state") if isinstance(journal, Mapping) else None
    initial_media = initial_state.get("media") if isinstance(initial_state, Mapping) else {}
    initial_inline = initial_media.get("inline") if isinstance(initial_media, Mapping) else {}
    initial_items = initial_inline.get("accepted", []) if isinstance(initial_inline, Mapping) else []
    initial_ids = {
        int(item["media_id"])
        for item in initial_items if isinstance(item, Mapping)
        and not isinstance(item.get("media_id"), bool)
        and str(item.get("media_id") or "").isdigit() and int(item["media_id"]) > 0
    } if isinstance(initial_items, list) else set()
    initial_featured = initial_media.get("featured") if isinstance(initial_media, Mapping) else None
    if isinstance(initial_featured, Mapping):
        value = initial_featured.get("media_id")
        if not isinstance(value, bool) and str(value or "").isdigit() and int(value) > 0:
            initial_ids.add(int(value))
    partial_ids = {int(value) for value in artifacts.get("partial_media_ids", [])}
    registry_runs = registry.get("run") if isinstance(registry.get("run"), list) else []
    exact_run_rows = [
        row for row in registry_runs
        if isinstance(row, Mapping)
        and str(row.get("run_id") or "") == str(run_id or "")
        and str(row.get("post_id") or "") == str((journal or {}).get("post_id") or "")
    ]
    signals: list[str] = []
    if partial_ids - initial_ids:
        signals.append("partial_references_media_not_in_initial_journal_state")
    if exact_run_rows:
        signals.append("media_registry_run_exists_for_current_run")
    if journal and any(journal.get(key) for key in ("media_ids", "media_records", "apply_media_records")):
        signals.append("journal_contains_media_references")
    expected_registry_identity = {
        "cycle_id": str((journal or {}).get("cycle_id") or ""),
        "cycle_round": (journal or {}).get("cycle_round"),
        "authorization_id": str((journal or {}).get("authorization_id") or ""),
    }
    registry_identity_conflicts: list[dict[str, Any]] = []
    for section in ("run", "decisions", "usages", "operations"):
        for index, row in enumerate(registry.get(section) or []):
            if not isinstance(row, Mapping) or str(row.get("run_id") or "") != str(run_id or ""):
                continue
            for field, expected in expected_registry_identity.items():
                if field not in row or row.get(field) in (None, ""):
                    continue
                actual = str(row.get(field)) if field == "cycle_round" else row.get(field)
                expected_value = str(expected) if field == "cycle_round" else expected
                if actual != expected_value:
                    registry_identity_conflicts.append({
                        "section": section,
                        "row_index": index,
                        "field": field,
                        "expected": expected_value,
                        "actual": actual,
                    })
    basic = bool(
        current_identity
        and journal is not None
        and str(journal.get("run_id") or "") == str(run_id or "")
        and journal.get("status") in PREWRITER_MEDIA_STATUSES
        and journal.get("writer_started") is False
        and journal.get("readback") is not True
    )
    sidecar = artifacts.get("sidecar_summary") or {}
    sidecar_present = artifacts.get("sidecar", {}).get("present") is True
    sidecar_media_ids = sidecar.get("media_ids")
    sidecar_identity_matches = bool(
        not sidecar_present
        or (
            sidecar.get("parse_status") == "valid"
            and not artifacts.get("sidecar_parse_errors")
            and str(sidecar.get("post_id") or "") == str((journal or {}).get("post_id") or "")
            and str(sidecar.get("run_id") or "") == str(run_id or "")
            and str(sidecar.get("cycle_id") or "") == str((journal or {}).get("cycle_id") or "")
            and str(sidecar.get("cycle_round") or "") == str((journal or {}).get("cycle_round") or "")
            and str(sidecar.get("authorization_id") or "") == str((journal or {}).get("authorization_id") or "")
            and str(sidecar.get("checkpoint_sha256") or "") == str(artifacts.get("partial_sha256") or "")
            and isinstance(sidecar_media_ids, list)
            and set(sidecar_media_ids) == partial_ids
        )
    )
    artifacts_valid = bool(
        artifacts.get("partial", {}).get("present") is True
        and artifacts.get("partial", {}).get("parse_status") == "valid"
        and not artifacts.get("partial_parse_errors")
        and sidecar_identity_matches
        and partial_ids
    )
    complete_evidence = bool(
        registry.get("present") is True
        and registry.get("schema_complete") is True
        and exact_run_rows
        and not registry_identity_conflicts
        and attachments.get("status") == "complete"
        and {
            int(item.get("id") or 0)
            for item in attachments.get("items", [])
            if item.get("status") == "read"
        } == partial_ids
    )
    eligible = bool(basic and artifacts_valid and complete_evidence and signals)
    return {
        "eligible": eligible,
        "reason": (
            "pre_writer_media_uncertainty_preserved"
            if eligible
            else "pre_writer_media_isolation_evidence_incomplete"
        ),
        "signals": signals,
        "partial_media_ids": sorted(partial_ids),
        "initial_media_ids": sorted(initial_ids),
        "registry_current_run_count": len(exact_run_rows),
        "registry_identity_conflicts": registry_identity_conflicts,
        "sidecar_present": sidecar_present,
        "sidecar_identity_matches": sidecar_identity_matches,
        "sidecar_parse_errors": list(artifacts.get("sidecar_parse_errors") or []),
        "missing_criteria": [
            name for name, ok in (
                ("current_journal_identity", current_identity),
                ("valid_media_artifacts", artifacts_valid),
                ("complete_registry_and_attachment_evidence", complete_evidence),
                ("uncertainty_signal", bool(signals)),
            ) if not ok
        ],
        "registry_complete": registry.get("schema_complete") is True,
        "artifacts_valid": artifacts_valid,
        "attachments_complete": attachments.get("status") == "complete",
    }


def _round_post_evidence(
    root: Path,
    client,
    *,
    post_id: int,
    cycle_id: str,
    authorization_id: str,
    pending_round: int,
    target_journal: Mapping[str, Any] | None = None,
    capture_media_artifacts: bool = False,
) -> dict[str, Any]:
    journal_path = Path(root) / "work" / "v2-journal" / f"{int(post_id)}.json"
    journal = None
    journal_sha256 = ""
    if journal_path.is_file():
        try:
            journal_bytes = journal_path.read_bytes()
            live_journal = json.loads(journal_bytes.decode("utf-8"))
        except (OSError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransactionIsolationError(f"invalid post journal for {post_id}") from exc
        if not isinstance(live_journal, dict):
            raise TransactionIsolationError(f"post journal must be an object for {post_id}")
        if target_journal is not None and dict(target_journal) != live_journal:
            raise TransactionIsolationError(f"post journal changed while reading evidence for {post_id}")
        journal = dict(target_journal) if target_journal is not None else live_journal
        journal_sha256 = _hash_bytes(journal_bytes)
    elif target_journal is not None:
        raise TransactionIsolationError(f"post journal disappeared for {post_id}")
    journal_post_id = str((journal or {}).get("post_id") or "")
    journal_identity_matches = journal is not None and journal_post_id == str(int(post_id))
    if journal is not None and journal_post_id not in {str(int(post_id)), ""}:
        raise TransactionIsolationError(f"post journal identity diverged for {post_id}")
    run_id = str((journal or {}).get("run_id") or "") or None
    registry = _registry_snapshot(root, post_id=post_id, run_id=run_id)
    post = client.get_post(int(post_id))
    if not isinstance(post, dict) or int(post.get("id") or 0) != int(post_id):
        raise TransactionIsolationError(f"WordPress post identity diverged for {post_id}")

    classification = "UNCERTAIN_ISOLATED"
    reason = "journal_missing_or_evidence_incomplete"
    no_write_evidence: dict[str, Any] | None = None
    historical_journal_evidence: dict[str, Any] | None = None
    current_identity = bool(
        journal is not None
        and journal_identity_matches
        and _journal_matches_authorized_round(
            journal,
            post_id=int(post_id),
            cycle_id=cycle_id,
            authorization_id=authorization_id,
            pending_round=int(pending_round),
        )
    )
    expected_identity = {
        "run_id": run_id or "",
        "cycle_id": str(cycle_id),
        "cycle_round": int(pending_round),
        "authorization_id": str(authorization_id),
        "post_id": int(post_id),
    }
    artifacts: dict[str, Any] | None = None
    attachments: dict[str, Any] | None = None
    prewriter_media_isolation: dict[str, Any] | None = None
    if capture_media_artifacts:
        artifacts = _media_artifact_snapshot(root, int(post_id))
        media_ids = {int(value) for value in artifacts.get("partial_media_ids", [])}
        for key in ("media_ids",):
            values = (journal or {}).get(key, [])
            if isinstance(values, list):
                media_ids.update(
                    int(value) for value in values
                    if not isinstance(value, bool) and str(value).isdigit() and int(value) > 0
                )
        for key in ("media_records", "apply_media_records"):
            values = (journal or {}).get(key, [])
            if isinstance(values, list):
                for item in values:
                    if not isinstance(item, Mapping):
                        continue
                    value = item.get("wordpress_media_id", item.get("media_id"))
                    if not isinstance(value, bool) and str(value or "").isdigit() and int(value) > 0:
                        media_ids.add(int(value))
        for item in registry.get("usages") or []:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("run_id") or "") != str(run_id or ""):
                continue
            value = item.get("wordpress_media_id", item.get("media_id"))
            if not isinstance(value, bool) and str(value or "").isdigit() and int(value) > 0:
                media_ids.add(int(value))
        attachments = _attachment_snapshot(client, media_ids=media_ids)
        prewriter_media_isolation = _prewriter_media_isolation_evidence(
            journal,
            run_id=run_id,
            current_identity=current_identity,
            registry=registry,
            artifacts=artifacts,
            attachments=attachments,
        )

    if journal is None:
        no_write_evidence = _post_no_write_evidence(
            client,
            root,
            int(post_id),
            cycle_id=cycle_id,
            round_number=int(pending_round),
            authorization_id=authorization_id,
        )
        if no_write_evidence is not None:
            classification = "NOT_EXECUTED"
            reason = "no_journal_and_current_authorization_no_write_proven"
    elif journal is not None and journal_post_id and not journal_identity_matches:
        reason = "journal_post_identity_missing"
    elif current_identity:
        current_state = _wp_state(post)
        journal_state = journal.get("state")
        state_matches = (
            isinstance(current_state, Mapping)
            and isinstance(journal_state, Mapping)
            and _hash_value(current_state) == _hash_value(dict(journal_state))
        )
        if registry.get("present") is True and state_matches:
            if (
                journal.get("status") == "committed"
                and journal.get("readback") is True
                and run_id
                and _registry_has_operation_run(
                    registry, post_id=int(post_id), run_id=run_id
                )
            ):
                classification = classify_transaction(
                    journal,
                    external_evidence={**expected_identity, "readback_confirmed": True},
                )
                reason = "current_authorization_commit_registry_run_and_wordpress_state_match"
            elif (
                journal.get("status") in {"interrupted_pre_writer", "failed_pre_writer"}
                and journal.get("writer_started") is False
                and journal.get("resumable") is True
                and run_id
                and not registry.get("run")
                and not registry.get("decisions")
                and not registry.get("usages")
                and not journal.get("media_ids")
                and not journal.get("media_records")
                and not journal.get("apply_media_records")
                and journal.get("media_state") == "apply_not_attempted"
                and journal.get("media_application_status") == "not_attempted"
                and journal.get("html_readback_status") == "not_attempted"
            ):
                classification = classify_transaction(
                    journal,
                    external_evidence={**expected_identity, "effects_absent": True},
                )
                reason = "current_authorization_pre_writer_registry_and_wordpress_state_match"
            elif classification == "UNCERTAIN_ISOLATED":
                reason = "current_authorization_operation_identity_or_effect_evidence_incomplete"
        else:
            reason = "current_authorization_journal_effects_or_readback_unresolved"
    else:
        correlation = _journal_correlation(
            journal,
            expected_cycle_id=cycle_id,
            expected_cycle_round=int(pending_round),
            expected_authorization_id=authorization_id,
        )
        if correlation == "historical":
            historical_journal_evidence = {
                "correlation": "historical",
                "run_id": run_id or "",
                "journal_cycle_id": journal.get("cycle_id"),
                "journal_cycle_round": journal.get("cycle_round"),
                "journal_authorization_id": journal.get("authorization_id"),
                "post_id": int(post_id),
            }
        elif correlation == "uncertain":
            authorization_at = _round_authorized_at(
                root,
                cycle_id,
                int(pending_round),
                authorization_id=authorization_id,
            )
            historical_journal_evidence = _historical_journal_evidence(
                root,
                int(post_id),
                journal,
                authorized_at=authorization_at,
            )
        if historical_journal_evidence is not None:
            no_write_evidence = _post_no_write_evidence(
                client,
                root,
                int(post_id),
                cycle_id=cycle_id,
                round_number=int(pending_round),
                authorization_id=authorization_id,
            )
            if no_write_evidence is not None:
                classification = "NOT_EXECUTED"
                reason = "historical_journal_and_current_authorization_no_write_proven"
            else:
                reason = "historical_journal_but_current_authorization_effects_unresolved"
        elif correlation == "uncertain" and _is_identityless_terminal_journal(journal):
            no_write_evidence = _post_no_write_evidence(
                client,
                root,
                int(post_id),
                cycle_id=cycle_id,
                round_number=int(pending_round),
                authorization_id=authorization_id,
            )
            if no_write_evidence is not None:
                classification = "NOT_EXECUTED"
                reason = "identityless_terminal_journal_and_current_authorization_no_write_proven"
            else:
                reason = "identityless_terminal_journal_but_current_authorization_effects_unresolved"
        elif correlation == "current":
            reason = "legacy_current_round_journal_lacks_authorization_identity"
        else:
            reason = "journal_identity_uncertain"

    return {
        "post_id": int(post_id),
        "classification": classification,
        "reason": reason,
        "journal": {
            "present": journal is not None,
            "sha256": journal_sha256,
            "status": (journal or {}).get("status"),
            "run_id": str((journal or {}).get("run_id") or ""),
            "cycle_id": (journal or {}).get("cycle_id"),
            "cycle_round": (journal or {}).get("cycle_round"),
            "authorization_id": (journal or {}).get("authorization_id"),
            "writer_started": (journal or {}).get("writer_started"),
            "resumable": (journal or {}).get("resumable"),
            "readback": (journal or {}).get("readback"),
            "media_state": (journal or {}).get("media_state"),
        },
        "historical_journal_evidence": historical_journal_evidence,
        "no_write_evidence": no_write_evidence,
        "wordpress": _wp_summary(post),
        "registry": _registry_summary(
            registry,
            include_audit_rows=capture_media_artifacts,
        ),
        "media_artifacts": artifacts,
        "attachments": attachments,
        "prewriter_media_isolation": prewriter_media_isolation,
    }


def _derive_round_classifications(
    round_posts: list[Mapping[str, Any]],
    *,
    isolated_post_ids: list[int] | set[int] | tuple[int, ...],
    require_resolved: bool = True,
) -> dict[str, list[int]]:
    isolated = {int(value) for value in isolated_post_ids}
    others = [
        item for item in round_posts
        if int(item.get("post_id") or 0) not in isolated
    ]
    unresolved = [
        int(item["post_id"])
        for item in others
        if item.get("classification") not in {
            "COMMITTED_NO_REPLAY", "PRE_WRITER_RECOVERABLE", "NOT_EXECUTED",
        }
    ]
    if unresolved and require_resolved:
        raise TransactionIsolationError(
            "other authorized posts have unresolved execution evidence: "
            + ",".join(str(value) for value in unresolved)
        )
    resolved = [item for item in others if int(item["post_id"]) not in set(unresolved)]
    return {
        "reconciled_post_ids": sorted(int(item["post_id"]) for item in resolved),
        "not_executed_post_ids": sorted(
            int(item["post_id"])
            for item in resolved
            if item.get("classification") in {"PRE_WRITER_RECOVERABLE", "NOT_EXECUTED"}
        ),
        "executed_ids": sorted(
            int(item["post_id"])
            for item in resolved
            if item.get("classification") == "COMMITTED_NO_REPLAY"
        ),
        "unresolved_post_ids": sorted(unresolved),
    }


def _validate_uncertain(
    journal: Mapping[str, Any],
    *,
    run_id: str,
) -> None:
    if str(journal.get("run_id") or "") != run_id:
        raise TransactionIsolationError("run_id diverged")
    if journal.get("status") != "committing":
        raise TransactionIsolationError("target journal is not committing")
    if journal.get("readback") is True:
        raise TransactionIsolationError("transactional readback is already present; use committed reconciliation")
    if journal.get("media_state") != "apply_not_attempted":
        raise TransactionIsolationError("media_state is not apply_not_attempted")
    if journal.get("writer_started") is not True:
        raise TransactionIsolationError("journal has no positive writer-start evidence for uncertain isolation")


def _validate_pre_writer_uncertain_journal(
    journal: Mapping[str, Any],
    *,
    run_id: str,
) -> None:
    """Validate only the journal identity/state; media evidence is checked separately."""
    if str(journal.get("run_id") or "") != run_id:
        raise TransactionIsolationError("run_id diverged")
    if journal.get("status") not in PREWRITER_MEDIA_STATUSES:
        raise TransactionIsolationError("pre-writer journal status is not isolatable")
    if journal.get("writer_started") is not False:
        raise TransactionIsolationError("pre-writer isolation requires explicit writer_started=false")
    if journal.get("readback") is True:
        raise TransactionIsolationError("pre-writer journal already has confirmed transactional readback")


def _validate_isolated_current_journal(
    journal: Mapping[str, Any],
    *,
    run_id: str,
    evidence: Mapping[str, Any] | None,
) -> None:
    if journal.get("writer_started") is True:
        _validate_uncertain(journal, run_id=run_id)
        return
    _validate_pre_writer_uncertain_journal(journal, run_id=run_id)
    prewriter = (evidence or {}).get("prewriter_media_isolation")
    if isinstance(prewriter, Mapping) and prewriter.get("attachments_complete") is not True:
        raise TransactionIsolationError("pre-writer attachment evidence is incomplete")
    if not isinstance(prewriter, Mapping) or prewriter.get("eligible") is not True:
        raise TransactionIsolationError("pre-writer media evidence is incomplete for isolation")


def _validate_historical_target_journal(
    journal: Mapping[str, Any],
    *,
    post_id: int,
    run_id: str,
    current_authorization_id: str,
) -> None:
    """Accept only a fully identified committed journal from another authorization."""
    try:
        cycle_round = journal.get("cycle_round")
        if isinstance(cycle_round, bool):
            raise ValueError
        cycle_round = int(cycle_round)
    except (TypeError, ValueError):
        cycle_round = 0
    journal_authorization_id = str(journal.get("authorization_id") or "").strip()
    writer_started_present = "writer_started" in journal
    writer_started_valid = (not writer_started_present) or journal.get("writer_started") is True
    if (
        str(journal.get("post_id") or "") != str(int(post_id))
        or str(journal.get("run_id") or "").strip() != str(run_id).strip()
        or not str(journal.get("cycle_id") or "").strip()
        or cycle_round < 1
        or not journal_authorization_id
        or journal_authorization_id == str(current_authorization_id)
        or journal.get("status") != "committed"
        or journal.get("readback") is not True
        or not writer_started_valid
    ):
        raise TransactionIsolationError("historical target journal identity or committed evidence is invalid")


def _transition_path(root: Path, isolation_id: str) -> Path:
    return Path(root) / "work" / "v2-cycle" / "transaction-isolations" / f"{isolation_id}.json"


def _validate_archive_scope(root: Path, archive: Path) -> Path:
    cycle_root = (Path(root) / "work" / "v2-cycle").resolve()
    archives_root = (cycle_root / "archives").resolve()
    try:
        archives_root.relative_to(cycle_root)
        archive.resolve().relative_to(archives_root)
    except ValueError as exc:
        raise TransactionIsolationError("archive path escapes the V2 cycle directory") from exc
    return archives_root


def _write_json_once(path: Path, payload: Mapping[str, Any]) -> None:
    rendered = _json(dict(payload))
    if path.is_symlink():
        raise TransactionIsolationError(f"prepared archive path must not be a symlink: {path.name}")
    if path.exists():
        if not path.is_file() or path.read_text(encoding="utf-8") != rendered:
            raise TransactionIsolationError(f"prepared archive evidence conflicts: {path.name}")
        return
    atomic_write_text(path, rendered)


def _validate_media_artifact_archive(
    archive: Path,
    *,
    post_id: int,
    primary_post_id: int,
    detail: Mapping[str, Any],
    hashes: Mapping[str, Any],
) -> None:
    artifacts = detail.get("media_artifacts")
    if not isinstance(artifacts, Mapping):
        raise TransactionIsolationError(f"media artifact evidence is missing for {post_id}")
    for kind, hash_key in (
        ("partial", "media_partial"),
        ("sidecar", "media_sidecar"),
    ):
        evidence = artifacts.get(kind)
        if not isinstance(evidence, Mapping):
            raise TransactionIsolationError(f"{kind} evidence is missing for {post_id}")
        expected_name = (
            f"editorial-partial-{int(post_id)}.json"
            if kind == "partial"
            else f"editorial-partial-v2-{int(post_id)}.json"
        )
        manifest_key = (
            hash_key
            if int(post_id) == int(primary_post_id)
            else f"{hash_key}_{int(post_id)}"
        )
        if evidence.get("archive_name") != expected_name:
            raise TransactionIsolationError(f"media artifact archive identity diverged for {post_id}")
        archived_path = archive / expected_name
        if archived_path.is_symlink():
            raise TransactionIsolationError(f"media artifact archive is a symlink: {expected_name}")
        if evidence.get("present") is True:
            expected_hash = str(evidence.get("sha256") or "")
            if (
                len(expected_hash) != 64
                or hashes.get(manifest_key) != expected_hash
                or not archived_path.is_file()
                or _hash_file(archived_path) != expected_hash
            ):
                raise TransactionIsolationError(f"archived media artifact is missing or changed: {expected_name}")
        elif evidence.get("present") is False:
            if hashes.get(manifest_key, "missing") is not None or archived_path.exists():
                raise TransactionIsolationError(f"absent media artifact was fabricated in archive: {expected_name}")
        else:
            raise TransactionIsolationError(f"media artifact presence is ambiguous for {post_id}")


def _event_matches_transition(event: Mapping[str, Any], transition: Mapping[str, Any]) -> bool:
    expected = {
        "cycle_id": transition["cycle_id"],
        "isolation_id": transition["isolation_id"],
        "authorization_id": transition["authorization_id"],
        "round_number": transition["pending_round"],
        "selected_ids": transition["selected_ids"],
        "isolated_post_ids": transition["isolated_post_ids"],
        "run_ids": transition["run_ids"],
        "isolated_post_id": transition["post_id"],
        "classification": transition["classification"],
        "reconciled_post_ids": transition["reconciled_post_ids"],
        "not_executed_post_ids": transition["not_executed_post_ids"],
        "executed_ids": transition["executed_ids"],
        "round_post_evidence": transition["round_post_evidence"],
        "write_replayed": False,
    }
    return all(event.get(key) == value for key, value in expected.items())


def _validate_transition_archive(
    root: Path, transition: Mapping[str, Any]
) -> tuple[Path, dict[str, Any]]:
    archive = Path(str(transition.get("archive_dir") or ""))
    _validate_archive_scope(root, archive)
    if transition.get("version") != ISOLATION_VERSION or transition.get("phase") not in {
        "prepared", "quarantine_applied", "checkpoint_applied", "completed",
    }:
        raise TransactionIsolationError("transition version or phase is invalid")
    selected = transition.get("selected_ids")
    if (
        not isinstance(selected, list)
        or len(selected) != len(set(selected))
        or int(transition.get("post_id") or 0) not in [int(value) for value in selected]
    ):
        raise TransactionIsolationError("transition selection is invalid")
    record = _read_json(archive / "isolation-record.json")
    if (
        record.get("version") != ISOLATION_VERSION
        or record.get("isolation_id") != transition.get("isolation_id")
        or record.get("cycle_id") != transition.get("cycle_id")
        or record.get("authorization_id") != transition.get("authorization_id")
        or record.get("pending_round") != transition.get("pending_round")
        or record.get("selected_ids") != transition.get("selected_ids")
        or record.get("post_id") != transition.get("post_id")
        or record.get("run_id") != transition.get("run_id")
        or record.get("isolated_post_ids") != transition.get("isolated_post_ids")
        or record.get("run_ids") != transition.get("run_ids")
        or record.get("snapshot_hash") != transition.get("snapshot_hash")
        or _hash_value(record.get("evidence") or {}) != record.get("snapshot_hash")
    ):
        raise TransactionIsolationError("transition record identity diverged")
    for field in (
        "reconciled_post_ids", "not_executed_post_ids", "executed_ids",
        "round_post_evidence", "pre_rounds", "pre_attempts_by_post",
        "pre_seen_post_ids",
    ):
        if transition.get(field) != record.get(field):
            raise TransactionIsolationError(f"transition {field} diverged from its archive record")
    hashes = record.get("archive_hashes")
    if not isinstance(hashes, Mapping):
        raise TransactionIsolationError("archive hash manifest is missing")
    round_evidence = record.get("round_post_evidence")
    if not isinstance(round_evidence, list):
        raise TransactionIsolationError("archived round evidence is missing")
    evidence_by_post = {
        int(item["post_id"]): item
        for item in round_evidence
        if isinstance(item, Mapping) and str(item.get("post_id") or "").isdigit()
    }
    for isolated_id in transition["isolated_post_ids"]:
        detail = evidence_by_post.get(int(isolated_id))
        if not isinstance(detail, Mapping):
            raise TransactionIsolationError(f"archived evidence is missing for isolated post {isolated_id}")
        _validate_media_artifact_archive(
            archive,
            post_id=int(isolated_id),
            primary_post_id=int(transition["post_id"]),
            detail=detail,
            hashes=hashes,
        )
    files = {
        "state": archive / "state.json",
        "journal": archive / "journal.jsonl",
        "target_journal": archive / f"journal-{int(transition['post_id'])}.json",
        "quarantine_before": archive / "quarantine-before.json",
    }
    for isolated_id in transition["isolated_post_ids"]:
        if int(isolated_id) != int(transition["post_id"]):
            files[f"target_journal_{int(isolated_id)}"] = archive / f"journal-{int(isolated_id)}.json"
    for key, snapshot in files.items():
        if not snapshot.is_file() or _hash_file(snapshot) != hashes.get(key):
            raise TransactionIsolationError(f"archived snapshot is incomplete or changed: {key}")
    archived_state = _read_json(archive / "state.json")
    pending = archived_state.get("pending_round")
    if (
        archived_state.get("cycle_id") != transition.get("cycle_id")
        or archived_state.get("status") != "running"
        or archived_state.get("phase") != "ADMISSION"
        or not isinstance(pending, Mapping)
        or int(pending.get("round_number", -1)) != int(transition.get("pending_round", -2))
        or str(pending.get("authorization_id") or "") != str(transition.get("authorization_id") or "")
        or [int(value) for value in (pending.get("preview_selected_ids") or [])]
        != [int(value) for value in selected]
    ):
        raise TransactionIsolationError("archived checkpoint identity diverges from transition")
    archived_attempts = {
        str(key): int(value)
        for key, value in (archived_state.get("attempts_by_post") or {}).items()
    }
    recorded_attempts = {
        str(key): int(value)
        for key, value in (transition.get("pre_attempts_by_post") or {}).items()
    }
    if (
        int(transition.get("pre_rounds", -1)) != int(archived_state.get("rounds") or 0)
        or recorded_attempts != archived_attempts
        or transition.get("pre_seen_post_ids") != archived_state.get("seen_post_ids")
    ):
        raise TransactionIsolationError("prepared round accounting diverges from archived checkpoint")
    archived_journal = _read_json(archive / f"journal-{int(transition['post_id'])}.json")
    if str(archived_journal.get("post_id") or "") != str(transition["post_id"]):
        raise TransactionIsolationError("archived target journal post identity diverges from transition")
    if _journal_matches_authorized_round(
        archived_journal,
        post_id=int(transition["post_id"]),
        cycle_id=str(transition["cycle_id"]),
        authorization_id=str(transition["authorization_id"]),
        pending_round=int(transition["pending_round"]),
    ):
        _validate_isolated_current_journal(
            archived_journal,
            run_id=str(transition.get("run_id") or ""),
            evidence=evidence_by_post.get(int(transition["post_id"])),
        )
    else:
        _validate_historical_target_journal(
            archived_journal,
            post_id=int(transition["post_id"]),
            run_id=str(transition.get("run_id") or ""),
            current_authorization_id=str(transition["authorization_id"]),
        )
    primary_evidence = next(
        (
            item for item in (record.get("round_post_evidence") or [])
            if isinstance(item, Mapping) and int(item.get("post_id") or 0) == int(transition["post_id"])
        ),
        None,
    )
    primary_journal_evidence = (primary_evidence or {}).get("journal")
    if (
        not isinstance(primary_journal_evidence, Mapping)
        or primary_journal_evidence.get("sha256") != hashes.get("target_journal")
        or primary_journal_evidence.get("run_id") != archived_journal.get("run_id")
        or primary_journal_evidence.get("cycle_id") != archived_journal.get("cycle_id")
        or primary_journal_evidence.get("cycle_round") != archived_journal.get("cycle_round")
        or primary_journal_evidence.get("authorization_id") != archived_journal.get("authorization_id")
        or primary_journal_evidence.get("status") != archived_journal.get("status")
    ):
        raise TransactionIsolationError("archived target journal evidence diverges from its preserved identity")
    for isolated_id in transition["isolated_post_ids"]:
        if int(isolated_id) == int(transition["post_id"]):
            continue
        extra_path = archive / f"journal-{int(isolated_id)}.json"
        extra = _read_json(extra_path)
        if str(extra.get("post_id") or "") != str(int(isolated_id)):
            raise TransactionIsolationError(f"archived journal identity diverges for {isolated_id}")
        if str(extra.get("run_id") or "") != str(transition["run_ids"][str(int(isolated_id))]):
            raise TransactionIsolationError(f"archived run identity diverges for {isolated_id}")
        if _journal_matches_authorized_round(
            extra,
            post_id=int(isolated_id),
            cycle_id=str(transition["cycle_id"]),
            authorization_id=str(transition["authorization_id"]),
            pending_round=int(transition["pending_round"]),
        ):
            _validate_isolated_current_journal(
                extra,
                run_id=str(transition["run_ids"][str(int(isolated_id))]),
                evidence=evidence_by_post.get(int(isolated_id)),
            )
        elif extra.get("status") not in {"committing", "committed", "failed_pre_writer", "interrupted_pre_writer"}:
            raise TransactionIsolationError(f"archived journal status is unsafe for {isolated_id}")
    derived = _derive_round_classifications(
        record.get("round_post_evidence") or [],
        isolated_post_ids=transition["isolated_post_ids"],
        require_resolved=True,
    )
    if any(record.get(field) != derived[field] for field in (
        "reconciled_post_ids", "not_executed_post_ids", "executed_ids",
    )):
        raise TransactionIsolationError("archived post classifications do not match their evidence")
    baseline = _read_json(archive / "quarantine-before.json")
    _validate_quarantine_payload(baseline)
    after = _read_json(archive / "quarantine-after.json")
    _assert_quarantine_additions_only(baseline, after)
    expected_after = deepcopy(baseline)
    for isolated_id in transition["isolated_post_ids"]:
        expected_after = _quarantine_with_target(
            expected_after,
            post_id=int(isolated_id),
            reason=f"UNCERTAIN_ISOLATED {transition['isolation_id']}; no replay until independently resolved",
        )
    if after != expected_after:
        raise TransactionIsolationError("archived quarantine differs from the prepared isolation change")
    return archive, record


def _validate_recovery_checkpoint(
    root: Path, transition: Mapping[str, Any], archive: Path
) -> tuple[CycleStateStore, Any, list[dict[str, Any]]]:
    store = CycleStateStore(root)
    state = store.load()
    if state is None or state.cycle_id != transition.get("cycle_id"):
        raise TransactionIsolationError("checkpoint changed during isolation")
    try:
        archived_state = json.loads((archive / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise TransactionIsolationError("archived checkpoint is invalid") from exc
    if not isinstance(archived_state, dict):
        raise TransactionIsolationError("archived checkpoint is invalid")
    current_state = state.to_dict()
    original_pending = deepcopy(archived_state)
    original_pending.pop("updated_at", None)
    comparable_current = deepcopy(current_state)
    comparable_current.pop("updated_at", None)
    mutable = {"pending_round", "rounds", "attempts_by_post", "seen_post_ids", "round_reports"}
    for key in set(original_pending) | set(comparable_current):
        if key not in mutable and comparable_current.get(key) != original_pending.get(key):
            raise TransactionIsolationError("checkpoint changed outside the isolated round accounting")
    original_mutable_equal = all(
        comparable_current.get(key) == original_pending.get(key)
        for key in mutable
    )
    is_original = original_mutable_equal
    attempts = {str(key): int(value) for key, value in (comparable_current.get("attempts_by_post") or {}).items()}
    pre_attempts = {str(key): int(value) for key, value in (transition.get("pre_attempts_by_post") or {}).items()}
    expected_attempts = dict(pre_attempts)
    for post_id in transition["executed_ids"]:
        expected_attempts[str(post_id)] = expected_attempts.get(str(post_id), 0) + 1
    accounted = attempts == expected_attempts
    expected_seen = [int(value) for value in (transition.get("pre_seen_post_ids") or [])]
    for post_id in transition["executed_ids"]:
        if int(post_id) not in expected_seen:
            expected_seen.append(int(post_id))
    current_seen = [int(value) for value in (comparable_current.get("seen_post_ids") or [])]
    original_reports = original_pending.get("round_reports") or []
    current_reports = comparable_current.get("round_reports") or []
    expected_report = {
        "event": ISOLATION_EVENT,
        "isolation_id": transition["isolation_id"],
        "round": int(transition["pending_round"]),
        "next_round_number": max(
            int(transition.get("pre_rounds") or 0), int(transition["pending_round"])
        ) + 1,
        "phase": archived_state.get("phase"),
        "preview_selected_ids": transition["selected_ids"],
        "executed_ids": transition["executed_ids"],
        "reconciled_post_ids": transition["reconciled_post_ids"],
        "not_executed_post_ids": transition["not_executed_post_ids"],
        "unresolved_post_ids": transition["isolated_post_ids"],
        "round_post_evidence": transition["round_post_evidence"],
        "isolated_post_ids": transition["isolated_post_ids"],
        "write_replayed": False,
        "operator_authorization": transition["operator_authorization"],
        "archive_dir": transition["archive_dir"],
    }
    report_matches = (
        isinstance(original_reports, list)
        and isinstance(current_reports, list)
        and len(current_reports) == len(original_reports) + 1
        and current_reports[:-1] == original_reports
        and isinstance(current_reports[-1], Mapping)
        and all(current_reports[-1].get(key) == value for key, value in expected_report.items())
    )
    is_cleared = (
        comparable_current.get("pending_round") is None
        and int(comparable_current.get("rounds") or 0) == max(
            int(transition.get("pre_rounds") or 0), int(transition["pending_round"])
        )
        and accounted
        and current_seen == expected_seen
        and report_matches
    )
    if not (is_original or is_cleared):
        raise TransactionIsolationError("checkpoint no longer matches the prepared isolation transition")
    if is_original and transition.get("phase") == "checkpoint_applied":
        raise TransactionIsolationError("checkpoint phase claims apply but pending authorization remains")
    isolated_post_ids = transition.get("isolated_post_ids")
    if not isinstance(isolated_post_ids, list) or not isolated_post_ids:
        raise TransactionIsolationError("isolated journal identities are missing")
    for isolated_id in isolated_post_ids:
        target_path = Path(root) / "work" / "v2-journal" / f"{int(isolated_id)}.json"
        archived_target = archive / f"journal-{int(isolated_id)}.json"
        if (
            not target_path.is_file()
            or not archived_target.is_file()
            or _hash_file(target_path) != _hash_file(archived_target)
        ):
            raise TransactionIsolationError(
                f"isolated journal changed during recovery: {int(isolated_id)}"
            )
    events = _event_records(root, str(transition["isolation_id"]))
    if len(events) > 1:
        raise TransactionIsolationError("duplicate isolation events already exist")
    if events and (not _event_matches_transition(events[0], transition) or not is_cleared):
        raise TransactionIsolationError("existing isolation event conflicts with checkpoint")
    if transition.get("phase") == "checkpoint_applied" and not events:
        raise TransactionIsolationError("checkpoint phase claims apply but journal event is absent")
    return store, state, events


def _validate_live_round_evidence(
    root: Path, client, transition: Mapping[str, Any]
) -> None:
    """Re-read all selected journals, WordPress states, and Registry evidence."""
    selected_ids = [int(value) for value in transition.get("selected_ids", [])]
    expected = transition.get("round_post_evidence")
    if (
        not selected_ids
        or not isinstance(expected, list)
        or [int(item.get("post_id") or 0) for item in expected if isinstance(item, Mapping)]
        != selected_ids
    ):
        raise TransactionIsolationError("prepared round evidence does not match the selected IDs")
    actual = [
        _round_post_evidence(
            root,
            client,
            post_id=post_id,
            cycle_id=str(transition["cycle_id"]),
            authorization_id=str(transition["authorization_id"]),
            pending_round=int(transition["pending_round"]),
            capture_media_artifacts=any(
                isinstance(item, Mapping)
                and int(item.get("post_id") or 0) == post_id
                and item.get("media_artifacts") is not None
                for item in expected
            ),
        )
        for post_id in selected_ids
    ]
    primary_id = int(transition["post_id"])
    primary = next((item for item in expected if int(item.get("post_id") or 0) == primary_id), None)
    primary_journal = (primary or {}).get("journal")
    primary_hash = str((primary_journal or {}).get("sha256") or "")
    if (
        not isinstance(primary_journal, Mapping)
        or primary_journal.get("present") is not True
        or len(primary_hash) != 64
        or any(char not in "0123456789abcdef" for char in primary_hash)
    ):
        raise TransactionIsolationError("prepared primary journal evidence is invalid")
    if _hash_value(actual) != _hash_value(expected):
        raise TransactionIsolationError(
            "live WordPress, MediaRegistry, or selected journal evidence changed since preparation"
        )


def _match_preview_evidence(
    root: Path,
    evidence: Mapping[str, Any],
    preview: Mapping[str, Any] | None,
    *,
    isolation_id: str,
    allow_quarantine_additions: bool,
    archive: Path,
) -> dict[str, Any]:
    if (
        not isinstance(preview, Mapping)
        or preview.get("isolation_id") != isolation_id
        or not isinstance(preview.get("evidence"), Mapping)
    ):
        raise TransactionIsolationError("apply requires the exact preview evidence")
    expected_evidence = deepcopy(dict(preview["evidence"]))
    current_evidence = deepcopy(dict(evidence))
    if allow_quarantine_additions:
        baseline_path = archive / "quarantine-before.json"
        expected_hash = str(
            ((expected_evidence.get("checkpoint") or {}).get("quarantine_sha256")) or ""
        )
        current_policy = _read_json(Path(root) / "config" / "v2-quarantine.json")
        if baseline_path.is_file():
            baseline = _read_json(baseline_path)
            if not expected_hash or _hash_file(baseline_path) != expected_hash:
                raise TransactionIsolationError("partial archive quarantine does not match the preview")
            _assert_quarantine_additions_only(baseline, current_policy)
        elif not expected_hash or _hash_file(Path(root) / "config" / "v2-quarantine.json") != expected_hash:
            raise TransactionIsolationError("partial archive lacks the original quarantine snapshot")
        current_evidence["checkpoint"]["quarantine_sha256"] = expected_hash
    if _hash_value(current_evidence) != str(preview.get("snapshot_hash") or ""):
        raise TransactionIsolationError("checkpoint, journals, WordPress, Registry, or quarantine changed since preview")
    return expected_evidence


def _ensure_snapshot(source: Path, destination: Path, expected_hash: str) -> str:
    if destination.is_symlink():
        raise TransactionIsolationError(f"partial archive snapshot is a symlink: {destination.name}")
    if destination.exists():
        if not destination.is_file() or _hash_file(destination) != expected_hash:
            raise TransactionIsolationError(f"partial archive snapshot conflicts: {destination.name}")
        return expected_hash
    if not source.is_file() or _hash_file(source) != expected_hash:
        raise TransactionIsolationError(f"source changed before archive preparation: {source.name}")
    return _copy_verified(source, destination)


def _ensure_absent_snapshot(source: Path, destination: Path) -> None:
    if source.is_symlink() or source.exists():
        raise TransactionIsolationError(f"media artifact appeared after preview: {source.name}")
    if destination.is_symlink() or destination.exists():
        raise TransactionIsolationError(f"archive contains an artifact recorded as absent: {destination.name}")


def _prepare_transition(
    root: Path,
    *,
    archive: Path,
    transition_path: Path,
    checkpoint: Mapping[str, Any],
    preview_evidence: Mapping[str, Any],
    snapshot_hash: str,
    isolation_id: str,
    cycle_id: str,
    authorization_id: str,
    selected_ids: list[int],
    pending_round: int,
    post_id: int,
    run_id: str,
    isolated_post_ids: list[int],
    run_ids: Mapping[int, str],
    operator_authorization: str,
    round_post_evidence: list[dict[str, Any]],
    classifications: Mapping[str, Any],
) -> dict[str, Any]:
    _validate_archive_scope(root, archive)
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.exists() and not archive.is_dir():
        raise TransactionIsolationError("archive destination is not a directory")
    archive.mkdir(exist_ok=True)
    sources = {
        "state": Path(checkpoint["paths"]["state"]),
        "journal": Path(checkpoint["paths"]["journal"]),
        "target_journal": Path(checkpoint["paths"]["target_journal"]),
        "quarantine_before": Path(root) / "config" / "v2-quarantine.json",
    }
    destinations = {
        "state": archive / "state.json",
        "journal": archive / "journal.jsonl",
        "target_journal": archive / f"journal-{post_id}.json",
        "quarantine_before": archive / "quarantine-before.json",
    }
    preview_checkpoint = preview_evidence["checkpoint"]
    hashes = {
        "state": str(preview_checkpoint["state_sha256"]),
        "journal": str(preview_checkpoint["journal_sha256"]),
        "target_journal": str(preview_checkpoint["target_journal_sha256"]),
        "quarantine_before": str(preview_checkpoint["quarantine_sha256"]),
    }
    for isolated_id in isolated_post_ids:
        if int(isolated_id) == int(post_id):
            continue
        detail = next(item for item in round_post_evidence if int(item["post_id"]) == int(isolated_id))
        key = f"target_journal_{int(isolated_id)}"
        sources[key] = Path(root) / "work" / "v2-journal" / f"{int(isolated_id)}.json"
        destinations[key] = archive / f"journal-{int(isolated_id)}.json"
        hashes[key] = str(detail["journal"]["sha256"])
    archived = {
        key: _ensure_snapshot(sources[key], destinations[key], hashes[key])
        for key in sources
    }
    for isolated_id in isolated_post_ids:
        detail = next(
            item for item in round_post_evidence
            if int(item["post_id"]) == int(isolated_id)
        )
        artifacts = detail.get("media_artifacts")
        if not isinstance(artifacts, Mapping):
            raise TransactionIsolationError(f"media artifact evidence is missing for {isolated_id}")
        for kind, hash_key in (
            ("partial", "media_partial"),
            ("sidecar", "media_sidecar"),
        ):
            artifact = artifacts.get(kind)
            if not isinstance(artifact, Mapping):
                raise TransactionIsolationError(f"{kind} evidence is missing for {isolated_id}")
            name = str(artifact.get("archive_name") or "")
            expected_name = (
                f"editorial-partial-{int(isolated_id)}.json"
                if kind == "partial"
                else f"editorial-partial-v2-{int(isolated_id)}.json"
            )
            if name != expected_name:
                raise TransactionIsolationError(f"media artifact path diverged for {isolated_id}")
            source = _media_artifact_path(root, int(isolated_id), kind)
            destination = archive / name
            scoped_key = (
                hash_key if int(isolated_id) == int(post_id)
                else f"{hash_key}_{int(isolated_id)}"
            )
            if artifact.get("present") is True:
                expected_hash = str(artifact.get("sha256") or "")
                archived[scoped_key] = _ensure_snapshot(source, destination, expected_hash)
            elif artifact.get("present") is False:
                _ensure_absent_snapshot(source, destination)
                archived[scoped_key] = None
            else:
                raise TransactionIsolationError(f"media artifact presence is ambiguous for {isolated_id}")
    baseline = _read_json(destinations["quarantine_before"])
    _validate_quarantine_payload(baseline)
    current_policy = _read_json(sources["quarantine_before"])
    _assert_quarantine_additions_only(baseline, current_policy)
    reason = f"UNCERTAIN_ISOLATED {isolation_id}; no replay until independently resolved"
    after_path = archive / "quarantine-after.json"
    if after_path.exists():
        after = _read_json(after_path)
        _assert_quarantine_additions_only(baseline, after)
        expected_after = deepcopy(baseline)
        for isolated_id in isolated_post_ids:
            expected_after = _quarantine_with_target(
                expected_after, post_id=int(isolated_id), reason=reason
            )
        if after != expected_after:
            raise TransactionIsolationError("prepared quarantine snapshot conflicts with original policy")
    else:
        prepared_after = deepcopy(baseline)
        for isolated_id in isolated_post_ids:
            prepared_after = _quarantine_with_target(
                prepared_after, post_id=int(isolated_id), reason=reason
            )
        _write_json_once(after_path, prepared_after)

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
        "isolated_post_ids": isolated_post_ids,
        "run_ids": {str(key): value for key, value in run_ids.items()},
        "pre_rounds": int(checkpoint["state"].get("rounds") or 0),
        "pre_attempts_by_post": {
            str(key): int(value)
            for key, value in (checkpoint["state"].get("attempts_by_post") or {}).items()
        },
        "pre_seen_post_ids": [int(value) for value in (checkpoint["state"].get("seen_post_ids") or [])],
        "snapshot_hash": snapshot_hash,
        "evidence": dict(preview_evidence),
        "round_post_evidence": round_post_evidence,
        "reconciled_post_ids": classifications["reconciled_post_ids"],
        "not_executed_post_ids": classifications["not_executed_post_ids"],
        "executed_ids": classifications["executed_ids"],
        "archive_hashes": archived,
        "reason": "single writer transaction remains uncertain; isolate without replay or commit claim",
        "operator_authorization": operator_authorization,
    }
    record_path = archive / "isolation-record.json"
    if record_path.exists():
        existing = _read_json(record_path)
        if existing != record:
            raise TransactionIsolationError("prepared isolation record conflicts with preview evidence")
    else:
        _write_json_once(record_path, record)

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
        "isolated_post_ids": isolated_post_ids,
        "run_ids": {str(key): value for key, value in run_ids.items()},
        "pre_rounds": int(checkpoint["state"].get("rounds") or 0),
        "pre_attempts_by_post": {
            str(key): int(value)
            for key, value in (checkpoint["state"].get("attempts_by_post") or {}).items()
        },
        "pre_seen_post_ids": [int(value) for value in (checkpoint["state"].get("seen_post_ids") or [])],
        "snapshot_hash": snapshot_hash,
        "classification": "UNCERTAIN_ISOLATED",
        "round_post_evidence": round_post_evidence,
        "reconciled_post_ids": classifications["reconciled_post_ids"],
        "not_executed_post_ids": classifications["not_executed_post_ids"],
        "executed_ids": classifications["executed_ids"],
        "operator_authorization": operator_authorization,
    }
    if transition_path.exists():
        existing_transition = _read_json(transition_path)
        if existing_transition != transition:
            raise TransactionIsolationError("isolation transition conflicts with prepared archive")
        transition = existing_transition
    else:
        transition_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json_once(transition_path, transition)
    return transition


def _apply_transition(root: Path, transition: dict[str, Any], client) -> dict[str, Any]:
    root = Path(root)
    path = _transition_path(root, str(transition["isolation_id"]))
    if path.is_symlink():
        raise TransactionIsolationError("transition record path must not be a symlink")
    if transition.get("phase") == "completed":
        return {"status": "already_applied", **{k: transition[k] for k in ("isolation_id", "archive_dir", "classification")}}
    archive, _record = _validate_transition_archive(root, transition)
    baseline = _read_json(archive / "quarantine-before.json")
    store, state, events = _validate_recovery_checkpoint(root, transition, archive)
    _validate_live_round_evidence(root, client, transition)
    _write_quarantine_additively(
        root,
        baseline=baseline,
        post_ids=[int(value) for value in transition["isolated_post_ids"]],
        reason=f"UNCERTAIN_ISOLATED {transition['isolation_id']}; no replay until independently resolved",
    )
    if transition.get("phase") == "prepared":
        transition["phase"] = "quarantine_applied"
        atomic_write_text(path, _json(transition))

    pending = state.pending_round
    expected = transition["selected_ids"]
    fields = {
        "isolation_id": transition["isolation_id"],
        "authorization_id": transition["authorization_id"],
        "round_number": transition["pending_round"],
        "selected_ids": expected,
        "isolated_post_ids": transition["isolated_post_ids"],
        "run_ids": transition["run_ids"],
        "isolated_post_id": transition["post_id"],
        "classification": transition["classification"],
        "reconciled_post_ids": transition["reconciled_post_ids"],
        "not_executed_post_ids": transition["not_executed_post_ids"],
        "executed_ids": transition["executed_ids"],
        "round_post_evidence": transition["round_post_evidence"],
        "write_replayed": False,
        "operator_authorization": transition["operator_authorization"],
        "archive_dir": transition["archive_dir"],
    }
    if pending is not None:
        if str(pending.get("authorization_id") or "") != transition["authorization_id"] or [int(v) for v in (pending.get("preview_selected_ids") or [])] != expected:
            raise TransactionIsolationError("checkpoint changed before isolation apply")
        if events:
            raise TransactionIsolationError("isolation event exists before checkpoint transition")
        pre_attempts = {
            int(key): int(value)
            for key, value in (transition.get("pre_attempts_by_post") or {}).items()
        }
        for executed_id in transition["executed_ids"]:
            current = int(state.attempts_by_post.get(int(executed_id), 0))
            state.attempts_by_post[int(executed_id)] = max(current, pre_attempts.get(int(executed_id), 0) + 1)
            if int(executed_id) not in state.seen_post_ids:
                state.seen_post_ids.append(int(executed_id))
        state.rounds = max(int(state.rounds), int(transition["pending_round"]))
        state.pending_round = None
        report = {
            "event": ISOLATION_EVENT,
            "isolation_id": transition["isolation_id"],
            "round": int(transition["pending_round"]),
            "next_round_number": int(state.rounds) + 1,
            "phase": state.phase,
            "preview_selected_ids": expected,
            "executed_ids": transition["executed_ids"],
            "reconciled_post_ids": transition["reconciled_post_ids"],
            "not_executed_post_ids": transition["not_executed_post_ids"],
            "unresolved_post_ids": transition["isolated_post_ids"],
            "round_post_evidence": transition["round_post_evidence"],
            "isolated_post_ids": transition["isolated_post_ids"],
            "write_replayed": False,
            "operator_authorization": transition["operator_authorization"],
            "archive_dir": transition["archive_dir"],
        }
        state.round_reports.append(report)
        _ensure_journal_append_boundary(store.journal_path)
        store.save(state, event=ISOLATION_EVENT, **fields)
    elif not events:
        # CycleStateStore writes the checkpoint before appending its event.
        # Re-saving the already-cleared state repairs that exact gap without
        # incrementing attempts a second time.
        if not any(item.get("event") == ISOLATION_EVENT and item.get("isolation_id") == transition["isolation_id"] for item in state.round_reports):
            report = {
                "event": ISOLATION_EVENT,
                "isolation_id": transition["isolation_id"],
                "round": int(transition["pending_round"]),
                "next_round_number": max(int(state.rounds), int(transition["pending_round"])) + 1,
                "phase": state.phase,
                "preview_selected_ids": expected,
                "executed_ids": transition["executed_ids"],
                "reconciled_post_ids": transition["reconciled_post_ids"],
                "not_executed_post_ids": transition["not_executed_post_ids"],
                "unresolved_post_ids": transition["isolated_post_ids"],
                "round_post_evidence": transition["round_post_evidence"],
                "isolated_post_ids": transition["isolated_post_ids"],
                "write_replayed": False,
                "operator_authorization": transition["operator_authorization"],
                "archive_dir": transition["archive_dir"],
            }
            state.round_reports.append(report)
        _ensure_journal_append_boundary(store.journal_path)
        store.save(state, event=ISOLATION_EVENT, **fields)
    events = _event_records(root, str(transition["isolation_id"]))
    if len(events) != 1 or not _event_matches_transition(events[0], transition):
        raise TransactionIsolationError("isolation journal event could not be verified")
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
    run_id: str | None,
    apply: bool = False,
    preview: Mapping[str, Any] | None = None,
    operator_authorization: str | None = None,
    isolated_post_ids: list[int] | None = None,
    run_ids: Mapping[int, str] | None = None,
) -> dict[str, Any]:
    """Preview/apply an idempotent, no-WordPress-write isolation transition."""
    root = Path(root)
    isolated_ids = sorted({int(value) for value in (isolated_post_ids or [post_id])})
    if int(post_id) not in isolated_ids or len(isolated_ids) == 0:
        raise TransactionIsolationError("isolated post IDs must include the primary post")
    run_map = {
        int(value): str((run_ids or {}).get(int(value)) or (run_id if int(value) == int(post_id) else ""))
        for value in isolated_ids
    }
    if any(not value for value in run_map.values()):
        raise TransactionIsolationError("every isolated post requires its exact run_id")
    run_id = run_map[int(post_id)]
    lock = RunSessionLock(root / "work" / "v2-run.lock")
    if not lock.acquire():
        return {"status": "blocked", "reason": "global_lock_unavailable", "wordpress_writes": 0}
    try:
        if len(selected_ids) != len(set(selected_ids)) or int(post_id) not in selected_ids:
            raise TransactionIsolationError("selected IDs must be unique and include the isolated post")
        isolation_id = _hash_value({
            "cycle_id": cycle_id,
            "authorization_id": authorization_id,
            "round": pending_round,
            "post_id": post_id,
            "run_id": run_id,
            "isolated_post_ids": isolated_ids,
            "run_ids": run_map,
        })[:24]
        transition_path = _transition_path(root, isolation_id)
        if transition_path.is_symlink():
            raise TransactionIsolationError("transition record path must not be a symlink")
        if transition_path.is_file():
            transition = _read_json(transition_path)
            if transition.get("phase") == "completed":
                return {
                    "status": "already_applied",
                    "isolation_id": isolation_id,
                    "archive_dir": transition["archive_dir"],
                    "classification": transition["classification"],
                    "wordpress_writes": 0,
                }
            if not apply:
                return {
                    "status": "pending_transition",
                    "phase": transition.get("phase"),
                    "isolation_id": isolation_id,
                    "archive_dir": transition.get("archive_dir"),
                    "wordpress_writes": 0,
                }
            if not operator_authorization or not str(operator_authorization).strip():
                raise TransactionIsolationError("explicit operator authorization is required")
            expected_inputs = {
                "isolation_id": isolation_id,
                "cycle_id": cycle_id,
                "authorization_id": authorization_id,
                "pending_round": int(pending_round),
                "selected_ids": selected_ids,
                "post_id": int(post_id),
                "run_id": run_id,
                "isolated_post_ids": isolated_ids,
                "run_ids": {str(key): value for key, value in run_map.items()},
                "operator_authorization": str(operator_authorization),
            }
            if any(transition.get(key) != value for key, value in expected_inputs.items()):
                raise TransactionIsolationError("pending transition does not match the requested authorization")
            return _apply_transition(root, transition, client)

        checkpoint = _validate_checkpoint(
            root,
            cycle_id=cycle_id,
            authorization_id=authorization_id,
            selected_ids=selected_ids,
            pending_round=pending_round,
            target_post_id=int(post_id),
        )
        journal = checkpoint["journal"]
        if str(journal.get("post_id") or "") not in {str(post_id), ""}:
            raise TransactionIsolationError("post_id diverged")
        if checkpoint.get("target_journal_is_current"):
            if journal.get("writer_started") is True:
                _validate_uncertain(journal, run_id=run_id)
            else:
                _validate_pre_writer_uncertain_journal(journal, run_id=run_id)
        else:
            _validate_historical_target_journal(
                journal,
                post_id=int(post_id),
                run_id=run_id,
                current_authorization_id=authorization_id,
            )
        round_post_evidence = [
            _round_post_evidence(
                root,
                client,
                post_id=selected_id,
                cycle_id=cycle_id,
                authorization_id=authorization_id,
                pending_round=int(pending_round),
                target_journal=journal if selected_id == int(post_id) else None,
                capture_media_artifacts=selected_id in isolated_ids,
            )
            for selected_id in selected_ids
        ]
        evidence_by_post = {int(item["post_id"]): item for item in round_post_evidence}
        for isolated_id in isolated_ids:
            detail = evidence_by_post[isolated_id]
            if str(detail["journal"].get("run_id") or "") != run_map[isolated_id]:
                raise TransactionIsolationError(f"run_id diverged for isolated post {isolated_id}")
            if detail["classification"] != "UNCERTAIN_ISOLATED":
                raise TransactionIsolationError(
                    f"isolated post {isolated_id} is not uncertain: {detail['classification']}"
                )
        target_evidence = next(
            item for item in round_post_evidence if int(item["post_id"]) == int(post_id)
        )
        checkpoint_summary = {
            "cycle_id": cycle_id,
            "status": checkpoint["state"].get("status"),
            "phase": checkpoint["state"].get("phase"),
            "pending_round": int(pending_round),
            "authorization_id": authorization_id,
            "selected_ids": selected_ids,
            "state_sha256": checkpoint["state_sha256"],
            "journal_sha256": checkpoint["journal_sha256"],
            "target_journal_sha256": checkpoint["target_journal_sha256"],
            "quarantine_sha256": checkpoint["quarantine_sha256"],
        }
        evidence = {
            "classification": target_evidence["classification"],
            "checkpoint": checkpoint_summary,
            "isolated_post_ids": isolated_ids,
            "run_ids": run_map,
            "isolated_post": target_evidence,
            "round_post_evidence": round_post_evidence,
        }
        snapshot_hash = _hash_value(evidence)
        if apply:
            if not operator_authorization or not str(operator_authorization).strip():
                raise TransactionIsolationError("explicit operator authorization is required")
            archive = root / "work" / "v2-cycle" / "archives" / f"uncertain-{isolation_id}"
            expected_evidence = _match_preview_evidence(
                root,
                evidence,
                preview,
                isolation_id=isolation_id,
                allow_quarantine_additions=archive.exists(),
                archive=archive,
            )
            for isolated_id in isolated_ids:
                current_journal = _read_json(
                    Path(root) / "work" / "v2-journal" / f"{isolated_id}.json"
                )
                if (
                    _journal_matches_authorized_round(
                        current_journal,
                        post_id=isolated_id,
                        cycle_id=cycle_id,
                        authorization_id=authorization_id,
                        pending_round=int(pending_round),
                    )
                    and current_journal.get("writer_started") is False
                ):
                    _validate_isolated_current_journal(
                        current_journal,
                        run_id=run_map[isolated_id],
                        evidence=evidence_by_post[isolated_id],
                    )
            classifications = _derive_round_classifications(
                round_post_evidence,
                isolated_post_ids=isolated_ids,
                require_resolved=True,
            )
            transition = _prepare_transition(
                root,
                archive=archive,
                transition_path=transition_path,
                checkpoint=checkpoint,
                preview_evidence=expected_evidence,
                snapshot_hash=str(preview["snapshot_hash"]),
                isolation_id=isolation_id,
                cycle_id=cycle_id,
                authorization_id=authorization_id,
                selected_ids=selected_ids,
                pending_round=int(pending_round),
                post_id=int(post_id),
                run_id=run_id,
                isolated_post_ids=isolated_ids,
                run_ids=run_map,
                operator_authorization=str(operator_authorization),
                round_post_evidence=round_post_evidence,
                classifications=classifications,
            )
            return _apply_transition(root, transition, client)
        classifications = _derive_round_classifications(
            round_post_evidence,
            isolated_post_ids=isolated_ids,
            require_resolved=False,
        )
        return {
            "status": "preview",
            "classification": evidence["classification"],
            "isolation_id": isolation_id,
            "snapshot_hash": snapshot_hash,
            "evidence": evidence,
            "selected_ids": selected_ids,
            "round_classifications": classifications,
            "wordpress_writes": 0,
        }
    finally:
        lock.release()


__all__ = ["TransactionIsolationError", "classify_transaction", "isolate_uncertain_transaction"]
