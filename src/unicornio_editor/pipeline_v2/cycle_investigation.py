"""Read-only V2 isolation queue and evidence-gated quarantine release."""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
import re
import stat
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..backup import atomic_write_text
from .cycle_recovery_v2 import _not_executed_evidence_is_complete
from .cycle_state import CycleStateStore
from .lock import RunSessionLock
from .quarantine import load_quarantine
from .transaction_admin import _hash_value, _round_post_evidence


_ISOLATION_ID_RE = re.compile(r"UNCERTAIN_ISOLATED\s+([A-Za-z0-9_-]{12,64})")
_SAFE_RELEASE_CLASSIFICATIONS = {
    "COMMITTED_NO_REPLAY",
    "NOT_EXECUTED",
    "PRE_WRITER_RECOVERABLE",
}
_SAFE_REASON = re.compile(r"^[a-zA-Z0-9_.:-]{1,100}$")


def _safe_label(value: Any, *, fallback: str = "unknown") -> str:
    label = str(value or "").casefold().strip()
    return label if _SAFE_REASON.fullmatch(label) else fallback


def _json(path: Path) -> dict[str, Any] | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _transition_path(root: Path, isolation_id: str) -> Path | None:
    cycle_root = root / "work" / "v2-cycle"
    for directory in ("transitions", "transaction-isolations"):
        path = cycle_root / directory / f"{isolation_id}.json"
        value = _json(path)
        if value and value.get("isolation_id") == isolation_id:
            return path
    return None


def _transition_for_post(root: Path, post_id: int, reason: str) -> tuple[Path, dict[str, Any]] | None:
    match = _ISOLATION_ID_RE.search(reason)
    if not match:
        return None
    path = _transition_path(root, match.group(1))
    transition = _json(path) if path else None
    if not path or not transition:
        return None
    classes = transition.get("classes")
    isolated_ids = transition.get("isolated_post_ids")
    if isinstance(classes, Mapping):
        if str(classes.get(str(post_id)) or "") != "UNCERTAIN_ISOLATED":
            return None
    elif isinstance(isolated_ids, list):
        if post_id not in {
            int(value) for value in isolated_ids
            if isinstance(value, int) or str(value or "").isdigit()
        }:
            return None
    elif int(transition.get("post_id") or 0) != int(post_id):
        return None
    raw_ids = transition.get("selected_ids") or transition.get("isolated_post_ids") or [transition.get("post_id")]
    transition_post_ids = {
        int(value) for value in raw_ids
        if isinstance(value, int) or str(value or "").isdigit()
    }
    if int(post_id) not in transition_post_ids:
        return None
    return path, transition


def _created_at_epoch(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (float, int)):
        return float(value) if value > 0 else None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _safe_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    journal = evidence.get("journal") if isinstance(evidence.get("journal"), Mapping) else {}
    wordpress = evidence.get("wordpress") if isinstance(evidence.get("wordpress"), Mapping) else {}
    registry = evidence.get("registry") if isinstance(evidence.get("registry"), Mapping) else {}
    no_write = evidence.get("no_write_evidence") if isinstance(evidence.get("no_write_evidence"), Mapping) else {}
    return {
        "classification": evidence.get("classification"),
        "reason": (
            str(evidence.get("reason"))
            if _SAFE_REASON.fullmatch(str(evidence.get("reason") or ""))
            else "unclassified_evidence"
        ),
        "journal": {
            key: journal.get(key)
            for key in ("present", "sha256", "status", "run_id", "cycle_id", "cycle_round", "authorization_id", "writer_started", "readback")
            if key in journal
        },
        "wordpress": {
            key: wordpress.get(key)
            for key in ("id", "status", "modified", "work_state_sha256", "ready_hash")
            if key in wordpress
        },
        "registry": {
            **{
                key: registry.get(key)
                for key in ("present", "schema_complete", "run_count", "decision_count", "usage_count", "operation_count")
                if key in registry
            },
            **({"snapshot_sha256": registry["snapshot_sha256"]}
               if re.fullmatch(r"[0-9a-f]{64}", str(registry.get("snapshot_sha256") or "")) else {}),
        },
        "no_write_proof_of_absence": no_write.get("proof_of_absence") is True,
    }


