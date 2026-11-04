"""Read-only aggregate report for the local V2 media acquisition funnel."""
from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .registry import registry_path


_SOURCE_FAILURES = {
    "unresolved_source",
    "source_mismatch",
    "source_bytes_mismatch",
    "source_verification_inconclusive",
    "source_page_unavailable",
    "source_asset_unavailable",
    "source_scan_incomplete",
    "source_decode_failed",
    "incomplete_provenance",
    "insufficient_source_context",
}
_REJECT_DISPOSITIONS = {"reject", "rejected", "reject_final", "candidate_rejected"}
_GENERIC_REASONS = {"unknown", "candidate_rejected", "candidate_accepted"}
_SEARCH_COMPLETION_REASONS = {
    "SEARCH_INCOMPLETE", "TARGET_REACHED", "EXHAUSTED_VERIFIED",
    "SOURCE_VERIFICATION_INCOMPLETE", "INCOMPLETE_PROVIDER", "NOT_REQUIRED",
    "INTERRUPTED",
}


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_decision_causes(value: Any) -> set[str]:
    """Read only allow-listed reason codes from the Registry's sanitized evidence."""
    if not isinstance(value, str) or len(value) > 16_384:
        return set()
    try:
        payload = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return set()
    causes = payload.get("decision_causes") if isinstance(payload, dict) else None
    if not isinstance(causes, list):
        return set()
    return {
        str(cause).casefold()
        for cause in causes
        if isinstance(cause, str)
        and (cause.casefold() in _SOURCE_FAILURES or cause.casefold() in _REJECT_DISPOSITIONS or cause.casefold() in {
            "reject", "duplicate_frame", "duplicate_existing_frame", "unrelated",
            "role_unsuitable", "visual_identity_unverified", "vision_provider_error",
            "width_below_minimum", "resolution", "identity_mismatch",
        })
    }


