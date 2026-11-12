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
from ..pipeline_v2.epoch_protection import load_epoch_protection
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


def _tree_fingerprint(path: Path) -> str:
    path = Path(path)
    if not path.exists():
        return "missing"
    if path.is_file():
        return _file_sha(path)
    files = {
        str(child.relative_to(path)): _file_sha(child)
        for child in sorted(path.rglob("*"))
        if child.is_file()
    }
    return _sha(_json(files).encode("utf-8"))


def _registry_snapshot(path: Path) -> tuple[str, set[int]]:
    path = Path(path)
    if not path.is_file():
        raise ResetEpochError("Media Registry is missing; external effects cannot be classified")
    source = target = None
    try:
        source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        source.execute("PRAGMA query_only=ON")
        target = sqlite3.connect(":memory:")
        source.backup(target)
        check = target.execute("PRAGMA quick_check").fetchone()
        if not check or check[0] != "ok":
            raise ResetEpochError("Media Registry quick_check failed")
        exists = target.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_operations'"
        ).fetchone()
        if not exists:
            raise ResetEpochError("Media Registry lacks media_operations; external effects are unclassified")
        columns = {row[1] for row in target.execute("PRAGMA table_info(media_operations)")}
        required = {"post_id", "operation_status", "completed_at", "readback_confirmed"}
        if not required <= columns:
            raise ResetEpochError("Media Registry operation schema is incomplete")
        rows = target.execute(
            """SELECT DISTINCT post_id FROM media_operations
            WHERE lower(trim(coalesce(operation_status, ''))) != 'completed'
               OR completed_at IS NULL OR trim(completed_at) = ''
               OR coalesce(readback_confirmed, 0) != 1"""
        ).fetchall()
        protected_ids: set[int] = set()

        def add_post_ids(rows, table: str) -> None:
            for row in rows:
                try:
                    post_id = int(row[0])
                except (TypeError, ValueError) as exc:
                    raise ResetEpochError(f"Media Registry contains an invalid post ID in {table}") from exc
                if post_id <= 0:
                    raise ResetEpochError(f"Media Registry contains an invalid post ID in {table}")
                protected_ids.add(post_id)

        add_post_ids(rows, "media_operations")
        for table, required_columns in (
            ("media_runs", {"post_id", "acquisition_outcome"}),
            ("media_usage", {"post_id", "status", "applied_at", "readback_confirmed_at"}),
        ):
            exists = target.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                raise ResetEpochError(f"Media Registry lacks {table}; external effects are unclassified")
            columns = {row[1] for row in target.execute(f"PRAGMA table_info({table})")}
            if not required_columns <= columns:
                raise ResetEpochError(f"Media Registry {table} schema is incomplete")
        started_or_unknown_runs = target.execute(
            """SELECT DISTINCT post_id FROM media_runs
            WHERE lower(trim(coalesce(acquisition_outcome, '')))
                  NOT IN ('retry', 'human_required', 'ready')"""
        ).fetchall()
        add_post_ids(started_or_unknown_runs, "media_runs")
        unresolved_usage = target.execute(
            """SELECT DISTINCT post_id FROM media_usage
            WHERE lower(trim(coalesce(status, ''))) NOT IN ('accepted', 'applied', 'rejected')
               OR lower(trim(coalesce(status, ''))) = 'accepted'
               OR (lower(trim(coalesce(status, ''))) = 'applied'
                   AND (applied_at IS NULL OR trim(applied_at) = ''
                        OR readback_confirmed_at IS NULL OR trim(readback_confirmed_at) = ''))"""
        ).fetchall()
        add_post_ids(unresolved_usage, "media_usage")
        foreign_key_errors = target.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise ResetEpochError("Media Registry foreign_key_check failed")
        schema = target.execute(
            """SELECT type, name, tbl_name, coalesce(sql, '') FROM sqlite_master
            WHERE type IN ('table', 'index', 'view', 'trigger') ORDER BY type, name"""
        ).fetchall()
        tables = [row[0] for row in target.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()]
        table_rows: dict[str, list[str]] = {}
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            rows = target.execute(f"SELECT * FROM {quoted}").fetchall()
            encoded_rows = []
            for row in rows:
                values = [({"__blob__": value.hex()} if isinstance(value, bytes) else value) for value in row]
                encoded_rows.append(_json(values))
            table_rows[table] = sorted(encoded_rows)
        logical_snapshot = {
            "schema": schema,
            "user_version": target.execute("PRAGMA user_version").fetchone()[0],
            "application_id": target.execute("PRAGMA application_id").fetchone()[0],
            "tables": table_rows,
        }
        return _sha(_json(logical_snapshot).encode("utf-8")), protected_ids
    except ResetEpochError:
        raise
    except sqlite3.Error as exc:
        raise ResetEpochError(f"Media Registry read failed: {type(exc).__name__}") from exc
    finally:
        if source is not None:
            source.close()
        if target is not None:
            target.close()


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
    rows: dict[int, dict[str, Any]] = {}
    for index, row in enumerate(payload["posts"]):
        if not isinstance(row, dict):
            raise ResetEpochError(f"invalid quarantine post entry at index {index}")
        raw_post_id = row.get("post_id")
        if isinstance(raw_post_id, bool):
            raise ResetEpochError(f"invalid quarantine post ID at index {index}")
        if isinstance(raw_post_id, int):
            post_id = raw_post_id
        elif isinstance(raw_post_id, str) and raw_post_id.isdigit():
            post_id = int(raw_post_id)
        else:
            raise ResetEpochError(f"invalid quarantine post ID at index {index}")
        if post_id <= 0 or post_id in rows:
            raise ResetEpochError(f"invalid or duplicate quarantine post ID at index {index}")
        rows[post_id] = row
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


