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
    relevant_meta = {key: meta.get(key) for key in sorted(set(LEGACY_KEYS + ("_hermes_work_state",)))}
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


def _registry_projection(source: Path, target: Path, epoch_id: str) -> dict[str, int]:
    source_conn = sqlite3.connect(str(source))
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    target_conn = sqlite3.connect(str(target))
    target_conn.execute("PRAGMA foreign_keys=ON")
    tables = [row[0] for row in source_conn.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name")]
    copied: dict[str, int] = {}
    for table in tables:
        ddl = source_conn.execute("select sql from sqlite_master where type='table' and name=?", (table,)).fetchone()[0]
        target_conn.execute(ddl)
    for table in ("media_assets", "media_decisions", "schema_meta"):
        columns = [row[1] for row in source_conn.execute(f"pragma table_info({table})")]
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
    if not preview:
        evidence_path = Path(preview_evidence or "")
        if evidence_path.is_dir():
            evidence_path /= "preview.json"
        evidence = _read_json(evidence_path)
        if not isinstance(evidence, dict) or evidence.get("snapshot_hash") != snapshot["snapshot_hash"]:
            raise ResetEpochError("preview evidence is stale or does not match current production snapshot")
        snapshot["operation_id"] = str(evidence.get("operation_id") or "")
        snapshot["old_epoch_id"] = str(evidence.get("old_epoch_id") or "legacy-unversioned")
        snapshot["new_epoch_id"] = str(evidence.get("new_epoch_id") or "")
        if not snapshot["operation_id"] or not snapshot["new_epoch_id"]:
            raise ResetEpochError("preview evidence has no operation or new epoch identity")
    _write_json(report_dir / "preview.json", snapshot)
    if preview:
        return {"status": "RESET_PREVIEW_ONLY", "preview": str(report_dir / "preview.json"), **snapshot}
    if not operator_authorization or not str(operator_authorization).strip():
        raise ResetEpochError("operator authorization is required")
    if evidence is None:
        raise ResetEpochError("preview evidence is required")
    if _active_processes():
        raise ResetEpochError("active V2/Publisher process prevents reset")
    reset_lock = RunSessionLock(root / "work" / "v2-reset-epoch.lock")
    run_lock = RunSessionLock(root / "work" / "v2-run.lock")
    if not reset_lock.acquire() or not run_lock.acquire():
        reset_lock.release(); run_lock.release()
        raise ResetEpochError("reset or V2 run lock unavailable")
    try:
        transition_root = root / "work" / "v2-epochs" / "transitions"
        archive = root / "work" / "v2-epochs" / "archives" / snapshot["old_epoch_id"] / snapshot["operation_id"]
        archive.mkdir(parents=True, exist_ok=True)
        transition = {"schema_version": 1, "operation_id": snapshot["operation_id"], "phase": "BACKUP_VERIFIED", "preview_hash": snapshot["snapshot_hash"], "authorization": str(operator_authorization), "new_epoch_id": snapshot["new_epoch_id"], "archive": str(archive), "updated_at": _now()}
        _write_json(transition_root / f"{snapshot['operation_id']}.json", transition)
        for source_name in ("v2-cycle", "v2-journal", "backups"):
            source = root / "work" / source_name
            if source.exists():
                shutil.copytree(source, archive / source_name, dirs_exist_ok=True)
        (archive / "preview.json").write_text(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        registry_backup = archive / "media_registry.sqlite.backup"
        source_conn = sqlite3.connect(str(root / "work" / "media_registry.sqlite")); dest_conn = sqlite3.connect(str(registry_backup)); source_conn.backup(dest_conn); dest_conn.close(); source_conn.close()
        transition["phase"] = "PREPARED"; transition["registry_backup"] = str(registry_backup); _write_json(transition_root / f"{snapshot['operation_id']}.json", transition)
        initial = _initial_state().to_dict()
        changed: list[int] = []
        for row in classifications:
            if row["classification"] != "reset":
                continue
            pid = int(row["post_id"]); current = client.get_post(pid)
            if _post_fingerprint(current) != row["snapshot"]:
                raise ResetEpochError(f"post {pid} changed after preview")
            old_meta = current.get("meta") or {}
            payload_meta: dict[str, Any] = {"_hermes_work_state": json.dumps(initial, ensure_ascii=False, separators=(",", ":"))}
            for key in LEGACY_KEYS:
                if key in old_meta:
                    payload_meta[key] = None
            if current.get("status") == "awaiting_human":
                client.move_to_status(pid, "pending")
            client.update_post(pid, {"meta": payload_meta})
            readback = client.get_post(pid)
            read_state = _state_from_post(readback)[0]
            if readback.get("status") != "pending" or read_state != _initial_state():
                raise ResetEpochError(f"post {pid} reset readback mismatch")
            _write_json(archive / "post-readbacks" / f"{pid}.json", {"post_id": pid, "before": row["snapshot"], "after": _post_fingerprint(readback)})
            changed.append(pid)
        transition["phase"] = "APPLYING"; transition["reset_post_ids"] = changed; _write_json(transition_root / f"{snapshot['operation_id']}.json", transition)
        old_cycle = root / "work" / "v2-cycle"; old_journal = root / "work" / "v2-journal"; old_backups = root / "work" / "backups"
        for source, name in ((old_cycle, "v2-cycle"), (old_journal, "v2-journal"), (old_backups, "backups")):
            if source.exists():
                shutil.move(str(source), str(archive / f"active-{name}"))
        (root / "work" / "v2-cycle").mkdir(parents=True, exist_ok=True)
        (root / "work" / "v2-journal").mkdir(parents=True, exist_ok=True)
        (root / "work" / "backups").mkdir(parents=True, exist_ok=True)
        projection = root / "work" / "media_registry.epoch-projection.sqlite"
        copied = _registry_projection(root / "work" / "media_registry.sqlite", projection, snapshot["new_epoch_id"])
        os.replace(projection, root / "work" / "media_registry.sqlite")
        store = CycleStateStore(root)
        config_values = (cycle.get("config") or {})
        new_cycle = store.create(config_values, cycle_id=uuid.uuid4().hex)
        epoch = {"schema_version": 1, "epoch_id": snapshot["new_epoch_id"], "old_epoch_id": snapshot["old_epoch_id"], "operation_id": snapshot["operation_id"], "cycle_id": new_cycle.cycle_id, "protected_transaction_ids": sorted(protected), "reset_post_ids": changed, "preserved_post_ids": [int(row["post_id"]) for row in classifications if row["classification"] == "preserve_published"], "activated_at": _now()}
        _write_json(root / "work" / "v2-epochs" / "active" / "epoch.json", epoch)
        transition.update({"phase": "ACTIVATED", "cycle_id": new_cycle.cycle_id, "registry_projection": copied, "readback_count": len(changed), "updated_at": _now()}); _write_json(transition_root / f"{snapshot['operation_id']}.json", transition)
        return {"status": "EDITORIAL_RESET_COMPLETE", "operation_id": snapshot["operation_id"], "epoch_id": snapshot["new_epoch_id"], "cycle_id": new_cycle.cycle_id, "reset_post_ids": changed, "protected_ids": sorted(protected), "counts": snapshot["counts"], "registry_projection": copied, "wordpress_writes": len(changed), "archive": str(archive)}
    finally:
        run_lock.release(); reset_lock.release()
