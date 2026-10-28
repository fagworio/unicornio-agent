"""Sanitized, bounded diagnostics for media candidate decisions."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Any
from urllib.parse import urlparse


_SAFE_TOKEN = re.compile(r"[^a-zA-Z0-9_.:-]+")
_MATCHED_FIELDS = frozenset({
    "filename",
    "og_title",
    "page_title",
    "alt_original",
    "figcaption",
    "heading",
    "page_url",
    "query",
})
_SAFE_VERDICTS = frozenset({
    "deterministic_match",
    "ambiguous",
    "insufficient_source_context",
    "unresolved_source",
    "source_mismatch",
    "reject",
    "duplicate_frame",
    "duplicate_existing_frame",
    "capacity_met",
    "previously_rejected_candidate",
})
_SAFE_VERIFICATION = frozenset({
    "unverified", "verified", "inconclusive",
})
_SAFE_DECISIONS = frozenset({"accepted", "rejected", "deferred"})
_SAFE_MEDIA_ROLES = frozenset({"inline", "featured"})
_SAFE_VERIFICATION_STAGES = frozenset({
    "source_page_validation", "candidate_url_validation", "source_page_fetch",
    "asset_extraction", "source_reference", "candidate_image_fetch",
    "source_asset_fetch", "visual_comparison", "asset_comparison_limit",
})
def _bounded_int(value: Any, *, low: int = -100, high: int = 100) -> int | None:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_token(value: Any, *, limit: int = 64) -> str:
    return _SAFE_TOKEN.sub("", str(value or ""))[:limit]


def _sanitize_stage_durations(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    sanitized: dict[str, float] = {}
    for stage, milliseconds in value.items():
        if stage not in _SAFE_VERIFICATION_STAGES:
            continue
        try:
            number = max(0.0, min(600_000.0, float(milliseconds)))
        except (TypeError, ValueError, OverflowError):
            continue
        sanitized[stage] = round(number, 1)
    return dict(sorted(sanitized.items()))


def _verification_category(candidate: dict[str, Any], evidence: dict[str, Any]) -> str:
    """Map candidate evidence to a bounded operational bucket, never a guess."""
    if candidate.get("capacity_deferred"):
        return "not_required_capacity"
    if candidate.get("valid") is True:
        if str(evidence.get("verdict") or "") in {
            "deterministic_match", "ambiguous", "insufficient_source_context"
        }:
            return "source_provenance_verified"
        return "valid_rejected_by_other_gate"

    reason = str(
        candidate.get("source_verification_reason_code")
        or evidence.get("reason_code")
        or ""
    )
    stage = str(candidate.get("verification_stage") or "")
    if reason == "source_page_unavailable":
        return "source_inaccessible"
    if reason == "candidate_asset_unavailable":
        return "candidate_image_inaccessible"
    if reason == "source_asset_unavailable":
        return "source_image_inaccessible"
    if reason == "source_scan_incomplete":
        if stage == "asset_extraction":
            return "parser_insufficient_references"
        return "source_comparison_incomplete"
    if reason == "source_decode_failed":
        return "visual_comparison_inconclusive"
    if reason in {"source_mismatch", "source_bytes_mismatch"}:
        return "definitively_incompatible"
    if reason == "source_reference_found":
        return "source_provenance_verified"
    if not str(candidate.get("source_page_url") or "").strip():
        return "source_unresolved"
    if stage == "visual_comparison":
        return "visual_comparison_inconclusive"
    return "unclassified"


def diagnostic_url_domain(value: Any) -> str:
    """Expose only the host label of a URL in diagnostic telemetry."""
    try:
        return (urlparse(str(value or "")).hostname or "").lower()[:253]
    except (TypeError, ValueError):
        return ""


def sanitize_candidate_diagnostic(
    candidate: dict[str, Any],
    *,
    decision: str = "",
    attempt_id: str = "",
    role: str = "",
) -> dict[str, Any]:
    """Return decision evidence without candidate URLs, query text, or snippets.

    Only bounded identifiers, score values, evidence field names, and
    allow-listed classifications are emitted. In particular, signed URL
    paths/query strings and raw source context never enter telemetry.
    """
    evidence = candidate.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    matched = evidence.get("matched")
    if not isinstance(matched, (list, tuple)):
        matched = ()
    reason = _safe_token(
        candidate.get("source_verification_reason_code") or evidence.get("reason_code"),
        limit=48,
    )
    query = str(candidate.get("query") or "")
    elapsed = _bounded_int(
        candidate.get("verification_elapsed_ms"), low=0, high=600_000
    )
    verdict = str(evidence.get("verdict") or "")
    verification = str(candidate.get("verification_status") or "unverified")
    result = {
        "candidate_id": _safe_token(candidate.get("candidate_id"), limit=64),
        "attempt_id": _safe_token(attempt_id or candidate.get("attempt_id"), limit=64),
        "query_id": hashlib.sha256(query.casefold().encode("utf-8", "ignore")).hexdigest()[:16]
        if query else "",
        "decision": decision if decision in _SAFE_DECISIONS else "",
        "role": role if role in _SAFE_MEDIA_ROLES else "",
        "engine": _safe_token(candidate.get("engine"), limit=24),
        "verdict": verdict if verdict in _SAFE_VERDICTS else "other",
        "gate": _safe_token(evidence.get("gate"), limit=32),
        "evidence_score": _bounded_int(candidate.get("evidence_score", evidence.get("score"))),
        "local_score": _bounded_int(evidence.get("local_score")),
        "matched_fields": sorted({str(item) for item in matched if str(item) in _MATCHED_FIELDS}),
        "source_verification": verification if verification in _SAFE_VERIFICATION else "unverified",
        "source_verification_stage": _safe_token(
            candidate.get("verification_stage"), limit=40
        ),
        "source_verification_category": _verification_category(candidate, evidence),
        "source_verification_reason": reason,
        "verification_elapsed_ms": elapsed,
        "verification_stage_durations_ms": _sanitize_stage_durations(
            candidate.get("verification_stage_durations_ms")
        ),
        "comparisons_attempted": _bounded_int(candidate.get("comparisons_attempted"), low=0, high=1000),
        "source_comparison_proved": bool(candidate.get("source_comparison_proved")),
        "recoverable": bool(candidate.get("source_verification_recoverable") or evidence.get("recoverable")),
        "needs_vision": bool(candidate.get("needs_vision") or evidence.get("needs_vision")),
        "source_reference_present": bool(
            str(candidate.get("source_page_url") or "").strip()
        ),
        "source_verified": bool(candidate.get("valid")),
        "official_source": candidate.get("official_source") is True,
    }
    return result


def sanitize_candidate_diagnostics(
    candidates: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    limit: int = 20,
    decision: str = "",
    attempt_id: str = "",
    role: str = "",
) -> list[dict[str, Any]]:
    """Bound telemetry volume while preserving deterministic candidate order."""
    return [
        sanitize_candidate_diagnostic(
            candidate, decision=decision, attempt_id=attempt_id, role=role
        )
        for candidate in candidates[: max(0, min(int(limit), 20))]
        if isinstance(candidate, dict)
    ]


def _is_relevance_qualified_frame(candidate: dict[str, Any]) -> bool:
    evidence = candidate.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    return (
        candidate.get("valid") is True
        and str(evidence.get("gate") or "") == "relevance"
        and str(evidence.get("verdict") or "") == "deterministic_match"
        and bool(str(candidate.get("direct_image_url") or "").strip())
        and not candidate.get("capacity_deferred")
    )


def _has_visual_hash(candidate: dict[str, Any]) -> bool:
    value = candidate.get("phash")
    return bool(
        isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{16}", value)
    ) or callable(getattr(value, "__sub__", None))


def _distinct_frame_count(candidates: list[dict[str, Any]]) -> int | None:
    try:
        from .visual_hash import canonical_visual_representatives

        return len(canonical_visual_representatives(candidates))
    except Exception:  # noqa: BLE001 - observability must never break acquisition
        return None


def measure_media_capacity(
    reuse_candidates: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    web_candidates: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    required_total: int,
    search_requested_web: int,
) -> dict[str, Any]:
    """Measure distinct candidates passing technical source and relevance gates."""
    reuse_rows = [row for row in reuse_candidates if isinstance(row, dict)]
    web_rows = [row for row in web_candidates if isinstance(row, dict)]
    reuse_qualified = [row for row in reuse_rows if _is_relevance_qualified_frame(row)]
    web_qualified = [row for row in web_rows if _is_relevance_qualified_frame(row)]
    reuse_raw = len(reuse_qualified)
    web_raw = len(web_qualified)
    reuse_frames = reuse_qualified
    web_frames = web_qualified
    reuse_distinct = _distinct_frame_count(reuse_frames)
    web_distinct = _distinct_frame_count(web_frames)
    effective_distinct = _distinct_frame_count(reuse_frames + web_frames)
    identity_available = all(
        count is not None
        for count in (reuse_distinct, web_distinct, effective_distinct)
    )
    required = _bounded_int(required_total, low=0, high=1000)
    requested_web = _bounded_int(search_requested_web, low=0, high=1000)
    required_web = (
        max(0, required - reuse_distinct)
        if required is not None and reuse_distinct is not None else None
    )
    effective_shortfall = (
        max(0, required - effective_distinct)
        if required is not None and effective_distinct is not None else None
    )
    return {
        "media_required_total": required,
        "media_reuse_candidates_raw": reuse_raw,
        "media_reuse_distinct_frames": reuse_distinct,
        "media_required_web": required_web,
        "media_search_requested_web": requested_web,
        "media_web_candidates_raw": web_raw,
        "media_web_distinct_frames": web_distinct,
        "media_web_distinct_shortfall": (
            max(0, required_web - web_distinct)
            if required_web is not None and web_distinct is not None else None
        ),
        "media_effective_distinct_frames": effective_distinct,
        "media_effective_shortfall": effective_shortfall,
        "media_reuse_without_visual_identity": sum(
            not _has_visual_hash(row) for row in reuse_frames
        ),
        "media_web_without_visual_identity": sum(
            not _has_visual_hash(row) for row in web_frames
        ),
        "media_visual_identity_available": identity_available,
        "media_visual_identity_complete": (
            identity_available
            and all(_has_visual_hash(row) for row in reuse_frames + web_frames)
        ),
        "media_effective_capacity_scope": "distinct_frames_passing_source_and_relevance_gates",
    }


def summarize_candidate_diagnostics(
    candidates: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    needed: int | None = None,
    media_capacity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate technical source, relevance and capacity evidence safely."""
    decisions = Counter()
    categories = Counter()
    reason_codes = Counter()
    gates = Counter()
    stages = Counter()
    providers = Counter()
    verification_states = Counter()
    roles = Counter()
    duration_totals = Counter()
    candidate_ids: set[str] = set()
    official_candidates = 0
    for row in candidates:
        if not isinstance(row, dict):
            continue
        decision = str(row.get("decision") or "")
        if decision in _SAFE_DECISIONS:
            decisions[decision] += 1
        role = str(row.get("role") or "")
        if role in _SAFE_MEDIA_ROLES:
            roles[role] += 1
        is_official = row.get("official_source") is True
        if is_official:
            official_candidates += 1
        category = str(row.get("source_verification_category") or "unclassified")
        categories[category] += 1
        stage = _safe_token(row.get("source_verification_stage"), limit=40) or "unknown"
        stages[stage] += 1
        provider = _safe_token(row.get("engine"), limit=24) or "unknown"
        providers[provider] += 1
        verification = str(row.get("source_verification") or "unverified")
        verification_states[
            verification if verification in _SAFE_VERIFICATION else "unverified"
        ] += 1
        for stage, milliseconds in _sanitize_stage_durations(
            row.get("verification_stage_durations_ms")
        ).items():
            duration_totals[stage] += milliseconds
        reason = _safe_token(row.get("source_verification_reason"), limit=48)
        if reason:
            reason_codes[reason] += 1
        gate = _safe_token(row.get("gate"), limit=32) or "unknown"
        verdict = _safe_token(row.get("verdict"), limit=48) or "unknown"
        gates[f"{gate}:{verdict}"] += 1
        candidate_id = _safe_token(row.get("candidate_id"), limit=64)
        if candidate_id:
            candidate_ids.add(candidate_id)
    media_needed = (
        _bounded_int(needed, low=0, high=1000) if needed is not None else None
    )
    capacity_summary: dict[str, Any] = {}
    raw_capacity = media_capacity if isinstance(media_capacity, dict) else {}
    capacity_count_fields = {
        "media_required_total", "media_reuse_candidates_raw", "media_reuse_distinct_frames",
        "media_required_web", "media_search_requested_web", "media_web_candidates_raw",
        "media_web_distinct_frames", "media_web_distinct_shortfall",
        "media_effective_distinct_frames", "media_effective_shortfall",
        "media_reuse_without_visual_identity", "media_web_without_visual_identity",
    }
    for key in capacity_count_fields:
        if key in raw_capacity:
            value = raw_capacity[key]
            capacity_summary[key] = (
                _bounded_int(value, low=0, high=1000) if value is not None else None
            )
    for key in ("media_visual_identity_available", "media_visual_identity_complete"):
        if key in raw_capacity:
            capacity_summary[key] = raw_capacity[key] is True
    if raw_capacity.get("media_effective_capacity_scope") == "distinct_frames_passing_source_and_relevance_gates":
        capacity_summary["media_effective_capacity_scope"] = raw_capacity["media_effective_capacity_scope"]
    summary = {
        "candidates_seen": sum(decisions.values()),
        "unique_candidate_ids": len(candidate_ids),
        "decisions": dict(sorted(decisions.items())),
        "roles": dict(sorted(roles.items())),
        "providers": dict(sorted(providers.items())),
        "verification_stages": dict(sorted(stages.items())),
        "source_verification_states": dict(sorted(verification_states.items())),
        "verification_stage_duration_totals_ms": {
            stage: round(milliseconds, 1)
            for stage, milliseconds in sorted(duration_totals.items())
        },
        "source_verification_categories": dict(sorted(categories.items())),
        "source_verification_reasons": dict(sorted(reason_codes.items())),
        "first_gate_and_verdict": dict(sorted(gates.items())),
        "official_candidates": official_candidates,
        "media_metric_scope": "candidates_passing_source_and_relevance_before_upload",
        "media_needed_candidates": media_needed,
        "media_candidate_shortfall": max(0, media_needed - decisions.get("accepted", 0))
        if media_needed is not None else None,
    }
    summary.update(capacity_summary)
    return summary