def _registry_incomplete_ids(root: Path) -> set[int]:
    path = Path(root) / "work" / "media_registry.sqlite"
    if not path.is_file():
        return set()
    return _registry_snapshot(path)[1]


def _collect_post_marker_ids(directory: Path, suffix: str, description: str, result: set[int]) -> None:
    directory = Path(directory)
    if directory.is_symlink():
        raise ResetEpochError(f"{description} marker directory is a symlink")
    if not directory.exists():
        return
    if not directory.is_dir():
        raise ResetEpochError(f"{description} marker path is not a directory")
    seen: set[int] = set()
    for path in sorted(directory.iterdir()):
        if path.is_symlink() or not path.is_file():
            raise ResetEpochError(f"uncategorized {description} marker: {path.name}")
        if path.suffix != suffix or not path.stem.isdigit() or int(path.stem) <= 0:
            raise ResetEpochError(f"uncategorized {description} marker: {path.name}")
        post_id = int(path.stem)
        if post_id in seen:
            raise ResetEpochError(f"duplicate {description} marker for post {post_id}")
        seen.add(post_id)
        result.add(post_id)


def _protected_ids(root: Path, *, registry_ids: set[int] | None = None) -> set[int]:
    root = Path(root)
    result = {PROTECTED_POST_ID}
    cycle = _cycle_snapshot(root)
    for post_id in cycle.get("unresolved_ids") or []:
        if type(post_id) is not int or post_id <= 0:
            raise ResetEpochError("cycle contains an invalid unresolved post ID")
        result.add(post_id)
    selected = cycle.get("selected_ids") or []
    if cycle.get("authorization_id") or selected:
        if (
            not isinstance(selected, list)
            or not selected
            or any(type(post_id) is not int or post_id <= 0 for post_id in selected)
            or len(selected) != len(set(selected))
        ):
            raise ResetEpochError("pending round authorization has invalid selected IDs")
        result.update(selected)
    for relative in (
        "work/v2-journal",
        "work/v2-epochs/active",
        "work/v2-publication-uncertain",
        "work/v2-uncertain",
    ):
        _collect_post_marker_ids(root / relative, ".json", relative, result)
    _collect_post_marker_ids(root / "work" / "locks", ".lock", "post lock", result)
    if registry_ids is None:
        registry_ids = _registry_incomplete_ids(root)
    result.update(registry_ids)
    return result


