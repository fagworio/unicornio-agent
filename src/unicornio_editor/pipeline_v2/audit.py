"""Read-only audits for persisted V2 states.

This module deliberately does not contain a second repair policy.  It calls
the same narrow signature functions used by ``pipeline_v2.migration`` and
only reports whether one of those existing repairs would match.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .migration import (
    HISTORICAL_MEDIA_HUMAN_REQUIRED_IDS,
    HISTORICAL_EDITORIAL_QUALITY_IDS,
    HISTORICAL_MEDIA_DUPLICATE_ID,
    _repair_historical_media_state,
    _repair_known_terminal_state,
)
from .model import LifecycleState, WorkState
from .runtime import load_historical_cohort
from .scheduler import cooldown_status, next_action


def _utc_now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _load_inventory(path: str | Path) -> tuple[list[dict[str, Any]], str | None]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [], f"inventory could not be read: {exc}"
    rows = payload.get("posts") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return [], "inventory must be a list or an object with a posts list"
    if not all(isinstance(row, dict) for row in rows):
        return [], "inventory contains a non-object post"
    return rows, None


def _decode_work_state(raw: Any) -> WorkState | None:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    if not isinstance(raw, dict):
        return None
    try:
        return WorkState.from_dict(raw)
    except (TypeError, ValueError, KeyError):
        return None


def _state_resolution(row: dict[str, Any] | None) -> dict[str, Any]:
    """Resolve inventory state without allowing a legacy snapshot to win.

    ``v2_work_state`` is the explicit inventory projection.  The persisted
    ``_hermes_work_state`` is the next canonical source.  ``state`` is the
    legacy snapshot and is used only when no canonical source is present.
    """
    if not isinstance(row, dict):
        return {"state": None, "source": None, "conflicts": [], "canonical_present": False}

    explicit_present = "v2_work_state" in row
    explicit_raw = row.get("v2_work_state")
    if explicit_present:
        explicit = _decode_work_state(explicit_raw)
        conflicts: list[str] = []
        legacy = _decode_work_state(row.get("state"))
        if explicit is None:
            conflicts.append("v2_work_state_invalid_legacy_ignored")
        elif legacy is not None and legacy.state is not explicit.state:
            conflicts.append("v2_work_state_conflicts_with_legacy_state")
        return {
            "state": explicit,
            "source": "v2_work_state",
            "conflicts": conflicts,
            "canonical_present": True,
        }

    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
    persisted_present = "_hermes_work_state" in row or "_hermes_work_state" in meta
    persisted_raw = row.get("_hermes_work_state") if "_hermes_work_state" in row else meta.get("_hermes_work_state")
    if persisted_present:
        persisted = _decode_work_state(persisted_raw)
        conflicts = [] if persisted is not None else ["_hermes_work_state_invalid_legacy_ignored"]
        legacy = _decode_work_state(row.get("state"))
        if persisted is not None and legacy is not None and legacy.state is not persisted.state:
            conflicts.append("_hermes_work_state_conflicts_with_legacy_state")
        return {
            "state": persisted,
            "source": "_hermes_work_state",
            "conflicts": conflicts,
            "canonical_present": True,
        }

    legacy = _decode_work_state(row.get("state"))
    return {
        "state": legacy,
        "source": "legacy_state" if legacy is not None else None,
        "conflicts": [],
        "canonical_present": False,
    }


def _state_from_inventory_row(row: dict[str, Any]) -> WorkState | None:
    return _state_resolution(row).get("state")


def _media_diagnostics(state: WorkState, row: dict[str, Any] | None = None) -> dict[str, Any]:
    media = state.media
    checklist = row.get("checklist_approved") if isinstance(row, dict) else None
    if checklist is None and isinstance(row, dict) and isinstance(row.get("checklist"), dict):
        checklist = row["checklist"].get("all_passed")
    return {
        "required": media.required,
        "accepted": media.accepted,
        "missing": media.missing,
        "inline_media_ids": [item.media_id for item in media.inline],
        "featured": media.featured.to_dict(),
        "search": media.search.to_dict(),
        "enrichment_round": media.enrichment_round,
        "waiver_applied": media.waiver_applied,
        "waiver_reason": media.waiver_reason,
        "featured_valid": media.featured.status.value == "valid",
        "checklist_approved": checklist,
        "no_progress": state.retry.no_progress,
    }


def classify_human_required_row(
    row: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Classify one inventory row without changing it.

    ``technical_known`` is emitted only when an existing, allowlisted repair
    signature matches exactly.  A normal terminal state is never converted
    into a repair candidate by inference.
    """
    post_id_raw = row.get("post_id", row.get("id"))
    try:
        post_id = int(post_id_raw)
    except (TypeError, ValueError):
        return {
            "post_id": post_id_raw,
            "category": "evidence_insufficient",
            "signature": "invalid_post_id",
            "repair": None,
            "eligible": False,
            "next_action": "provide_valid_post_id_and_state",
        }

    resolution = _state_resolution(row)
    state = resolution["state"]
    if state is None:
        return {
            "post_id": post_id,
            "category": "evidence_insufficient",
            "signature": "missing_or_invalid_work_state",
            "repair": None,
            "eligible": False,
            "next_action": "capture_complete_v2_state",
            "state_source": resolution["source"],
            "state_conflicts": resolution["conflicts"],
        }

    result: dict[str, Any] = {
        "post_id": post_id,
        "state": state.state.value,
        "phase": state.phase.value,
        "blocker": state.blocker.value if state.blocker else None,
        "attempts": state.retry.attempts,
        "phase_attempts": state.retry.phase_attempts,
        "media": _media_diagnostics(state),
        "repair": None,
        "eligible": False,
        "state_source": resolution["source"],
        "state_conflicts": resolution["conflicts"],
    }
    if state.state is not LifecycleState.HUMAN_REQUIRED:
        result.update({
            "category": "not_human_required",
            "signature": "state_not_terminal",
            "next_action": "leave_unchanged",
        })
        return result

    if state.blocker is None:
        result.update({
            "category": "evidence_insufficient",
            "signature": "human_required_without_blocker",
            "next_action": "capture_complete_v2_state",
        })
        return result

    current = _utc_now(now)
    historical = (
        post_id in HISTORICAL_MEDIA_HUMAN_REQUIRED_IDS
        and _repair_historical_media_state(state, current) is not None
    )
    known_terminal = _repair_known_terminal_state(post_id, state, current) is not None
    if historical:
        result.update({
            "category": "technical_known",
            "signature": "audited_historical_media_terminal",
            "repair": "v2-repair-historical-media",
            "eligible": True,
            "next_action": "run_existing_allowlisted_repair_in_dry_run",
        })
    elif known_terminal:
        signature = (
            "audited_media_duplicate"
            if post_id == HISTORICAL_MEDIA_DUPLICATE_ID
            else "audited_editorial_quality"
            if post_id in HISTORICAL_EDITORIAL_QUALITY_IDS
            else "audited_known_terminal"
        )
        result.update({
            "category": "technical_known",
            "signature": signature,
            "repair": "v2-repair-known-terminal-states",
            "eligible": True,
            "next_action": "run_existing_allowlisted_repair_in_dry_run",
        })
    elif state.blocker.value in {
        "text_quality", "seo", "structure", "source", "schema",
        "inline_missing", "featured_missing", "featured_invalid",
        "featured_vision", "media_origin", "media_duplicate", "media_invalid",
    }:
        result.update({
            "category": "legitimate_block",
            "signature": "no_known_repair_signature",
            "next_action": "human_review_or_new_evidence",
        })
    else:
        result.update({
            "category": "evidence_insufficient",
            "signature": "unsupported_terminal_blocker",
            "next_action": "capture_diagnostic_artifacts_before_repair",
        })
    return result