def _file_sha256(path: Path, *, required: bool = True) -> str | None:
    if path.is_symlink():
        raise ValueError("evidence_path_is_symlink")
    if not path.is_file():
        if required:
            raise ValueError("required_evidence_file_missing")
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _probe_lock(
    path: Path,
    *,
    legacy_ttl: int = 900,
    empty_recent_is_ambiguous: bool = True,
) -> str:
    """Check a lock without creating it or holding it beyond the probe."""
    if path.is_symlink():
        return "ambiguous_symlink"
    if not path.exists():
        return "missing"
    if not path.is_file():
        return "ambiguous_non_regular_file"
    try:
        fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
    except OSError:
        return "unavailable"
    acquired = False
    try:
        fd_stat = os.fstat(fd)
        if not stat.S_ISREG(fd_stat.st_mode):
            return "ambiguous_non_regular_file"
        path_stat = path.stat()
        if (fd_stat.st_dev, fd_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
            return "ambiguous_replaced_during_probe"
        try:
            # LockManager and RunSessionLock hold LOCK_EX. A shared probe on
            # an O_RDONLY descriptor is portable on Linux and does not
            # require write access to the lock file.
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            return "busy"
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 8192)
        if raw:
            try:
                record = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                record = None
            if isinstance(record, dict) and record.get("token") and not record.get("released"):
                return "ambiguous_stale_owner_record"
            if not isinstance(record, dict) and time.time() - fd_stat.st_mtime <= legacy_ttl:
                return "ambiguous_recent_lock_record"
        elif empty_recent_is_ambiguous and time.time() - fd_stat.st_mtime <= legacy_ttl:
            # LockManager treats a recently-created empty file as an
            # uninitialized legacy owner; do the same without taking it over.
            return "ambiguous_recent_lock_record"
        return "unlocked"
    except OSError:
        return "unavailable"
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def _git_head(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if re.fullmatch(r"[0-9a-fA-F]{40,64}", value) else None


def audit_pending_round_readonly(root: str | Path, client) -> dict[str, Any]:
    """Classify the current authorized round twice without changing any state."""
    root = Path(root)
    cycle_dir = root / "work" / "v2-cycle"
    state_path = cycle_dir / "state.json"
    journal_path = cycle_dir / "journal.jsonl"
    quarantine_path = root / "config" / "v2-quarantine.json"
    try:
        if state_path.is_symlink():
            raise ValueError("checkpoint_path_is_symlink")
        if not state_path.is_file():
            return {
                "status": "no_pending_round",
                "read_only": True,
                "git_sha": _git_head(root),
                "wordpress_writes": 0,
                "replay": False,
            }
        before_hashes = {
            "state": _file_sha256(state_path),
            "cycle_journal": _file_sha256(journal_path, required=False),
            "quarantine": _file_sha256(quarantine_path, required=False),
        }
        state = CycleStateStore(root).load()
        pending = state.pending_round if state and isinstance(state.pending_round, Mapping) else None
        if not state or not pending:
            return {
                "status": "no_pending_round",
                "read_only": True,
                "git_sha": _git_head(root),
                "cycle_id": state.cycle_id if state else None,
                "wordpress_writes": 0,
                "replay": False,
            }
        if before_hashes["cycle_journal"] is None or before_hashes["quarantine"] is None:
            return {
                "status": "blocked",
                "reason": "required_cycle_or_quarantine_evidence_missing",
                "read_only": True,
                "git_sha": _git_head(root),
                "cycle_id": state.cycle_id,
                "wordpress_writes": 0,
                "replay": False,
            }
        selected_ids = pending.get("preview_selected_ids")
        authorization_id = str(pending.get("authorization_id") or "")
        pending_round = pending.get("round_number")
        if selected_ids is None:
            registry_path = root / "work" / "media_registry.sqlite"
            post_journal_dir = root / "work" / "v2-journal"
            initial_post_journals = sorted(post_journal_dir.glob("*.json")) if post_journal_dir.is_dir() else []
            initial_post_hash = hashlib.sha256(
                "\n".join(
                    f"{path.name}:{_file_sha256(path)}" for path in initial_post_journals
                ).encode("utf-8")
            ).hexdigest()
            initial_registry_hash = _file_sha256(registry_path, required=False)
            try:
                preauthorization = CycleStateStore(root).validate_pre_authorization(state)
            except RuntimeError as exc:
                return {
                    "status": "blocked",
                    "reason": "preauthorization_checkpoint_journal_mismatch",
                    "detail": str(exc)[:240],
                    "read_only": True,
                    "git_sha": _git_head(root),
                    "cycle_id": state.cycle_id,
                    "pending_round": pending_round,
                    "authorization_id": None,
                    "selected_ids": None,
                    "wordpress_writes": 0,
                    "replay": False,
                }
            global_lock = _probe_lock(
                root / "work" / "v2-run.lock",
                empty_recent_is_ambiguous=False,
            )
            if global_lock not in {"unlocked", "missing"} or (root / "work" / "v2-maintenance.lock").exists():
                return {
                    "status": "busy_or_ambiguous",
                    "reason": "cycle_or_maintenance_lock_not_idle",
                    "lock_status": global_lock,
                    "read_only": True,
                    "git_sha": _git_head(root),
                    "cycle_id": state.cycle_id,
                    "pending_round": pending_round,
                    "wordpress_writes": 0,
                    "replay": False,
                }
            post_journal_dir = root / "work" / "v2-journal"
            post_journals = sorted(post_journal_dir.glob("*.json")) if post_journal_dir.is_dir() else []
            registry_path = root / "work" / "media_registry.sqlite"
            post_journal_inventory = "\n".join(
                f"{path.name}:{_file_sha256(path)}" for path in post_journals
            )
            registry_hash = _file_sha256(registry_path, required=False)
            after_hashes = {
                "state": _file_sha256(state_path),
                "cycle_journal": _file_sha256(journal_path),
                "quarantine": _file_sha256(quarantine_path),
                "registry": _file_sha256(registry_path, required=False),
                "post_journals": hashlib.sha256(post_journal_inventory.encode("utf-8")).hexdigest(),
            }
            if initial_post_hash != after_hashes["post_journals"]:
                return {
                    "status": "blocked",
                    "reason": "preauthorization_evidence_changed_during_audit",
                    "read_only": True,
                    "git_sha": _git_head(root),
                    "cycle_id": state.cycle_id,
                    "wordpress_writes": 0,
                    "replay": False,
                }
            if any(before_hashes[key] != after_hashes[key] for key in before_hashes) or initial_registry_hash != after_hashes["registry"]:
                return {
                    "status": "blocked",
                    "reason": "preauthorization_evidence_changed_during_audit",
                    "read_only": True,
                    "git_sha": _git_head(root),
                    "cycle_id": state.cycle_id,
                    "wordpress_writes": 0,
                    "replay": False,
                }
            if _probe_lock(
                root / "work" / "v2-run.lock",
                empty_recent_is_ambiguous=False,
            ) not in {"unlocked", "missing"}:
                return {
                    "status": "busy_or_ambiguous",
                    "reason": "cycle_lock_changed_during_audit",
                    "read_only": True,
                    "git_sha": _git_head(root),
                    "cycle_id": state.cycle_id,
                    "wordpress_writes": 0,
                    "replay": False,
                }
            return {
                "status": "preauthorization_interrupted",
                "classification": "SAFE_TO_RESUME_PREVIEW",
                "read_only": True,
                "git_sha": _git_head(root),
                "cycle_id": state.cycle_id,
                "pending_round": pending_round,
                "round_intent_id": preauthorization["round_intent_id"],
                "authorization_id": None,
                "selected_ids": None,
                "evidence": {
                    "matching_round_started_event": True,
                    "round_authorized_event_after_start": False,
                    "global_lock": global_lock,
                    "post_journal_count": len(post_journals),
                    "post_journal_inventory_sha256": after_hashes["post_journals"],
                    "media_registry_sha256": registry_hash,
                    "cycle_checkpoint_sha256": before_hashes["state"],
                    "cycle_journal_sha256": before_hashes["cycle_journal"],
                    "quarantine_sha256": before_hashes["quarantine"],
                    "writer_possible_by_protocol": False,
                },
                "wordpress_writes": 0,
                "wordpress_readbacks": 0,
                "registry_writes": 0,
                "replay": False,
                "next_action": "resume_preview_only; authorization will be created only after selection is persisted",
            }
        if (
            not isinstance(selected_ids, list)
            or not selected_ids
            or any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in selected_ids)
            or len(selected_ids) != len(set(selected_ids))
            or not authorization_id
            or isinstance(pending_round, bool)
            or not isinstance(pending_round, int)
            or pending_round < 1
        ):
            return {
                "status": "blocked",
                "reason": "pending_round_identity_invalid",
                "read_only": True,
                "git_sha": _git_head(root),
                "wordpress_writes": 0,
            }

        try:
            authorization_audit = CycleStateStore(root).audit_round_authorization(state)
        except RuntimeError as exc:
            return {
                "status": "blocked",
                "reason": "round_authorization_checkpoint_journal_mismatch",
                "detail": str(exc)[:240],
                "read_only": True,
                "git_sha": _git_head(root),
                "cycle_id": state.cycle_id,
                "authorization_id": authorization_id,
                "pending_round": pending_round,
                "selected_ids": selected_ids,
                "wordpress_writes": 0,
                "replay": False,
            }
        prepared_authorization = authorization_audit.get("status") == "prepared_recoverable"

        quarantine, quarantine_error = load_quarantine(quarantine_path)
        if quarantine_error:
            return {
                "status": "blocked",
                "reason": "quarantine_policy_invalid",
                "read_only": True,
                "git_sha": _git_head(root),
                "cycle_id": state.cycle_id,
                "authorization_id": authorization_id,
                "pending_round": pending_round,
                "selected_ids": selected_ids,
                "wordpress_writes": 0,
                "replay": False,
            }
        quarantined_selected = sorted(set(selected_ids) & set(quarantine))
        if quarantined_selected:
            return {
                "status": "blocked",
                "reason": "selected_post_already_quarantined",
                "read_only": True,
                "git_sha": _git_head(root),
                "cycle_id": state.cycle_id,
                "authorization_id": authorization_id,
                "pending_round": pending_round,
                "selected_ids": selected_ids,
                "quarantined_selected_ids": quarantined_selected,
                "wordpress_writes": 0,
                "replay": False,
            }

        lock_paths = {
            "global": root / "work" / "v2-run.lock",
            **{
                str(post_id): root / "work" / "locks" / f"{post_id}.lock"
                for post_id in selected_ids
            },
        }

        def probe_locks() -> dict[str, str]:
            values = {
                key: _probe_lock(path, empty_recent_is_ambiguous=(key != "global"))
                for key, path in lock_paths.items()
            }
            if (root / "work" / "v2-maintenance.lock").exists():
                values["maintenance"] = "present"
            return values

        locks_before = probe_locks()
        allowed_idle = {"unlocked", "missing"}
        if any(value not in allowed_idle for key, value in locks_before.items() if key != "maintenance") or "maintenance" in locks_before:
            return {
                "status": "busy_or_ambiguous",
                "read_only": True,
                "git_sha": _git_head(root),
                "cycle_id": state.cycle_id,
                "authorization_id": authorization_id,
                "pending_round": pending_round,
                "selected_ids": selected_ids,
                "lock_status": locks_before,
                "wordpress_writes": 0,
            }

        first_pass = [
            _round_post_evidence(
                root,
                client,
                post_id=post_id,
                cycle_id=state.cycle_id,
                authorization_id=authorization_id,
                pending_round=pending_round,
                capture_media_artifacts=False,
                prepared_authorization=prepared_authorization,
            )
            for post_id in selected_ids
        ]
        second_pass = [
            _round_post_evidence(
                root,
                client,
                post_id=post_id,
                cycle_id=state.cycle_id,
                authorization_id=authorization_id,
                pending_round=pending_round,
                capture_media_artifacts=False,
                prepared_authorization=prepared_authorization,
            )
            for post_id in selected_ids
        ]
        after_hashes = {
            "state": _file_sha256(state_path),
            "cycle_journal": _file_sha256(journal_path),
            "quarantine": _file_sha256(quarantine_path),
        }
        locks_after = probe_locks()
        stable = (
            first_pass == second_pass
            and before_hashes == after_hashes
            and locks_before == locks_after
            and all(value in allowed_idle for key, value in locks_after.items() if key != "maintenance")
            and "maintenance" not in locks_after
        )
        classification_counts: dict[str, int] = {}
        posts = []
        evidence_by_id = {int(row["post_id"]): row for row in (second_pass if stable else first_pass)}
        for post_id in selected_ids:
            evidence = evidence_by_id.get(post_id)
            classification = str(evidence.get("classification") or "unknown") if evidence else "unknown"
            if stable:
                classification_counts[classification] = classification_counts.get(classification, 0) + 1
            registry = evidence.get("registry") if isinstance(evidence, Mapping) else None
            posts.append({
                "post_id": post_id,
                "classification": classification if stable else "UNSTABLE",
                "reason": (
                    _safe_label(evidence.get("reason"), fallback="unclassified_evidence")
                    if evidence and stable else "evidence_changed_during_audit"
                ),
                "registry_complete": bool(
                    isinstance(registry, Mapping)
                    and registry.get("present") is True
                    and registry.get("schema_complete") is True
                ),
                "no_write_proven": bool(
                    evidence
                    and evidence.get("classification") == "NOT_EXECUTED"
                    and _not_executed_evidence_is_complete(evidence)
                ),
                "evidence": _safe_evidence(evidence) if evidence and stable else None,
            })
        if not stable:
            classification_counts = {"UNSTABLE": len(selected_ids)}
        wordpress_readbacks = sum(
            int(evidence.get("wordpress_readbacks") or 0)
            for evidence in first_pass + second_pass
        )
        if prepared_authorization:
            no_write_proven = bool(
                stable
                and len(posts) == len(selected_ids)
                and all(row["no_write_proven"] and row["registry_complete"] for row in posts)
            )
            return {
                "status": (
                    "authorization_prepared_recoverable"
                    if no_write_proven else "blocked"
                ),
                "reason": None if no_write_proven else "prepared_round_effects_or_evidence_uncertain",
                "read_only": True,
                "git_sha": _git_head(root),
                "cycle_id": state.cycle_id,
                "authorization_id": authorization_id,
                "pending_round": pending_round,
                "selected_ids": selected_ids,
                "checkpoint_hashes": before_hashes,
                "lock_status": {"before": locks_before, "after": locks_after},
                "classification_counts": dict(sorted(classification_counts.items())),
                "posts": posts,
                "evidence": {
                    "prepared_event_matches_checkpoint": True,
                    "round_authorized_event_present": False,
                    "checkpoint_and_journal_stable": stable,
                    "all_posts_independently_prove_no_write": no_write_proven,
                    "writer_started": False if no_write_proven else None,
                },
                "wordpress_writes": 0,
                "wordpress_readbacks": wordpress_readbacks,
                "registry_writes": 0,
                "replay": False,
                "automatic_recovery": False,
                "next_action": (
                    "resume_the_exact_prepared_authorization_without_new_selection"
                    if no_write_proven else "preserve_checkpoint_and_escalate_uncertain_post_evidence"
                ),
            }
        return {
            "status": "ok" if stable else "snapshot_changed",
            "read_only": True,
            "git_sha": _git_head(root),
            "cycle_id": state.cycle_id,
            "cycle_status": state.status,
            "phase": state.phase,
            "authorization_id": authorization_id,
            "pending_round": pending_round,
            "selected_ids": selected_ids,
            "rounds": state.rounds,
            "attempts_total": state.attempts_total,
            "checkpoint_hashes": before_hashes,
            "lock_status": {"before": locks_before, "after": locks_after},
            "classification_counts": dict(sorted(classification_counts.items())),
            "posts": posts,
            "wordpress_writes": 0,
            "wordpress_readbacks": wordpress_readbacks,
            "replay": False,
            "safe_for_operator_review": stable and all(row["registry_complete"] for row in posts),
            "automatic_recovery": False,
            "operator_decision_required": True,
        }
    except Exception as exc:
        return {
            "status": "error",
            "reason": f"audit_failed:{type(exc).__name__}",
            "read_only": True,
            "wordpress_writes": 0,
        }