def _wordpress_state_diverges(post: dict[str, Any], state: WorkState | None) -> bool:
    raw_meta = post.get("meta")
    meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    legacy = str(meta.get("_hermes_state") or "").strip()
    if state is None:
        return bool(legacy)
    if not legacy:
        return True
    expected = {
        LifecycleState.PENDING: {"new", "pending"},
        LifecycleState.READY: {"ready"},
        LifecycleState.HUMAN_REQUIRED: {"awaiting_human"},
        LifecycleState.SKIPPED: {"skipped"},
        LifecycleState.PUBLISHED: {"published"},
    }
    return legacy not in expected.get(state.state, set())


def _classify(root: Path, post: dict[str, Any], quarantine: dict[int, dict[str, Any]], protected: set[int]) -> dict[str, Any]:
    pid = int(post["id"])
    status = str(post.get("status") or "")
    raw_meta = post.get("meta")
    meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    legacy_state = str(meta.get("_hermes_state") or "").strip()
    state, state_error = _state_from_post(post)
    if status == "publish" or legacy_state == "published" or (state and state.state is LifecycleState.PUBLISHED):
        return {"post_id": pid, "classification": "preserve_published", "reason": "published"}
    if pid in quarantine:
        return {"post_id": pid, "classification": "protected_quarantine", "reason": quarantine[pid].get("reason_code", "quarantine")}
    if pid in protected:
        return {"post_id": pid, "classification": "protected_transaction", "reason": "uncertain_external_effect"}
    if state_error:
        return {"post_id": pid, "classification": "exception", "reason": state_error}
    if _wordpress_state_diverges(post, state):
        return {"post_id": pid, "classification": "protected_transaction", "reason": "wordpress_state_divergence"}
    if status not in {"pending", "awaiting_human"}:
        return {"post_id": pid, "classification": "exclude_status", "reason": f"wordpress_status:{status}"}
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
    proc_root = Path("/proc")
    ignored_pids: set[int] = set()
    pid = os.getpid()
    while pid > 1 and pid not in ignored_pids:
        ignored_pids.add(pid)
        try:
            stat_fields = (proc_root / str(pid) / "stat").read_text().split()
            pid = int(stat_fields[3])
        except (OSError, IndexError, ValueError):
            break
    commands = {
        "v2-cycle", "v2-run", "v2-reset-epoch", "v2-release-isolated",
        "v2-reconcile", "v2-audit-pending-round", "v2-investigation-queue",
        "publish-ready",
    }
    script_names = {
        "run-v2-cycle.sh", "start-v2-cycle.sh", "unicorniohater-publish.sh",
        "publish-ready.sh", "v2-cycle.sh",
    }
    found = []
    for entry in proc_root.glob("[0-9]*"):
        try:
            process_id = int(entry.name)
        except ValueError:
            continue
        if process_id in ignored_pids:
            continue
        try:
            raw_args = (entry / "cmdline").read_bytes().split(b"\x00")
            args = [value.decode(errors="replace") for value in raw_args if value]
        except OSError:
            continue
        if not args:
            continue
        executable = Path(args[0]).name
        cli_command = (
            executable == "unicornio-editor"
            and any(argument in commands for argument in args[1:])
        ) or (
            "unicornio_editor.cli" in args
            and any(argument in commands for argument in args[args.index("unicornio_editor.cli") + 1:])
        ) or "unicornio_editor.pipeline_v2.writer" in args
        script_command = any(Path(argument).name in script_names for argument in args)
        if cli_command or script_command:
            found.append(f"{process_id}: {' '.join(args)}")
    return found


def _copy_tree_verified(source: Path, target: Path) -> dict[str, str]:
    source = Path(source)
    target = Path(target)
    if source.is_symlink():
        raise ResetEpochError(f"archive source is a symlink: {source}")
    if not source.exists():
        if target.exists() or target.is_symlink():
            raise ResetEpochError(f"archive target exists without source evidence: {target}")
        return {}
    source_hashes = _tree_hashes(source)
    if target.exists() or target.is_symlink():
        if target.is_symlink() or _tree_hashes(target) != source_hashes:
            raise ResetEpochError(f"existing archive differs from source evidence: {target}")
        return source_hashes
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    if _tree_hashes(target) != source_hashes:
        raise ResetEpochError(f"archive copy verification failed: {source}")
    return source_hashes