def media_funnel_report(
    root: str | Path,
    *,
    since_days: int = 7,
    top_posts: int = 20,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Summarize media_runs and media_decisions without migrating or writing SQLite."""
    if isinstance(since_days, bool) or not 1 <= int(since_days) <= 3650:
        return {"status": "error", "reason": "since_days_out_of_range", "read_only": True}
    if isinstance(top_posts, bool) or not 1 <= int(top_posts) <= 100:
        return {"status": "error", "reason": "top_posts_out_of_range", "read_only": True}
    path = registry_path(root)
    if path.is_symlink() or not path.is_file():
        return {"status": "unavailable", "reason": "media_registry_missing", "read_only": True}

    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    instant = instant.astimezone(timezone.utc)
    cutoff = instant - timedelta(days=int(since_days))
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            tables = {
                str(row[0])
                for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not {"media_runs", "media_decisions"} <= tables:
                return {
                    "status": "unavailable",
                    "reason": "media_registry_schema_incomplete",
                    "read_only": True,
                }
            required = {
                "media_runs": {"post_id", "queries_attempted", "acquisition_outcome", "completion_reason", "planned_deficits", "created_at"},
                "media_decisions": {"post_id", "role", "reason_code", "disposition", "evidence_reference", "created_at"},
            }
            for table, expected in required.items():
                columns = {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}
                if not expected <= columns:
                    return {
                        "status": "unavailable",
                        "reason": "media_registry_schema_incomplete",
                        "read_only": True,
                    }
            runs = db.execute(
                "SELECT post_id, queries_attempted, acquisition_outcome, completion_reason, planned_deficits, created_at FROM media_runs"
            ).fetchall()
            decisions = db.execute(
                "SELECT post_id, role, reason_code, disposition, evidence_reference, created_at FROM media_decisions"
            ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        return {
            "status": "unavailable",
            "reason": f"media_registry_read_failed:{type(exc).__name__}",
            "read_only": True,
        }

    run_total = 0
    media_phase_runs = 0
    queries_total = 0
    bad_timestamps = 0
    completion_counts: Counter[str] = Counter()
    search_completion_counts: Counter[str] = Counter()
    outcome_counts: Counter[str] = Counter()
    post_counts: dict[int, Counter[str]] = defaultdict(Counter)
    for row in runs:
        created = _parse_time(row["created_at"])
        if created is None:
            bad_timestamps += 1
            continue
        if created < cutoff or created > instant:
            continue
        run_total += 1
        try:
            queries_total += max(0, int(row["queries_attempted"] or 0))
        except (TypeError, ValueError, OverflowError):
            pass
        completion = str(row["completion_reason"] or "unknown")
        outcome = str(row["acquisition_outcome"] or "unknown")
        search_completion = ""
        try:
            planned = json.loads(row["planned_deficits"] or "{}")
            final_metrics = planned.get("final") if isinstance(planned, dict) else {}
            if isinstance(final_metrics, dict):
                search_completion = str(final_metrics.get("search_completion_reason") or "")
                if str(final_metrics.get("phase") or "").casefold() == "media":
                    media_phase_runs += 1
        except (TypeError, ValueError, json.JSONDecodeError):
            search_completion = ""
        completion_counts[completion] += 1
        outcome_counts[outcome] += 1
        if search_completion:
            search_completion_counts[search_completion] += 1
        elif completion in _SEARCH_COMPLETION_REASONS:
            # Older rows stored the search result directly in completion_reason.
            search_completion_counts[completion] += 1
        post_id = int(row["post_id"] or 0)
        if post_id > 0 and (
            search_completion == "SEARCH_INCOMPLETE"
            or (not search_completion and completion == "SEARCH_INCOMPLETE")
        ):
            post_counts[post_id]["search_incomplete_runs"] += 1

    decision_total = 0
    candidate_decisions = 0
    role_counts: Counter[str] = Counter()
    disposition_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    featured_rejects: Counter[str] = Counter()
    for row in decisions:
        created = _parse_time(row["created_at"])
        if created is None:
            bad_timestamps += 1
            continue
        if created < cutoff or created > instant:
            continue
        decision_total += 1
        role = str(row["role"] or "unknown").casefold()
        reason = str(row["reason_code"] or "unknown").casefold()
        disposition = str(row["disposition"] or "unknown").casefold()
        detail_causes = _safe_decision_causes(row["evidence_reference"])
        reason_counts[reason] += 1
        disposition_counts[disposition] += 1
        role_counts[role] += 1
        post_id = int(row["post_id"] or 0)
        if role == "run":
            continue
        candidate_decisions += 1
        source_causes = set()
        if disposition != "accepted":
            source_causes = ({reason} if reason in _SOURCE_FAILURES else set()) | (detail_causes & _SOURCE_FAILURES)
        for source_cause in source_causes:
            source_counts[source_cause] += 1
            if post_id > 0:
                post_counts[post_id][source_cause] += 1
        if role == "featured" and disposition in _REJECT_DISPOSITIONS:
            root_reason = reason
            if root_reason in _GENERIC_REASONS:
                root_reason = next(
                    (cause for cause in sorted(detail_causes) if cause not in _GENERIC_REASONS),
                    "candidate_rejected",
                )
            featured_rejects[root_reason] += 1
            if post_id > 0:
                post_counts[post_id]["featured_rejections"] += 1

    post_rows = [
        {"post_id": post_id, **dict(counts)}
        for post_id, counts in post_counts.items()
    ]
    post_rows.sort(
        key=lambda row: (
            -sum(int(value) for key, value in row.items() if key != "post_id"),
            int(row["post_id"]),
        )
    )
    return {
        "status": "ok",
        "read_only": True,
        "window": {
            "since_days": int(since_days),
            "start": cutoff.isoformat(),
            "end": instant.isoformat(),
        },
        "metrics": {
            "media_runs": run_total,
            "queries_attempted": queries_total,
            "completion_reasons": dict(sorted(completion_counts.items())),
            "search_completion_reasons": dict(sorted(search_completion_counts.items())),
            "acquisition_outcomes": dict(sorted(outcome_counts.items())),
            "media_phase_runs": media_phase_runs,
            "search_incomplete_runs": search_completion_counts.get("SEARCH_INCOMPLETE", 0),
            "media_decisions": decision_total,
            "candidate_decisions": candidate_decisions,
            "decision_roles": dict(sorted(role_counts.items())),
            "decision_dispositions": dict(sorted(disposition_counts.items())),
            "source_failure_reasons": dict(sorted(source_counts.items())),
            "source_mismatch": source_counts.get("source_mismatch", 0),
            "unresolved_source": source_counts.get("unresolved_source", 0),
            "featured_rejections_by_reason": dict(sorted(featured_rejects.items())),
            "featured_rejections": sum(featured_rejects.values()),
            "unparseable_timestamps": bad_timestamps,
        },
        "posts": post_rows[: int(top_posts)],
        "privacy": "aggregate-only; no candidate URLs, evidence_reference, prompts, or content included",
    }


__all__ = ["media_funnel_report"]