def audit_human_required_inventory(
    path: str | Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a deterministic, read-only report for a local inventory file."""
    rows, error = _load_inventory(path)
    if error:
        return {
            "command": "v2-audit-human-required",
            "inventory": str(path),
            "read_only": True,
            "error": error,
            "counts": {},
            "posts": [],
        }
    posts = [classify_human_required_row(row, now=now) for row in rows]
    counts: dict[str, int] = {}
    for post in posts:
        category = str(post.get("category") or "evidence_insufficient")
        counts[category] = counts.get(category, 0) + 1
    return {
        "command": "v2-audit-human-required",
        "inventory": str(path),
        "read_only": True,
        "counts": counts,
        "posts": posts,
    }


def historical_cohort_report(
    cohort_path: str | Path,
    inventory_path: str | Path,
    *,
    allowlist: tuple[int, ...] = (),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Join a frozen cohort with a local read-only state inventory.

    The cohort remains the immutable admission record; the inventory supplies
    the latest persisted V2 state.  Missing inventory rows are reported as
    insufficient evidence instead of being treated as pending or ready.
    """
    cohort, cohort_error = load_historical_cohort(cohort_path)
    rows, inventory_error = _load_inventory(inventory_path)
    if cohort_error or inventory_error:
        return {
            "command": "v2-cohort-report",
            "read_only": True,
            "cohort": str(cohort_path),
            "inventory": str(inventory_path),
            "error": cohort_error or inventory_error,
            "posts": [],
        }
    inventory_by_id: dict[int, dict[str, Any]] = {}
    for row in rows:
        try:
            post_id = int(row.get("post_id", row.get("id")))
        except (TypeError, ValueError):
            continue
        inventory_by_id[post_id] = row

    current = _utc_now(now)
    explicit_allowlist = {int(post_id) for post_id in allowlist}
    report_posts: list[dict[str, Any]] = []
    for post_id, frozen in sorted(cohort.items()):
        row = inventory_by_id.get(post_id)
        resolution = _state_resolution(row)
        state = resolution["state"]
        wordpress_status = None
        if row is not None:
            wordpress_status = row.get("wordpress_status", row.get("status"))
            if wordpress_status is None and isinstance(row.get("wordpress"), dict):
                wordpress_status = row["wordpress"].get("status")
            if wordpress_status is not None:
                wordpress_status = str(wordpress_status)
        if state is None:
            report_posts.append({
                "post_id": post_id,
                "original_datetime": frozen["original_datetime"].isoformat(),
                "classification": frozen["classification"],
                "eligibility": "allowlisted" if post_id in explicit_allowlist else frozen["classification"],
                "wordpress_status": wordpress_status,
                "v2_state": None,
                "divergence": ["missing_or_invalid_v2_state"],
                "eligibility_real": {
                    "admission": "allowlisted" if post_id in explicit_allowlist else frozen["classification"],
                    "admission_authorized": post_id in explicit_allowlist or frozen.get("classification") == "admitted",
                    "processable": False,
                    "reason": "missing_or_invalid_v2_state",
                },
                "evidence": "missing_or_invalid_state",
                "state_source": resolution["source"],
                "state_conflicts": resolution["conflicts"],
                "state": None,
                "phase": None,
                "blocker": None,
                "media": None,
                "cooldown": None,
                "reevaluations": None,
                "ready": False,
                "published": False,
                "next_action": "capture_complete_v2_state",
            })
            continue

        v2_value = state.state.value
        divergence: list[str] = []
        if wordpress_status == "publish" and v2_value == LifecycleState.READY.value:
            divergence.append("wordpress_publish_v2_ready")
        elif wordpress_status == "publish" and v2_value != LifecycleState.PUBLISHED.value:
            divergence.append("wordpress_publish_v2_not_published")
        elif wordpress_status != "publish" and v2_value == LifecycleState.PUBLISHED.value:
            divergence.append("v2_published_wordpress_not_publish")
        admission = "allowlisted" if post_id in explicit_allowlist else frozen["classification"]
        admission_authorized = (
            post_id in explicit_allowlist
            or frozen.get("classification") == "admitted"
        )
        next_at = state.retry.next_at
        cooldown = cooldown_status(next_at, now=current)
        cooldown_active = cooldown["active"]
        processable = bool(
            admission_authorized
            and wordpress_status == "pending"
            and state.state is LifecycleState.PENDING
            and cooldown_active is False
        )
        eligibility_reason = (
            "admission_and_scheduler_eligible" if processable
            else "historical_not_allowlisted" if not admission_authorized
            else "wordpress_not_pending" if wordpress_status != "pending"
            else "state_not_pending" if state.state is not LifecycleState.PENDING
            else "cooldown_active"
        )
        recommended_action = next_action(state)
        if "wordpress_publish_v2_ready" in divergence:
            recommended_action = "v2-reconcile-publication"
        report_posts.append({
            "post_id": post_id,
            "original_datetime": frozen["original_datetime"].isoformat(),
            "classification": frozen["classification"],
            "eligibility": admission,
            "eligibility_real": {
                "admission": admission,
                "admission_authorized": admission_authorized,
                "processable": processable,
                "reason": eligibility_reason,
            },
            "wordpress_status": wordpress_status,
            "v2_state": v2_value,
            "divergence": divergence,
            "evidence": "state_present",
            "state_source": resolution["source"],
            "state_conflicts": resolution["conflicts"],
            "state": state.state.value,
            "phase": state.phase.value,
            "blocker": state.blocker.value if state.blocker else None,
            "media": _media_diagnostics(state, row),
            "cooldown": {**cooldown, "next_at": next_at},
            "reevaluations": {
                "attempts": state.retry.attempts,
                "phase_attempts": state.retry.phase_attempts,
            },
            "ready": state.state is LifecycleState.READY,
            "published": state.state is LifecycleState.PUBLISHED,
            "next_action": recommended_action,
        })

    return {
        "command": "v2-cohort-report",
        "read_only": True,
        "cohort": str(cohort_path),
        "inventory": str(inventory_path),
        "count": len(report_posts),
        "counts": {
            "ready": sum(post["ready"] for post in report_posts),
            "published": sum(post["published"] for post in report_posts),
            "missing_state": sum(post["evidence"] != "state_present" for post in report_posts),
        },
        "posts": report_posts,
    }
