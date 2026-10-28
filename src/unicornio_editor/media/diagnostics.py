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
    "unverified", "verified", "incompatible", "inconclusive",
})
_SAFE_DECISIONS = frozenset({"accepted", "rejected", "deferred"})
_SAFE_MEDIA_ROLES = frozenset({"inline", "featured"})
_SAFE_VERIFICATION_STAGES = frozenset({
    "source_page_validation", "candidate_url_validation", "source_page_fetch",
    "asset_extraction", "source_reference", "candidate_image_fetch",
    "source_asset_fetch", "visual_comparison", "asset_comparison_limit",
})
_SAFE_RIGHTS_STATUSES = frozenset({
    "VERIFIED", "UNPROVEN", "INCOMPATIBLE", "UNSUPPORTED_EVIDENCE",
    "TECHNICAL_FAILURE",
})
_SAFE_RIGHTS_BASES = frozenset({
    "schema.org_image_object", "wikimedia_commons_api",
    "no_exact_image_object", "source_page_unavailable",
    "unsupported_commercial_or_derivative_terms",
    "unsupported_or_incomplete_image_object",
    "image_object_without_bound_license", "wikimedia_api_unavailable",
    "wikimedia_rights_metadata_incomplete",
    "wikimedia_image_identity_unavailable", "wikimedia_image_identity_mismatch",
    "wikimedia_rights_response_invalid", "wikimedia_rights_unverified",
    "legacy_or_missing_evidence",
})
_SAFE_RIGHTS_REASONS = frozenset({
    "image_object_license_match", "media_rights_unproven",
    "media_rights_incompatible", "media_rights_unsupported_evidence",
    "media_rights_unavailable",
})
_RIGHTS_REJECTION_REASONS = frozenset({
    "media_rights_unproven",
    "media_rights_incompatible",
    "media_rights_unsupported_evidence",
    "media_rights_unavailable",
})
_UNRESOLVED_RIGHTS_STATUSES = frozenset({
    "UNPROVEN", "UNSUPPORTED_EVIDENCE", "TECHNICAL_FAILURE",
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
    rights = candidate.get("rights_decision")
    rights = rights if isinstance(rights, dict) else {}
    raw_rights_status = str(rights.get("status") or "")
    rights_status = (
        raw_rights_status if raw_rights_status in _SAFE_RIGHTS_STATUSES else "unassessed"
    )
    raw_rights_basis = str(rights.get("basis") or "")
    rights_basis = (
        raw_rights_basis if raw_rights_basis in _SAFE_RIGHTS_BASES
        else "other" if raw_rights_basis else ""
    )
    raw_rights_reason = str(rights.get("reason_code") or "")
    rights_reason = (
        raw_rights_reason if raw_rights_reason in _SAFE_RIGHTS_REASONS
        else "other" if raw_rights_reason else ""
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
        # Rights evidence is summarized separately from source provenance.
        # Only bounded enums are copied; raw grants, URLs and provider text are
        # intentionally excluded from diagnostics.
        "official_source": candidate.get("official_source") is True,
        "rights_status": rights_status,
        "rights_basis": rights_basis,
        "rights_reason": rights_reason,
        "rights_retryable": rights.get("retryable") is True,
        # Set by the resolver only after the source-bound rights evidence is
        # validated; diagnostics never copy the attestation or its payload.
        "rights_attestation_current": candidate.get("rights_verified") is True,
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


def _rights_status(candidate: dict[str, Any]) -> str:
    decision = candidate.get("rights_decision")
    if not isinstance(decision, dict):
        return "unassessed"
    status = str(decision.get("status") or "")
    return status if status in _SAFE_RIGHTS_STATUSES else "unassessed"


def _has_verified_rights(candidate: dict[str, Any]) -> bool:
    return (
        candidate.get("rights_verified") is True
        and _rights_status(candidate) == "VERIFIED"
    )


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


def _sanitize_effective_rights_blocker(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    status = str(value.get("status") or "")
    reason = str(value.get("reason") or "")
    return {
        "candidate_id": _safe_token(value.get("candidate_id"), limit=64),
        "engine": _safe_token(value.get("engine"), limit=24),
        "status": status if status in _SAFE_RIGHTS_STATUSES else "unassessed",
        "reason": reason if reason in _SAFE_RIGHTS_REASONS else "other",
    }


def measure_rights_capacity(
    reuse_candidates: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    web_candidates: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    required_total: int,
    search_requested_web: int,
) -> dict[str, Any]:
    """Measure proven, distinct rights capacity without affecting media gates.

    Only candidates that passed deterministic source and relevance checks and
    carry the resolver's current verified-rights flag count as usable frames.
    Visual identity is counted with the same canonical pHash grouping used by
    media selection. Missing hashes are not treated as proof of a distinct
    frame. This metric does not change any selection or publication decision.
    """
    reuse_rows = [row for row in reuse_candidates if isinstance(row, dict)]
    web_rows = [row for row in web_candidates if isinstance(row, dict)]
    reuse_raw = sum(_has_verified_rights(row) for row in reuse_rows)
    web_raw = sum(_has_verified_rights(row) for row in web_rows)
    reuse_rights_frames = [row for row in reuse_rows if _has_verified_rights(row)]
    web_rights_frames = [row for row in web_rows if _has_verified_rights(row)]
    reuse_frames = [
        row for row in reuse_rights_frames
        if _is_relevance_qualified_frame(row)
    ]
    web_frames = [
        row for row in web_rights_frames
        if _is_relevance_qualified_frame(row)
    ]
    reuse_distinct = _distinct_frame_count(reuse_frames)
    web_distinct = _distinct_frame_count(web_rights_frames)
    web_effective_distinct = _distinct_frame_count(web_frames)
    effective_distinct = _distinct_frame_count(reuse_frames + web_frames)
    identity_available = all(
        count is not None
        for count in (
            reuse_distinct, web_distinct, web_effective_distinct, effective_distinct
        )
    )
    required = _bounded_int(required_total, low=0, high=1000)
    requested_web = _bounded_int(search_requested_web, low=0, high=1000)
    required_web = (
        max(0, required - reuse_distinct)
        if required is not None and reuse_distinct is not None else None
    )
    first_effective_blocker: dict[str, str] | None = None
    effective_shortfall = (
        max(0, required - effective_distinct)
        if required is not None and effective_distinct is not None else None
    )
    if identity_available and effective_distinct is not None and effective_shortfall:
        verified_frames = reuse_frames + web_frames
        unresolved_statuses = _UNRESOLVED_RIGHTS_STATUSES | {"INCOMPATIBLE"}
        for candidate in web_rows:
            status = _rights_status(candidate)
            rejection_reason = str(candidate.get("rejected_reason") or "")
            if (
                status not in unresolved_statuses
                or candidate.get("rights_checked") is not True
                or rejection_reason not in _RIGHTS_REJECTION_REASONS
                or not _is_relevance_qualified_frame(candidate)
                or not _has_visual_hash(candidate)
            ):
                continue
            increased = _distinct_frame_count(verified_frames + [candidate])
            if increased is None or increased <= effective_distinct:
                continue
            safe = sanitize_candidate_diagnostic(candidate)
            first_effective_blocker = {
                "candidate_id": safe["candidate_id"],
                "engine": safe["engine"],
                "status": safe["rights_status"],
                "reason": safe["rights_reason"] or "other",
            }
            break

    return {
        "rights_required_total": required,
        "rights_reuse_verified_candidates_raw": reuse_raw,
        "rights_reuse_verified": reuse_distinct,
        "rights_required_web": required_web,
        "rights_search_requested_web": requested_web,
        "rights_web_verified_candidates_raw": web_raw,
        "rights_web_verified": web_distinct,
        "rights_verified_candidates_raw": web_raw,
        "rights_verified_distinct_frames": web_distinct,
        "rights_verified_distinct_shortfall": (
            max(0, required_web - web_distinct)
            if required_web is not None and web_distinct is not None else None
        ),
        "rights_verified_distinct_scope": (
            "web_candidates_with_verified_rights_and_proven_phash_before_final_selection"
        ),
        "rights_effective_verified": effective_distinct,
        "rights_effective_shortfall": (
            effective_shortfall
        ),
        "rights_reuse_verified_without_visual_identity": sum(
            not _has_visual_hash(row) for row in reuse_frames
        ),
        "rights_web_verified_without_visual_identity": sum(
            not _has_visual_hash(row) for row in web_rights_frames
        ),
        "rights_visual_identity_available": identity_available,
        "rights_visual_identity_complete": (
            identity_available
            and all(
                _has_visual_hash(row)
                for row in reuse_rights_frames + web_rights_frames
            )
        ),
        "rights_effective_web_verified": web_effective_distinct,
        "rights_effective_capacity_scope": (
            "distinct_frames_passing_source_relevance_and_current_verified_rights_flag"
        ),
        "first_effective_rights_blocker": first_effective_blocker,
    }


def summarize_candidate_diagnostics(
    candidates: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    needed: int | None = None,
    rights_capacity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate candidate-stage evidence without exposing source text.

    Rights counts describe discovered candidates before vision, download,
    upload, or final application; they are not proof of final media capacity
    or of a completed/exhausted search.
    """
    decisions = Counter()
    categories = Counter()
    reason_codes = Counter()
    gates = Counter()
    stages = Counter()
    providers = Counter()
    verification_states = Counter()
    roles = Counter()
    duration_totals = Counter()
    rights_statuses = Counter()
    rights_bases = Counter()
    rights_reasons = Counter()
    candidate_ids: set[str] = set()
    official_candidates = 0
    official_rights_unresolved = 0
    open_license_verified_candidates = 0
    accepted_after_rights = 0
    technical_rights_failures = 0
    verified_rights_candidates = 0
    first_unresolved_rights_candidate: dict[str, str] | None = None
    for row in candidates:
        if not isinstance(row, dict):
            continue
        decision = str(row.get("decision") or "")
        if decision in _SAFE_DECISIONS:
            decisions[decision] += 1
        role = str(row.get("role") or "")
        if role in _SAFE_MEDIA_ROLES:
            roles[role] += 1
        rights_status = str(row.get("rights_status") or "unassessed")
        if rights_status not in _SAFE_RIGHTS_STATUSES:
            rights_status = "unassessed"
        rights_statuses[rights_status] += 1
        rights_basis = str(row.get("rights_basis") or "")
        if rights_basis in _SAFE_RIGHTS_BASES or rights_basis == "other":
            rights_bases[rights_basis] += 1
        rights_reason = str(row.get("rights_reason") or "")
        if rights_reason in _SAFE_RIGHTS_REASONS or rights_reason == "other":
            rights_reasons[rights_reason] += 1
        is_official = row.get("official_source") is True
        if is_official:
            official_candidates += 1
            if rights_status in _UNRESOLVED_RIGHTS_STATUSES:
                official_rights_unresolved += 1
        if (
            rights_status == "VERIFIED"
            and rights_basis in {"schema.org_image_object", "wikimedia_commons_api"}
            and row.get("rights_attestation_current") is True
        ):
            open_license_verified_candidates += 1
        if rights_status == "VERIFIED" and row.get("rights_attestation_current") is True:
            verified_rights_candidates += 1
        if (
            decision == "accepted"
            and rights_status == "VERIFIED"
            and row.get("rights_attestation_current") is True
        ):
            accepted_after_rights += 1
        if rights_status == "TECHNICAL_FAILURE":
            technical_rights_failures += 1
        if (
            first_unresolved_rights_candidate is None
            and rights_status in _UNRESOLVED_RIGHTS_STATUSES | {"INCOMPATIBLE"}
        ):
            first_unresolved_rights_candidate = {
                "candidate_id": _safe_token(row.get("candidate_id"), limit=64),
                "engine": _safe_token(row.get("engine"), limit=24),
                "status": rights_status,
                "reason": rights_reason or "other",
            }
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
    rights_needed = (
        _bounded_int(needed, low=0, high=1000) if needed is not None else None
    )
    capacity_summary: dict[str, Any] = {}
    raw_capacity = rights_capacity if isinstance(rights_capacity, dict) else {}
    capacity_count_fields = {
        "rights_required_total",
        "rights_reuse_verified_candidates_raw",
        "rights_reuse_verified",
        "rights_required_web",
        "rights_search_requested_web",
        "rights_web_verified_candidates_raw",
        "rights_web_verified",
        "rights_verified_candidates_raw",
        "rights_verified_distinct_frames",
        "rights_verified_distinct_shortfall",
        "rights_effective_web_verified",
        "rights_effective_verified",
        "rights_effective_shortfall",
        "rights_reuse_verified_without_visual_identity",
        "rights_web_verified_without_visual_identity",
    }
    for key in capacity_count_fields:
        if key in raw_capacity:
            value = raw_capacity[key]
            capacity_summary[key] = (
                _bounded_int(value, low=0, high=1000) if value is not None else None
            )
    for key in (
        "rights_visual_identity_available", "rights_visual_identity_complete"
    ):
        if key in raw_capacity:
            capacity_summary[key] = raw_capacity[key] is True
    if raw_capacity.get("rights_verified_distinct_scope") == (
        "web_candidates_with_verified_rights_and_proven_phash_before_final_selection"
    ):
        capacity_summary["rights_verified_distinct_scope"] = raw_capacity[
            "rights_verified_distinct_scope"
        ]
    if raw_capacity.get("rights_effective_capacity_scope") == (
        "distinct_frames_passing_source_relevance_and_current_verified_rights_flag"
    ):
        capacity_summary["rights_effective_capacity_scope"] = raw_capacity[
            "rights_effective_capacity_scope"
        ]
    capacity_summary["first_effective_rights_blocker"] = (
        _sanitize_effective_rights_blocker(
            raw_capacity.get("first_effective_rights_blocker")
        )
    )
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
        "rights_status_counts": dict(sorted(rights_statuses.items())),
        "rights_basis_counts": dict(sorted(rights_bases.items())),
        "rights_reason_counts": dict(sorted(rights_reasons.items())),
        "official_candidates": official_candidates,
        "official_rights_unresolved": official_rights_unresolved,
        "open_license_verified_candidates": open_license_verified_candidates,
        "accepted_after_rights": accepted_after_rights,
        "technical_rights_failures": technical_rights_failures,
        "rights_metric_scope": "discovered_candidates_before_vision_and_upload",
        "rights_needed_candidates": rights_needed,
        "rights_verified_candidates": verified_rights_candidates,
        "rights_verified_candidate_shortfall": (
            max(0, rights_needed - verified_rights_candidates)
            if rights_needed is not None else None
        ),
        "rights_candidate_shortfall_scope": (
            "raw_web_candidate_rows_not_distinct_visual_capacity"
        ),
        "rights_verified_candidates_raw": verified_rights_candidates,
        "first_unresolved_rights_candidate": first_unresolved_rights_candidate,
    }
    summary.update(capacity_summary)
    # Keep the old key as a compatibility alias, now with causal rather than
    # merely unresolved-candidate semantics.
    summary["first_rights_blocker"] = summary.get("first_effective_rights_blocker")
    return summary