def _tree_hashes(path: Path) -> dict[str, str]:
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise ResetEpochError(f"archive path is not a regular directory: {path}")
    return {
        str(child.relative_to(path)): _file_sha(child)
        for child in sorted(path.rglob("*"))
        if child.is_file()
    }


def _archive_tree_preserving(
    source: Path,
    target: Path,
    *,
    expected_sha256: str | None = None,
    expected_files: dict[str, str] | None = None,
) -> None:
    if expected_sha256 is None and expected_files is None:
        if target.exists() or target.is_symlink():
            raise ResetEpochError(f"archive target exists without preview evidence: {target}")
        if source.is_symlink():
            raise ResetEpochError(f"active source is a symlink without preview evidence: {source}")
        if source.exists() and any(child.is_file() for child in source.rglob("*")):
            raise ResetEpochError(f"active source contains unclassified files: {source}")
        return

    def verified(path: Path) -> bool:
        if path.is_symlink() or not path.is_dir():
            return False
        if expected_sha256 is not None and _tree_fingerprint(path) != expected_sha256:
            return False
        if expected_files is not None and _tree_hashes(path) != expected_files:
            return False
        return True

    if target.exists() or target.is_symlink():
        if not verified(target):
            raise ResetEpochError(f"existing archive differs from verified evidence: {target}")
        if source.exists() and any(child.is_file() for child in source.rglob("*")):
            raise ResetEpochError(f"archive and active source both contain data: {source}")
        return
    if not source.exists():
        return
    if not verified(source):
        raise ResetEpochError(f"active source differs from preview evidence: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    if not verified(target):
        raise ResetEpochError(f"archive move verification failed: {target}")


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


def _human_required_evidence(root: Path, post: dict[str, Any], client: Any | None = None) -> dict[str, Any]:
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
    usage_rows: list[dict[str, Any]] = []
    operation_rows: list[dict[str, Any]] = []
    registry = root / "work" / "media_registry.sqlite"
    if registry.is_file():
        try:
            with sqlite3.connect(f"file:{registry}?mode=ro", uri=True) as db:
                for table, column in (("media_runs", "post_id"), ("media_decisions", "post_id"), ("media_usage", "post_id"), ("media_operations", "post_id")):
                    exists = db.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone()
                    if exists:
                        registry_rows[table] = int(db.execute(f"select count(*) from {table} where {column}=?", (pid,)).fetchone()[0])
                        if table == "media_usage":
                            cols = [row[1] for row in db.execute("pragma table_info(media_usage)")]
                            usage_rows = [dict(zip(cols, row)) for row in db.execute("select * from media_usage where post_id=?", (pid,)).fetchall()]
                        elif table == "media_operations":
                            cols = [row[1] for row in db.execute("pragma table_info(media_operations)")]
                            operation_rows = [dict(zip(cols, row)) for row in db.execute("select * from media_operations where post_id=?", (pid,)).fetchall()]
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
    media_reconciled = False
    if usage_rows and not operation_rows and all(
        row.get("status") == "accepted" and not row.get("applied_at") and not row.get("readback_confirmed_at")
        for row in usage_rows
    ):
        media_reconciled = True
        if client is not None:
            try:
                media_reconciled = all(client.get_media(int(row["wordpress_media_id"])) for row in usage_rows if row.get("wordpress_media_id"))
            except Exception:
                media_reconciled = False
    external = bool(uncertain_present or journal_uncertain or operation_rows or (registry_rows.get("media_usage", 0) and not media_reconciled))
    return {
        "artifacts": artifacts,
        "journal": journal_present,
        "journal_details": journal_details,
        "uncertain": uncertain_present,
        "registry": registry_rows,
        "media_reconciled": media_reconciled,
        "markers": markers,
        "external_effects_evidence": external,
    }


def _human_required_classification(root: Path, post: dict[str, Any], quarantine: dict[int, dict[str, Any]], protected: set[int], client: Any | None = None) -> tuple[str, str, dict[str, Any]]:
    pid = int(post["id"])
    evidence = _human_required_evidence(root, post, client)
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
    if evidence.get("media_reconciled"):
        return "SAFE_RESET_RECONCILED", "known_media_effect_reconciled_and_not_applied", evidence
    return "SAFE_RESET", "pending_human_required_without_external_effect_evidence", evidence


def _human_required_audit(root: Path, posts: list[dict[str, Any]], quarantine: dict[int, dict[str, Any]], protected: set[int], client: Any | None = None) -> list[dict[str, Any]]:
    audit: list[dict[str, Any]] = []
    for post in posts:
        state, error = _state_from_post(post)
        if error or not state or state.state is not LifecycleState.HUMAN_REQUIRED:
            continue
        classification, reason, evidence = _human_required_classification(root, post, quarantine, protected, client)
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


def _capture_reset_snapshot(
    client: Any,
    root: Path,
    active_protection: Any,
    *,
    operation_id: str | None = None,
    old_epoch_id: str | None = None,
    new_epoch_id: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], set[int]]:
    registry_sha256, registry_incomplete_ids = _registry_snapshot(
        root / "work" / "media_registry.sqlite"
    )
    quarantine, quarantine_hash = _quarantine(root)
    posts = _all_posts(client)
    protected = _protected_ids(root, registry_ids=registry_incomplete_ids) | set(
        active_protection.protected_ids
    )
    cycle = _cycle_snapshot(root)
    classifications = []
    for post in posts:
        row = _classify(root, post, quarantine, protected)
        row["snapshot"] = _post_fingerprint(post)
        classifications.append(row)
    human_required_audit = _human_required_audit(
        root, posts, quarantine, protected, client
    )
    audit_by_id = {int(row["post_id"]): row for row in human_required_audit}
    for row in classifications:
        audit = audit_by_id.get(int(row["post_id"]))
        if (
            audit
            and audit["classification"] in {"SAFE_RESET", "SAFE_RESET_RECONCILED"}
            and row["classification"] == "reset"
        ):
            row["reason"] = "safe_human_required_reset"
            row["human_required_audit"] = audit
        elif audit:
            row["human_required_audit"] = audit
    current_epoch = (
        _read_json(root / "work" / "v2-epochs" / "active" / "epoch.json", {}) or {}
    ).get("epoch_id", "legacy-unversioned")
    snapshot = {
        "schema_version": 1,
        "operation_id": operation_id or uuid.uuid4().hex,
        "old_epoch_id": old_epoch_id if old_epoch_id is not None else current_epoch,
        "new_epoch_id": new_epoch_id or uuid.uuid4().hex,
        "generated_at": _now(),
        "production_sha": os.popen(f"git -C {root} rev-parse HEAD").read().strip(),
        "active_epoch_id": active_protection.epoch_id,
        "active_epoch_policy_fingerprint": active_protection.policy_fingerprint,
        "quarantine_sha256": quarantine_hash,
        "cycle": cycle,
        "cycle_files_sha256": _tree_fingerprint(root / "work" / "v2-cycle"),
        "journal_sha256": _tree_fingerprint(root / "work" / "v2-journal"),
        "lock_snapshot_sha256": _tree_fingerprint(root / "work" / "locks"),
        "protected_ids": sorted(protected),
        "registry_sha256": registry_sha256,
        "posts": classifications,
        "counts": {
            name: sum(1 for row in classifications if row["classification"] == name)
            for name in sorted({row["classification"] for row in classifications})
        },
    }
    hash_fields = (
        "production_sha", "active_epoch_id", "active_epoch_policy_fingerprint",
        "quarantine_sha256", "cycle", "cycle_files_sha256", "journal_sha256",
        "lock_snapshot_sha256", "protected_ids", "registry_sha256", "posts", "counts",
    )
    snapshot["snapshot_hash"] = _sha(
        _json({key: snapshot[key] for key in hash_fields}).encode()
    )
    return snapshot, posts, human_required_audit, protected