def build_investigation_queue(
    root: str | Path,
    client=None,
    *,
    now: float | None = None,
    systemic_threshold: int = 3,
    age_alert_seconds: int = 24 * 60 * 60,
    count_alert_threshold: int = 10,
) -> dict[str, Any]:
    """Build a sanitized queue from quarantine and current evidence; never writes."""
    root = Path(root)
    policy_path = root / "config" / "v2-quarantine.json"
    if policy_path.is_symlink():
        return {"status": "error", "reason": "quarantine_policy_symlink", "posts": []}
    quarantines, error = load_quarantine(policy_path)
    if error:
        return {"status": "error", "reason": "quarantine_policy_invalid", "error": error, "posts": []}

    instant = float(now if now is not None else time.time())
    rows: list[dict[str, Any]] = []
    causes: dict[str, int] = {}
    for post_id, quarantine in sorted(quarantines.items()):
        reason = str(quarantine.get("reason") or "")
        located = _transition_for_post(root, post_id, reason)
        transition = located[1] if located else None
        current_evidence: dict[str, Any] | None = None
        evidence_status = "unavailable"
        classification = "UNCERTAIN_ISOLATED"
        cause = _safe_label(quarantine.get("reason_code"))
        if transition and transition.get("phase") == "completed":
            classes = transition.get("classes")
            classification = str(
                (
                    (classes or {}).get(str(post_id))
                    if isinstance(classes, Mapping)
                    else transition.get("classification")
                )
                or "UNCERTAIN_ISOLATED"
            )
            if client is not None:
                try:
                    current_evidence = _round_post_evidence(
                        root,
                        client,
                        post_id=post_id,
                        cycle_id=str(transition.get("cycle_id") or ""),
                        authorization_id=str(transition.get("authorization_id") or ""),
                        pending_round=int(transition.get("pending_round") or 0),
                        capture_media_artifacts=False,
                    )
                    classification = str(current_evidence.get("classification") or classification)
                    registry = current_evidence.get("registry")
                    evidence_status = (
                        "current_evidence_available"
                        if isinstance(registry, Mapping)
                        and registry.get("present") is True
                        and registry.get("schema_complete") is True
                        else "registry_evidence_incomplete"
                    )
                    cause = _safe_label(current_evidence.get("reason"), fallback="unclassified_evidence")
                except Exception as exc:
                    evidence_status = f"readback_unavailable:{type(exc).__name__}"
                    cause = evidence_status
        elif transition is not None:
            evidence_status = "transition_incomplete"
            cause = "recovery_transition_incomplete"
        elif client is not None:
            evidence_status = "transition_identity_unavailable"
            cause = "transition_identity_unavailable"

        created_at = _created_at_epoch(
            (transition or {}).get("created_at") or (transition or {}).get("completed_at")
        )
        age_seconds = max(0, int(instant - created_at)) if created_at is not None else None
        if evidence_status == "current_evidence_available" and classification in _SAFE_RELEASE_CLASSIFICATIONS:
            next_action = "review_read_only_evidence_then_authorize_release"
        elif evidence_status.startswith("readback_unavailable") or evidence_status == "registry_evidence_incomplete":
            next_action = "restore_complete_wordpress_and_registry_readback"
        elif evidence_status == "transition_incomplete":
            next_action = "resume_or_audit_original_recovery_transition"
        elif transition is None:
            next_action = "locate_original_transition_and_evidence_before_any_release"
        else:
            next_action = "inspect_journal_registry_and_wordpress_readback_without_replay"

        cause_key = _safe_label(cause)
        causes[cause_key] = causes.get(cause_key, 0) + 1
        reason_summary = (
            f"UNCERTAIN_ISOLATED {transition.get('isolation_id')}; no replay until independently resolved"
            if transition and transition.get("isolation_id")
            else f"quarantined: {_safe_label(quarantine.get('reason_code'))}"
        )
        rows.append({
            "post_id": post_id,
            "reason_code": _safe_label(quarantine.get("reason_code")),
            "reason": reason_summary,
            "classification": classification,
            "evidence_status": evidence_status,
            "age_seconds": age_seconds,
            "created_at_epoch": created_at,
            "cycle_id": (transition or {}).get("cycle_id"),
            "authorization_id": (transition or {}).get("authorization_id"),
            "pending_round": (transition or {}).get("pending_round"),
            "isolation_id": (transition or {}).get("isolation_id"),
            "evidence": _safe_evidence(current_evidence) if current_evidence else None,
            "next_action": next_action,
        })

    rows.sort(key=lambda row: (row["age_seconds"] is None, -(row["age_seconds"] or 0), row["post_id"]))
    oldest = max((row["age_seconds"] for row in rows if row["age_seconds"] is not None), default=None)
    systemic = {
        cause: count for cause, count in sorted(causes.items())
        if count >= max(2, int(systemic_threshold))
    }
    alerts = []
    if len(rows) >= max(1, int(count_alert_threshold)):
        alerts.append({"code": "isolated_queue_high", "count": len(rows)})
    if oldest is not None and oldest >= int(age_alert_seconds):
        alerts.append({"code": "oldest_isolation_overdue", "age_seconds": oldest})
    for cause, count in systemic.items():
        alerts.append({"code": "recurring_recovery_cause", "cause": cause, "count": count})
    return {
        "status": "ok",
        "read_only": True,
        "generated_at": datetime.fromtimestamp(instant, timezone.utc).isoformat(),
        "metrics": {
            "isolated_count": len(rows),
            "oldest_age_seconds": oldest,
            "cause_counts": dict(sorted(causes.items())),
            "systemic_causes": systemic,
        },
        "alerts": alerts,
        "posts": rows,
    }


