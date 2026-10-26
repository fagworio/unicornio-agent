"""Safe, epoch-scoped administrative reset for V2.

The reset is deliberately explicit: preview is read-only, apply requires an
unchanged preview plus an operator authorization, and uncertain external
 effects are excluded rather than guessed away.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..backup import atomic_write_text
from ..pipeline_v2.cycle_state import CycleStateStore
from ..pipeline_v2.lock import RunSessionLock
from ..pipeline_v2.model import CURRENT_RETRY_POLICY_VERSION, LifecycleState, Phase, RetryInfo, WorkState


LEGACY_KEYS = (
    "_hermes_state", "_hermes_attempts", "_hermes_next_retry_at",
    "_hermes_last_error", "_hermes_partial_kind", "_hermes_media_required",
    "_hermes_media_completed", "_hermes_media_missing", "_hermes_no_progress_attempts",
    "_hermes_ready_manifest", "_hermes_ready_hash",
)
HUMAN_REOPEN_KEYS = ("_hermes_human_reopened_at", "_hermes_human_reopened_from")
PROTECTED_POST_ID = 115757


class ResetEpochError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_sha(path: Path) -> str:
    if not path.exists():
        return "missing"
    return _sha(path.read_bytes())


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def _state_from_post(post: dict[str, Any]) -> tuple[WorkState | None, str | None]:
    raw = (post.get("meta") or {}).get("_hermes_work_state")
    if raw in (None, ""):
        return None, None
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        return WorkState.from_dict(value), None
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return None, f"invalid_canonical_work_state:{type(exc).__name__}"


def _quarantine(root: Path) -> tuple[dict[int, dict[str, Any]], str]:
    path = root / "config" / "v2-quarantine.json"
    payload = _read_json(path)
    if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("posts"), list):
        raise ResetEpochError("invalid or missing V2 quarantine policy")
    rows = {int(row["post_id"]): row for row in payload["posts"] if isinstance(row, dict) and str(row.get("post_id", "")).isdigit()}
    return rows, _file_sha(path)


def _cycle_snapshot(root: Path) -> dict[str, Any]:
    state_path = root / "work" / "v2-cycle" / "state.json"
    value = _read_json(state_path, {}) or {}
    pending = value.get("pending_round") or {}
    unresolved = []
    for report in value.get("round_reports") or []:
        for item in report.get("unresolved_posts") or []:
            if isinstance(item, dict) and item.get("post_id") is not None:
                unresolved.append(int(item["post_id"]))
    return {
        "cycle_id": value.get("cycle_id"), "status": value.get("status"),
        "phase": value.get("phase"), "stop_reason": value.get("stop_reason"),
        "pending_round": pending.get("round_number"),
        "authorization_id": pending.get("authorization_id"),
        "selected_ids": pending.get("preview_selected_ids") or [],
        "unresolved_ids": sorted(set(unresolved)),
        "sha256": _file_sha(state_path),
    }


def _post_fingerprint(post: dict[str, Any]) -> dict[str, Any]:
    meta = post.get("meta") or {}
    relevant_meta = {key: meta.get(key) for key in sorted(set(LEGACY_KEYS + HUMAN_REOPEN_KEYS + ("_hermes_work_state",)))}
    content = (post.get("content") or {}).get("raw", (post.get("content") or {}).get("rendered", ""))
    return {
        "id": int(post["id"]), "status": post.get("status"),
        "content_sha256": _sha(str(content).encode("utf-8")),
        "featured_media": int(post.get("featured_media") or 0),
        "meta": relevant_meta,
    }


def _initial_state() -> WorkState:
    return WorkState(
        state=LifecycleState.PENDING,
        phase=Phase.RELEVANCE,
        blocker=None,
        retry=RetryInfo(policy_version=CURRENT_RETRY_POLICY_VERSION),
        relevance_approved=False,
    )


def _protected_ids(root: Path) -> set[int]:
    result = {PROTECTED_POST_ID}
    cycle = _cycle_snapshot(root)
    result.update(cycle.get("unresolved_ids") or [])
    return result


def _classify(root: Path, post: dict[str, Any], quarantine: dict[int, dict[str, Any]], protected: set[int]) -> dict[str, Any]:
    pid = int(post["id"])
    status = str(post.get("status") or "")
    state, state_error = _state_from_post(post)
    if status == "publish" or (state and state.state is LifecycleState.PUBLISHED):
        return {"post_id": pid, "classification": "preserve_published", "reason": "published"}
    if pid in quarantine:
        return {"post_id": pid, "classification": "protected_quarantine", "reason": quarantine[pid].get("reason_code", "quarantine")}
    if pid in protected:
        return {"post_id": pid, "classification": "protected_transaction", "reason": "uncertain_external_effect"}
    if status not in {"pending", "awaiting_human"}:
        return {"post_id": pid, "classification": "exclude_status", "reason": f"wordpress_status:{status}"}
    if state_error:
        return {"post_id": pid, "classification": "exception", "reason": state_error}
    if state and state.state is LifecycleState.HUMAN_REQUIRED and status != "awaiting_human":
        return {"post_id": pid, "classification": "exception", "reason": "human_required_status_mismatch"}
    return {"post_id": pid, "classification": "reset", "reason": "safe_unpublished_editorial_state", "previous_state": state.to_dict() if state else None}


def _all_posts(client: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for status in ("pending", "awaiting_human", "publish"):
        page = 1
        while True:
            batch = client.list_pending(page=page, per_page=100, status=status)
            if not batch:
                break
            result.extend(batch)
            if len(batch) < 100:
                break
            page += 1
    unique = {int(item["id"]): item for item in result if isinstance(item, dict) and item.get("id") is not None}
    return [unique[key] for key in sorted(unique)]


def _active_artifact_roots(root: Path) -> list[Path]:
    """Return only the roots that production readers treat as active."""
    roots = [Path(root) / "backups", Path(root) / "work" / "backups"]
    return [path for path in roots if path.exists()]


def _registry_projection(source: Path, target: Path, epoch_id: str) -> dict[str, int]:
    source_conn = sqlite3.connect(str(source))
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    target_conn = sqlite3.connect(str(target))
    target_conn.execute("PRAGMA journal_mode=WAL")
    target_conn.execute("PRAGMA foreign_keys=ON")
    tables = [row[0] for row in source_conn.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name")]
    copied: dict[str, int] = {}
    for table in tables:
        ddl = source_conn.execute("select sql from sqlite_master where type='table' and name=?", (table,)).fetchone()[0]
        target_conn.execute(ddl)
    for table in ("media_assets", "media_decisions", "schema_meta"):
        columns = [row[1] for row in source_conn.execute(f"pragma table_info({table})")]
        if table == "media_decisions" and "scope" in columns:
            rows = source_conn.execute(
                "select " + ",".join(columns) + " from media_decisions "
                "where lower(coalesce(scope,'')) in ('asset','candidate','global','library')"
            ).fetchall()
        else:
            rows = source_conn.execute(f"select {','.join(columns)} from {table}").fetchall()
        if rows:
            marks = ",".join("?" for _ in columns)
            target_conn.executemany(f"insert into {table} ({','.join(columns)}) values ({marks})", rows)
        copied[table] = len(rows)
    target_conn.execute("insert or replace into schema_meta(key,value) values(?,?)", ("active_epoch_id", epoch_id))
    target_conn.execute("insert or replace into schema_meta(key,value) values(?,?)", ("projection_kind", "assets_and_qualification_only"))
    target_conn.commit()
    integrity = target_conn.execute("pragma integrity_check").fetchone()[0]
    foreign = target_conn.execute("pragma foreign_key_check").fetchall()
    target_conn.close(); source_conn.close()
    if integrity != "ok" or foreign:
        raise ResetEpochError(f"projected registry integrity failure: {integrity} {foreign}")
    return copied


def _active_processes() -> list[str]:
    found = []
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            cmd = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
        except OSError:
            continue
        if any(token in cmd for token in ("v2-cycle", "v2-run", "publish-ready", "unicorniohater-publish.sh")):
            found.append(cmd)
    return found


def _copy_tree_verified(source: Path, target: Path) -> dict[str, str]:
    if not source.exists():
        return {}
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, dirs_exist_ok=True)
    expected: dict[str, str] = {}
    for path in sorted(source.rglob("*")):
        if path.is_file():
            relative = str(path.relative_to(source))
            copied = target / relative
            if not copied.is_file() or _file_sha(path) != _file_sha(copied):
                raise ResetEpochError(f"archive copy verification failed: {path}")
            expected[relative] = _file_sha(path)
    return expected


def _archive_active_artifacts(root: Path, archive: Path) -> dict[str, Any]:
    roots = _active_artifact_roots(root)
    manifest: dict[str, Any] = {"roots": {}, "space_required": 0}
    for source in roots:
        size = sum(path.stat().st_size for path in source.rglob("*") if path.is_file())
        manifest["space_required"] += size
        target = archive / ("root-backups" if source == root / "backups" else "work-backups")
        manifest["roots"][str(source)] = _copy_tree_verified(source, target)
    if shutil.disk_usage(archive).free < int(manifest["space_required"] * 1.1) + 1024 * 1024:
        raise ResetEpochError("insufficient disk space for verified artifact archive")
    return manifest


def _post_reset_journal(path: Path, payload: dict[str, Any]) -> None:
    _write_json(path, payload)


def _reset_transition_path(root: Path, operation_id: str) -> Path:
    return root / "work" / "v2-epochs" / "transitions" / f"{operation_id}.json"


def _human_required_evidence(root: Path, post: dict[str, Any]) -> dict[str, Any]:
    pid = int(post["id"])
    directory = root / "backups" / str(pid)
    artifacts = sorted(path.name for path in directory.glob("*") if path.is_file()) if directory.is_dir() else []
    journal_paths = [
        root / "work" / "v2-journal" / f"{pid}.json",
        root / "work" / "v2-epochs" / "active" / f"{pid}.json",
    ]
    journal_present = [str(path.relative_to(root)) for path in journal_paths if path.is_file()]
    uncertain_markers = [
        root / "work" / "v2-publication-uncertain" / f"{pid}.json",
        root / "work" / "v2-uncertain" / f"{pid}.json",
    ]
    uncertain_present = [str(path.relative_to(root)) for path in uncertain_markers if path.is_file()]
    registry_rows: dict[str, int] = {}
    registry = root / "work" / "media_registry.sqlite"
    if registry.is_file():
        try:
            with sqlite3.connect(f"file:{registry}?mode=ro", uri=True) as db:
                for table, column in (("media_runs", "post_id"), ("media_decisions", "post_id"), ("media_usage", "post_id"), ("media_operations", "post_id")):
                    exists = db.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone()
                    if exists:
                        registry_rows[table] = int(db.execute(f"select count(*) from {table} where {column}=?", (pid,)).fetchone()[0])
        except sqlite3.Error:
            return {"registry_read": "error", "artifacts": artifacts, "journal": journal_present, "uncertain": uncertain_present, "classification": "REVIEW_REQUIRED"}
    journal_details: list[dict[str, Any]] = []
    for relative in journal_present:
        try:
            value = _read_json(root / relative, {}) or {}
            journal_details.append({key: value.get(key) for key in ("status", "writer_started", "writer_status", "readback", "media_application_status", "media_readback_status")})
        except (OSError, ValueError, TypeError):
            journal_details.append({"read_error": relative})
    journal_uncertain = any(
        detail.get("read_error")
        or detail.get("status") not in {"committed", "readback_verified"}
        or detail.get("readback") is not True
        or detail.get("writer_status") in {"possibly_started", "submitted", "uncertain"}
        or detail.get("media_application_status") in {"started", "submitted", "uncertain"}
        for detail in journal_details
    )
    meta = post.get("meta") or {}
    markers = {key: meta.get(key) for key in (*HUMAN_REOPEN_KEYS, "_hermes_ready_manifest", "_hermes_ready_hash") if meta.get(key)}
    external = bool(uncertain_present or journal_uncertain or registry_rows.get("media_operations", 0) or registry_rows.get("media_usage", 0) or markers)
    return {
        "artifacts": artifacts,
        "journal": journal_present,
        "journal_details": journal_details,
        "uncertain": uncertain_present,
        "registry": registry_rows,
        "markers": markers,
        "external_effects_evidence": external,
    }


def _human_required_classification(root: Path, post: dict[str, Any], quarantine: dict[int, dict[str, Any]], protected: set[int]) -> tuple[str, str, dict[str, Any]]:
    pid = int(post["id"])
    evidence = _human_required_evidence(root, post)
    if pid in protected:
        return "PROTECTED_TRANSACTION", "epoch_or_uncertain_transaction", evidence
    if pid in quarantine:
        return "PROTECTED_QUARANTINE", "active_quarantine", evidence
    if post.get("status") == "publish":
        return "PRESERVE_PUBLISHED", "wordpress_publish", evidence
    if post.get("status") != "pending":
        return "REVIEW_REQUIRED", "status_not_pending", evidence
    if evidence.get("registry_read") == "error":
        return "REVIEW_REQUIRED", "registry_read_failed", evidence
    if evidence.get("external_effects_evidence"):
        return "REVIEW_REQUIRED", "external_effect_evidence_present", evidence
    return "SAFE_RESET", "pending_human_required_without_external_effect_evidence", evidence


def _human_required_audit(root: Path, posts: list[dict[str, Any]], quarantine: dict[int, dict[str, Any]], protected: set[int]) -> list[dict[str, Any]]:
    audit: list[dict[str, Any]] = []
    for post in posts:
        state, error = _state_from_post(post)
        if error or not state or state.state is not LifecycleState.HUMAN_REQUIRED:
            continue
        classification, reason, evidence = _human_required_classification(root, post, quarantine, protected)
        audit.append({
            "post_id": int(post["id"]),
            "wordpress_status": post.get("status"),
            "canonical_state": state.state.value,
            "phase": state.phase.value,
            "blocker": state.blocker.value if state.blocker else None,
            "classification": classification,
            "reason": reason,
            "evidence": evidence,
            "content_sha256": _post_fingerprint(post)["content_sha256"],
        })
    return sorted(audit, key=lambda row: row["post_id"])


def reset_epoch(client: Any, config: Any, root: Path, *, preview: bool, apply: bool = False, preview_evidence: Path | None = None, operator_authorization: str | None = None, report_dir: Path | None = None) -> dict[str, Any]:
    if preview == apply:
        raise ResetEpochError("choose exactly one of --preview or --apply")
    root = Path(root).resolve()
    report_dir = Path(report_dir or (root / "work" / "v2-epochs" / "reports" / uuid.uuid4().hex)).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    quarantine, quarantine_hash = _quarantine(root)
    posts = _all_posts(client)
    protected = _protected_ids(root)
    cycle = _cycle_snapshot(root)
    classifications = []
    for post in posts:
        row = _classify(root, post, quarantine, protected)
        row["snapshot"] = _post_fingerprint(post)
        classifications.append(row)
    human_required_audit = _human_required_audit(root, posts, quarantine, protected)
    audit_by_id = {int(row["post_id"]): row for row in human_required_audit}
    for row in classifications:
        audit = audit_by_id.get(int(row["post_id"]))
        if audit and audit["classification"] == "SAFE_RESET":
            row["classification"] = "reset"
            row["reason"] = "safe_human_required_reset"
            row["human_required_audit"] = audit
        elif audit:
            row["human_required_audit"] = audit
    _write_json(report_dir / "human-required-audit.json", human_required_audit)
    snapshot = {
        "schema_version": 1,
        "operation_id": uuid.uuid4().hex,
        "old_epoch_id": (_read_json(root / "work" / "v2-epochs" / "active" / "epoch.json", {}) or {}).get("epoch_id", "legacy-unversioned"),
        "new_epoch_id": uuid.uuid4().hex,
        "generated_at": _now(),
        "production_sha": os.popen(f"git -C {root} rev-parse HEAD").read().strip(),
        "quarantine_sha256": quarantine_hash,
        "cycle": cycle,
        "registry_sha256": _file_sha(root / "work" / "media_registry.sqlite"),
        "posts": classifications,
        "counts": {name: sum(1 for row in classifications if row["classification"] == name) for name in sorted({row["classification"] for row in classifications})},
    }
    snapshot["snapshot_hash"] = _sha(_json({key: snapshot[key] for key in ("production_sha", "quarantine_sha256", "cycle", "registry_sha256", "posts", "counts")}).encode())
    evidence = None
    resume_transition = None
    if not preview:
        evidence_path = Path(preview_evidence or "")
        if evidence_path.is_dir():
            evidence_path /= "preview.json"
        evidence = _read_json(evidence_path)
        if not isinstance(evidence, dict):
            raise ResetEpochError("preview evidence is missing or invalid")
        operation_id = str(evidence.get("operation_id") or "")
        if not operation_id:
            raise ResetEpochError("preview evidence has no operation identity")
        resume_transition = _read_json(_reset_transition_path(root, operation_id), {}) or {}
        if resume_transition:
            if resume_transition.get("operation_id") != operation_id:
                raise ResetEpochError("transition operation identity mismatch")
            if resume_transition.get("preview_hash") != evidence.get("snapshot_hash"):
                raise ResetEpochError("transition preview hash mismatch")
            if resume_transition.get("new_epoch_id") != evidence.get("new_epoch_id"):
                raise ResetEpochError("transition epoch identity mismatch")
            if resume_transition.get("authorization") and resume_transition.get("authorization") != str(operator_authorization or ""):
                raise ResetEpochError("authorization identity mismatch on resume")
        if evidence.get("snapshot_hash") != snapshot["snapshot_hash"]:
            if resume_transition.get("phase") not in {"APPLYING", "READBACK_VERIFIED"}:
                raise ResetEpochError("preview evidence is stale or does not match current production snapshot")
            snapshot["resume"] = True
            frozen = evidence.get("posts")
            if not isinstance(frozen, list) or not frozen:
                raise ResetEpochError("resume evidence has no frozen post scope")
            current_by_id = {int(item["id"]): item for item in posts}
            missing = [int(row["post_id"]) for row in frozen if int(row["post_id"]) not in current_by_id]
            if missing:
                raise ResetEpochError(f"frozen post scope no longer readable: {missing[:10]}")
            classifications = [dict(row) for row in frozen]
            posts = [current_by_id[int(row["post_id"])] for row in frozen]
        snapshot["operation_id"] = operation_id
        snapshot["old_epoch_id"] = str(evidence.get("old_epoch_id") or "legacy-unversioned")
        snapshot["new_epoch_id"] = str(evidence.get("new_epoch_id") or "")
        if not snapshot["new_epoch_id"]:
            raise ResetEpochError("preview evidence has no new epoch identity")
    _write_json(report_dir / "preview.json", snapshot)
    if preview:
        return {"status": "RESET_PREVIEW_ONLY", "preview": str(report_dir / "preview.json"), **snapshot}
    if not operator_authorization or not str(operator_authorization).strip():
        raise ResetEpochError("operator authorization is required")
    if evidence is None:
        raise ResetEpochError("preview evidence is required")
    if resume_transition and resume_transition.get("phase") == "ACTIVATED":
        result = resume_transition.get("result")
        if result and resume_transition.get("authorization") == str(operator_authorization):
            return result
    if _active_processes():
        raise ResetEpochError("active V2/Publisher process prevents reset")
    reset_lock = RunSessionLock(root / "work" / "v2-reset-epoch.lock", allow_maintenance=True)
    if not reset_lock.acquire():
        raise ResetEpochError("reset lock unavailable")
    maintenance_path = root / "work" / "v2-maintenance.lock"
    run_lock = RunSessionLock(root / "work" / "v2-run.lock", allow_maintenance=True)
    mutation_started = bool(
        resume_transition
        and resume_transition.get("phase") in {"APPLYING", "READBACK_VERIFIED"}
        and (resume_transition.get("reset_post_ids") or maintenance_path.exists())
    )
    completed = False
    try:
        _write_json(maintenance_path, {"operation_id": snapshot["operation_id"], "phase": "APPLYING", "created_at": _now()})
        if not run_lock.acquire():
            raise ResetEpochError("V2 run lock unavailable")
        transition_root = root / "work" / "v2-epochs" / "transitions"
        transition_root.mkdir(parents=True, exist_ok=True)
        archive = root / "work" / "v2-epochs" / "archives" / snapshot["old_epoch_id"] / snapshot["operation_id"]
        archive.mkdir(parents=True, exist_ok=True)
        transition_path = _reset_transition_path(root, snapshot["operation_id"])
        transition = resume_transition or {"schema_version": 1, "operation_id": snapshot["operation_id"], "phase": "BACKUP_VERIFIED", "preview_hash": evidence["snapshot_hash"], "authorization": str(operator_authorization), "new_epoch_id": snapshot["new_epoch_id"], "archive": str(archive), "updated_at": _now()}
        _write_json(transition_path, transition)
        if transition.get("phase") == "BACKUP_VERIFIED":
            for source_name in ("v2-cycle", "v2-journal"):
                source = root / "work" / source_name
                if source.exists():
                    _copy_tree_verified(source, archive / source_name)
            archive_manifest = _archive_active_artifacts(root, archive)
            _write_json(archive / "preview.json", evidence)
            _write_json(archive / "wordpress-scope.json", {"captured_at": _now(), "posts": posts})
            _write_json(archive / "artifact-manifest.json", archive_manifest)
            registry_backup = archive / "media_registry.sqlite.backup"
            source_conn = sqlite3.connect(str(root / "work" / "media_registry.sqlite")); dest_conn = sqlite3.connect(str(registry_backup)); source_conn.backup(dest_conn); dest_conn.close(); source_conn.close()
            transition.update({"phase": "PREPARED", "registry_backup": str(registry_backup), "artifact_manifest": str(archive / "artifact-manifest.json"), "updated_at": _now()})
            _write_json(transition_path, transition)
        transition["phase"] = "APPLYING"
        _write_json(transition_path, transition)
        initial = _initial_state().to_dict()
        changed = list(transition.get("reset_post_ids") or [])
        post_journal_dir = archive / "post-reset-journals"
        post_journal_dir.mkdir(parents=True, exist_ok=True)
        (archive / "post-readbacks").mkdir(parents=True, exist_ok=True)
        for row in classifications:
            if row["classification"] != "reset":
                continue
            pid = int(row["post_id"])
            post_journal = post_journal_dir / f"{pid}.json"
            prior = _read_json(post_journal, {}) or {}
            if prior.get("phase") == "READBACK_VERIFIED":
                current = client.get_post(pid)
                confirmed_after = prior.get("after") or {}
                if _post_fingerprint(current) != confirmed_after or current.get("status") != "pending" or _state_from_post(current)[0] != _initial_state():
                    raise ResetEpochError(f"post {pid} completed readback diverged during resume")
                if pid not in changed:
                    changed.append(pid)
                continue
            current = client.get_post(pid)
            current_fp = _post_fingerprint(current)
            if current_fp != row["snapshot"]:
                read_state = _state_from_post(current)[0]
                if current.get("status") == "pending" and read_state == _initial_state():
                    _post_reset_journal(post_journal, {"post_id": pid, "phase": "READBACK_VERIFIED", "before": row["snapshot"], "after": current_fp, "resumed": True})
                    if pid not in changed: changed.append(pid)
                    continue
                raise ResetEpochError(f"post {pid} changed after preview without verified reset state")
            before = {"post_id": pid, "phase": "PREPARED", "before": row["snapshot"], "updated_at": _now()}
            _post_reset_journal(post_journal, before)
            old_meta = current.get("meta") or {}
            payload_meta: dict[str, Any] = {"_hermes_work_state": json.dumps(initial, ensure_ascii=False, separators=(",", ":"))}
            for key in LEGACY_KEYS:
                if key in old_meta:
                    payload_meta[key] = None
            _post_reset_journal(post_journal, {**before, "phase": "APPLYING", "write": payload_meta})
            mutation_started = True
            if current.get("status") == "awaiting_human":
                client.move_to_status(pid, "pending")
            client.update_post(pid, {"meta": payload_meta})
            readback = client.get_post(pid)
            read_state = _state_from_post(readback)[0]
            if readback.get("status") != "pending" or read_state != _initial_state():
                _post_reset_journal(post_journal, {**before, "phase": "READBACK", "after": _post_fingerprint(readback), "error": "readback_mismatch"})
                raise ResetEpochError(f"post {pid} reset readback mismatch")
            after = _post_fingerprint(readback)
            _post_reset_journal(post_journal, {"post_id": pid, "phase": "READBACK_VERIFIED", "before": row["snapshot"], "after": after, "updated_at": _now()})
            _write_json(archive / "post-readbacks" / f"{pid}.json", {"post_id": pid, "before": row["snapshot"], "after": after})
            if pid not in changed: changed.append(pid)
        transition.update({"phase": "READBACK_VERIFIED", "reset_post_ids": sorted(changed), "updated_at": _now()}); _write_json(transition_path, transition)
        mutation_started = True
        for source, name in ((root / "work" / "v2-cycle", "active-v2-cycle"), (root / "work" / "v2-journal", "active-v2-journal"), (root / "backups", "active-root-backups"), (root / "work" / "backups", "active-work-backups")):
            if source.exists():
                target = archive / name
                if target.exists(): shutil.rmtree(target)
                shutil.move(str(source), str(target))
        (root / "work" / "v2-cycle").mkdir(parents=True, exist_ok=True); (root / "work" / "v2-journal").mkdir(parents=True, exist_ok=True); (root / "backups").mkdir(parents=True, exist_ok=True); (root / "work" / "backups").mkdir(parents=True, exist_ok=True)
        projection = root / "work" / "media_registry.epoch-projection.sqlite"
        copied = _registry_projection(root / "work" / "media_registry.sqlite", projection, snapshot["new_epoch_id"])
        os.replace(projection, root / "work" / "media_registry.sqlite")
        store = CycleStateStore(root); new_cycle = store.create({}, cycle_id=uuid.uuid4().hex)
        invalidated_reopen_ids = sorted(
            int(row["post_id"])
            for row in classifications
            if row["classification"] == "reset"
            and any(row.get("snapshot", {}).get("meta", {}).get(key) for key in HUMAN_REOPEN_KEYS)
        )
        _write_json(archive / "human-reopen-invalidations.json", {
            "schema_version": 1,
            "operation_id": snapshot["operation_id"],
            "post_ids": invalidated_reopen_ids,
            "reason": "prior-generation-human-reopen-markers-archived-not-authorizations",
        })
        epoch = {"schema_version": 1, "epoch_id": snapshot["new_epoch_id"], "old_epoch_id": snapshot["old_epoch_id"], "operation_id": snapshot["operation_id"], "cycle_id": new_cycle.cycle_id, "protected_transaction_ids": sorted(protected), "invalidated_human_reopen_ids": invalidated_reopen_ids, "reset_post_ids": sorted(changed), "preserved_post_ids": [int(row["post_id"]) for row in classifications if row["classification"] == "preserve_published"], "activated_at": _now()}
        (root / "work" / "v2-epochs" / "active").mkdir(parents=True, exist_ok=True)
        _write_json(root / "work" / "v2-epochs" / "active" / "epoch.json", epoch)
        result = {"status": "EDITORIAL_RESET_COMPLETE", "operation_id": snapshot["operation_id"], "epoch_id": snapshot["new_epoch_id"], "cycle_id": new_cycle.cycle_id, "reset_post_ids": sorted(changed), "protected_ids": sorted(protected), "counts": snapshot["counts"], "registry_projection": copied, "wordpress_writes": len(changed), "archive": str(archive)}
        transition.update({"phase": "ACTIVATED", "cycle_id": new_cycle.cycle_id, "registry_projection": copied, "readback_count": len(changed), "result": result, "updated_at": _now()}); _write_json(transition_path, transition)
        completed = True
        return result
    finally:
        run_lock.release()
        if completed or not mutation_started:
            try:
                maintenance_path.unlink()
            except FileNotFoundError:
                pass
        reset_lock.release()