def reset_epoch(client: Any, config: Any, root: Path, *, preview: bool, apply: bool = False, preview_evidence: Path | None = None, operator_authorization: str | None = None, report_dir: Path | None = None) -> dict[str, Any]:
    if preview == apply:
        raise ResetEpochError("choose exactly one of --preview or --apply")
    root = Path(root).resolve()
    active_protection = load_epoch_protection(root)
    report_dir = Path(report_dir or (root / "work" / "v2-epochs" / "reports" / uuid.uuid4().hex)).resolve()
    snapshot, posts, human_required_audit, protected = _capture_reset_snapshot(
        client, root, active_protection
    )
    classifications = snapshot["posts"]
    report_dir.mkdir(parents=True, exist_ok=True)
    _write_json(report_dir / "human-required-audit.json", human_required_audit)

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
        if _active_processes():
            raise ResetEpochError("active V2/Publisher process prevents reset")
        if (
            resume_transition
            and resume_transition.get("phase") in {"APPLYING", "READBACK_VERIFIED"}
        ):
            expected_archive = (
                root / "work" / "v2-epochs" / "archives"
                / snapshot["old_epoch_id"] / snapshot["operation_id"]
            ).resolve()
            if Path(str(resume_transition.get("archive") or "")).resolve() != expected_archive:
                raise ResetEpochError("resume archive identity mismatch")
            if snapshot.get("production_sha") != evidence.get("production_sha"):
                raise ResetEpochError("production code changed during partial reset")
            _, current_quarantine_sha = _quarantine(root)
            if current_quarantine_sha != evidence.get("quarantine_sha256"):
                raise ResetEpochError("quarantine changed during partial reset")
            if _tree_fingerprint(root / "work" / "locks") != evidence.get("lock_snapshot_sha256"):
                raise ResetEpochError("lock snapshot changed during partial reset")
            current_protection = load_epoch_protection(root)
            if (
                current_protection.epoch_id != evidence.get("active_epoch_id")
                or current_protection.policy_fingerprint != evidence.get("active_epoch_policy_fingerprint")
            ):
                raise ResetEpochError("active epoch protection changed during partial reset")
            live_registry_hash, _ = _registry_snapshot(root / "work" / "media_registry.sqlite")
            if live_registry_hash != evidence.get("registry_sha256"):
                raise ResetEpochError("Media Registry changed during partial reset; resume blocked")
            registry_backup = expected_archive / "media_registry.sqlite.backup"
            backup_registry_hash, _ = _registry_snapshot(registry_backup)
            if backup_registry_hash != evidence.get("registry_sha256"):
                raise ResetEpochError("archived Media Registry backup does not match preview evidence")
            expected_backup_sha = resume_transition.get("registry_backup_file_sha256")
            if expected_backup_sha and _file_sha(registry_backup) != expected_backup_sha:
                raise ResetEpochError("archived Media Registry backup file hash changed")
            manifest_path = expected_archive / "artifact-manifest.json"
            manifest = _read_json(manifest_path, {}) or {}
            if not isinstance(manifest.get("roots"), dict):
                raise ResetEpochError("archived artifact manifest is missing or invalid")
            recorded_manifest_path = resume_transition.get("artifact_manifest")
            if recorded_manifest_path and Path(recorded_manifest_path).resolve() != manifest_path.resolve():
                raise ResetEpochError("artifact manifest identity mismatch")
        if not (
            resume_transition
            and resume_transition.get("phase") in {"APPLYING", "READBACK_VERIFIED"}
        ):
            locked_protection = load_epoch_protection(root)
            locked_snapshot, locked_posts, locked_audit, locked_protected = _capture_reset_snapshot(
                client,
                root,
                locked_protection,
                operation_id=snapshot["operation_id"],
                old_epoch_id=snapshot["old_epoch_id"],
                new_epoch_id=snapshot["new_epoch_id"],
            )
            if locked_snapshot["snapshot_hash"] != evidence["snapshot_hash"]:
                _write_json(report_dir / "apply-preflight.json", {
                    "status": "blocked_snapshot_drift",
                    "expected_snapshot_hash": evidence["snapshot_hash"],
                    "observed_snapshot_hash": locked_snapshot["snapshot_hash"],
                })
                raise ResetEpochError("preview snapshot changed after maintenance locks were acquired")
            snapshot = locked_snapshot
            posts = locked_posts
            classifications = snapshot["posts"]
            human_required_audit = locked_audit
            protected = locked_protected
            active_protection = locked_protection
            _write_json(report_dir / "human-required-audit.json", human_required_audit)
            _write_json(report_dir / "preview.json", snapshot)
        transition_root = root / "work" / "v2-epochs" / "transitions"
        transition_root.mkdir(parents=True, exist_ok=True)
        archive = root / "work" / "v2-epochs" / "archives" / snapshot["old_epoch_id"] / snapshot["operation_id"]
        archive.mkdir(parents=True, exist_ok=True)
        transition_path = _reset_transition_path(root, snapshot["operation_id"])
        transition = resume_transition or {"schema_version": 1, "operation_id": snapshot["operation_id"], "phase": "BACKUP_PENDING", "preview_hash": evidence["snapshot_hash"], "authorization": str(operator_authorization), "new_epoch_id": snapshot["new_epoch_id"], "archive": str(archive), "updated_at": _now()}
        mutation_started = True
        _write_json(transition_path, transition)
        if transition.get("phase") in {"BACKUP_PENDING", "BACKUP_VERIFIED"}:
            transition.update({"phase": "BACKUP_PENDING", "updated_at": _now()})
            _write_json(transition_path, transition)
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
                    if (
                        current_fp.get("content_sha256") != row["snapshot"].get("content_sha256")
                        or current_fp.get("featured_media") != row["snapshot"].get("featured_media")
                    ):
                        raise ResetEpochError(f"post {pid} content or featured media changed during reset resume")
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
            after = _post_fingerprint(readback)
            if readback.get("status") != "pending" or read_state != _initial_state():
                _post_reset_journal(post_journal, {**before, "phase": "READBACK", "after": after, "error": "readback_mismatch"})
                raise ResetEpochError(f"post {pid} reset readback mismatch")
            if (
                after.get("content_sha256") != row["snapshot"].get("content_sha256")
                or after.get("featured_media") != row["snapshot"].get("featured_media")
            ):
                _post_reset_journal(post_journal, {**before, "phase": "READBACK", "after": after, "error": "content_or_featured_media_changed"})
                raise ResetEpochError(f"post {pid} content or featured media changed during reset")
            _post_reset_journal(post_journal, {"post_id": pid, "phase": "READBACK_VERIFIED", "before": row["snapshot"], "after": after, "updated_at": _now()})
            _write_json(archive / "post-readbacks" / f"{pid}.json", {"post_id": pid, "before": row["snapshot"], "after": after})
            if pid not in changed: changed.append(pid)
        transition.update({"phase": "READBACK_VERIFIED", "reset_post_ids": sorted(changed), "updated_at": _now()}); _write_json(transition_path, transition)
        mutation_started = True
        artifact_manifest_path = Path(
            transition.get("artifact_manifest") or (archive / "artifact-manifest.json")
        )
        artifact_manifest = _read_json(artifact_manifest_path, {}) or {}
        archived_roots = artifact_manifest.get("roots") if isinstance(artifact_manifest, dict) else None
        if not isinstance(archived_roots, dict):
            raise ResetEpochError("verified artifact manifest is missing before archive activation")
        moves = (
            (root / "work" / "v2-cycle", "active-v2-cycle", evidence.get("cycle_files_sha256"), None),
            (root / "work" / "v2-journal", "active-v2-journal", evidence.get("journal_sha256"), None),
            (root / "backups", "active-root-backups", None, archived_roots.get(str(root / "backups"))),
            (root / "work" / "backups", "active-work-backups", None, archived_roots.get(str(root / "work" / "backups"))),
        )
        for source, name, expected_sha256, expected_files in moves:
            target = archive / name
            if source.exists() or target.exists():
                _archive_tree_preserving(
                    source,
                    target,
                    expected_sha256=expected_sha256,
                    expected_files=expected_files,
                )
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