def release_isolated_post(
    root: str | Path,
    client,
    *,
    post_id: int,
    authorization_id: str,
    authorized_by: str,
    reason: str,
    preview: Mapping[str, Any] | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Release quarantine only after fresh classifier evidence proves a safe state."""
    root = Path(root)
    post_id = int(post_id)
    for label, value in (("authorization_id", authorization_id), ("authorized_by", authorized_by), ("reason", reason)):
        if not isinstance(value, str) or not value.strip():
            return {"status": "blocked", "reason": f"{label}_required", "wordpress_writes": 0}
    for label, value, maximum in (
        ("authorization_id", authorization_id, 128),
        ("authorized_by", authorized_by, 120),
        ("reason", reason, 500),
    ):
        if len(value.strip()) > maximum or any(ord(character) < 32 for character in value):
            return {"status": "blocked", "reason": f"{label}_invalid", "wordpress_writes": 0}
    lock = RunSessionLock(root / "work" / "v2-run.lock")
    if not lock.acquire():
        return {"status": "blocked", "reason": "global_lock_unavailable", "wordpress_writes": 0}
    handles: dict[int, Any] = {}
    try:
        from .cycle_runtime import _reserve_post_lock_batch, _release_post_lock_batch

        handles, locked_post_id, lock_error = _reserve_post_lock_batch(root, [post_id], lock_ttl=900)
        if locked_post_id is not None:
            return {"status": "blocked", "reason": "post_lock_busy", "locked_post_id": locked_post_id, "lock_error": lock_error, "wordpress_writes": 0}

        policy_path = root / "config" / "v2-quarantine.json"
        if policy_path.is_symlink():
            return {"status": "blocked", "reason": "quarantine_policy_symlink", "wordpress_writes": 0}
        quarantines, error = load_quarantine(policy_path)
        if error:
            return {"status": "blocked", "reason": "quarantine_policy_invalid", "wordpress_writes": 0}
        policy_payload = json.loads(policy_path.read_text(encoding="utf-8"))
        if any(
            row.get("authorization_id") == authorization_id
            for row in (policy_payload.get("release_history") or [])
            if isinstance(row, Mapping)
        ):
            return {"status": "blocked", "reason": "release_authorization_id_already_used", "wordpress_writes": 0}
        if post_id not in quarantines:
            history = policy_payload.get("release_history") or []
            existing = next((row for row in history if row.get("post_id") == post_id and row.get("authorization_id") == authorization_id), None)
            return {"status": "already_released" if existing else "blocked", "reason": None if existing else "post_not_quarantined", "wordpress_writes": 0}
        quarantine = quarantines[post_id]
        located = _transition_for_post(root, post_id, quarantine.get("reason", ""))
        if located is None:
            return {"status": "blocked", "reason": "original_recovery_identity_unavailable", "wordpress_writes": 0}
        transition_path, transition = located
        if transition.get("phase") != "completed":
            return {"status": "blocked", "reason": "recovery_transition_incomplete", "wordpress_writes": 0}
        cycle_id = str(transition.get("cycle_id") or "")
        transition_authorization = str(transition.get("authorization_id") or "")
        round_number = transition.get("pending_round")
        if not cycle_id or not transition_authorization or isinstance(round_number, bool) or not isinstance(round_number, int) or round_number < 1:
            return {"status": "blocked", "reason": "original_recovery_identity_invalid", "wordpress_writes": 0}

        evidence = _round_post_evidence(
            root,
            client,
            post_id=post_id,
            cycle_id=cycle_id,
            authorization_id=transition_authorization,
            pending_round=round_number,
            capture_media_artifacts=False,
        )
        classification = str(evidence.get("classification") or "")
        registry = evidence.get("registry")
        if (
            classification not in _SAFE_RELEASE_CLASSIFICATIONS
            or not isinstance(registry, Mapping)
            or registry.get("present") is not True
            or registry.get("schema_complete") is not True
            or (
                classification == "NOT_EXECUTED"
                and not _not_executed_evidence_is_complete(evidence)
            )
        ):
            return {
                "status": "blocked",
                "reason": "current_evidence_does_not_prove_safe_release",
                "classification": classification,
                "evidence": _safe_evidence(evidence),
                "wordpress_writes": 0,
            }

        policy_sha256 = hashlib.sha256(policy_path.read_bytes()).hexdigest()
        transition_sha256 = hashlib.sha256(transition_path.read_bytes()).hexdigest()
        snapshot = {
            "post_id": post_id,
            "quarantine": quarantine,
            "policy_sha256": policy_sha256,
            "transition_sha256": transition_sha256,
            "cycle_id": cycle_id,
            "authorization_id": transition_authorization,
            "pending_round": round_number,
            "classification": classification,
            "evidence": evidence,
            "release_authorization_id": authorization_id,
            "authorized_by": authorized_by,
            "reason": reason,
        }
        snapshot_hash = _hash_value(snapshot)
        result = {
            "status": "preview",
            "post_id": post_id,
            "classification": classification,
            "snapshot_hash": snapshot_hash,
            "evidence": _safe_evidence(evidence),
            "wordpress_writes": 0,
            "registry_writes": 0,
            "eligible_for_apply": True,
        }
        if not apply:
            return result
        if not isinstance(preview, Mapping) or preview.get("snapshot_hash") != snapshot_hash:
            return {"status": "blocked", "reason": "apply_requires_exact_fresh_preview", "wordpress_writes": 0}

        policy_payload["posts"] = [row for row in policy_payload["posts"] if row.get("post_id") != post_id]
        policy_payload["release_history"].append({
            "post_id": post_id,
            "authorization_id": authorization_id.strip(),
            "authorized_by": authorized_by.strip(),
            "reason": reason.strip(),
            "released_at": datetime.now(timezone.utc).isoformat(),
            "classification": classification,
            "evidence_sha256": _hash_value(evidence),
            "quarantine_reason_code": quarantine.get("reason_code"),
        })
        if hashlib.sha256(policy_path.read_bytes()).hexdigest() != policy_sha256:
            return {"status": "blocked", "reason": "quarantine_policy_changed_before_release", "wordpress_writes": 0}
        atomic_write_text(policy_path, json.dumps(policy_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        updated, update_error = load_quarantine(policy_path)
        if update_error or post_id in updated:
            raise RuntimeError("quarantine release readback failed")
        return {
            "status": "released",
            "post_id": post_id,
            "classification": classification,
            "authorization_id": authorization_id.strip(),
            "evidence_sha256": _hash_value(evidence),
            "wordpress_writes": 0,
            "registry_writes": 0,
        }
    finally:
        from .cycle_runtime import _release_post_lock_batch

        _release_post_lock_batch(handles)
        lock.release()


__all__ = ["audit_pending_round_readonly", "build_investigation_queue", "release_isolated_post"]
