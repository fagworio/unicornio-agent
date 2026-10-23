"""Sanitized, bounded diagnostics for media candidate decisions."""

from __future__ import annotations

import re
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


def _bounded_int(value: Any, *, low: int = -100, high: int = 100) -> int | None:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_token(value: Any, *, limit: int = 64) -> str:
    return _SAFE_TOKEN.sub("", str(value or ""))[:limit]


def diagnostic_url_domain(value: Any) -> str:
    """Expose only the host label of a URL in diagnostic telemetry."""
    try:
        return (urlparse(str(value or "")).hostname or "").lower()[:253]
    except (TypeError, ValueError):
        return ""


def sanitize_candidate_diagnostic(candidate: dict[str, Any]) -> dict[str, Any]:
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
    verdict = str(evidence.get("verdict") or "")
    verification = str(candidate.get("verification_status") or "unverified")
    result = {
        "candidate_id": _safe_token(candidate.get("candidate_id"), limit=64),
        "engine": _safe_token(candidate.get("engine"), limit=24),
        "verdict": verdict if verdict in _SAFE_VERDICTS else "other",
        "gate": _safe_token(evidence.get("gate"), limit=32),
        "evidence_score": _bounded_int(candidate.get("evidence_score", evidence.get("score"))),
        "local_score": _bounded_int(evidence.get("local_score")),
        "matched_fields": sorted({str(item) for item in matched if str(item) in _MATCHED_FIELDS}),
        "source_verification": verification if verification in _SAFE_VERIFICATION else "unverified",
        "source_verification_reason": reason,
        "comparisons_attempted": _bounded_int(candidate.get("comparisons_attempted"), low=0, high=1000),
        "source_comparison_proved": bool(candidate.get("source_comparison_proved")),
        "recoverable": bool(candidate.get("source_verification_recoverable") or evidence.get("recoverable")),
        "needs_vision": bool(candidate.get("needs_vision") or evidence.get("needs_vision")),
        "source_available": bool(str(candidate.get("source_page_url") or "").strip()),
        "source_verified": bool(candidate.get("valid")),
    }
    return result


def sanitize_candidate_diagnostics(
    candidates: list[dict[str, Any]] | tuple[dict[str, Any], ...], *, limit: int = 20
) -> list[dict[str, Any]]:
    """Bound telemetry volume while preserving deterministic candidate order."""
    return [
        sanitize_candidate_diagnostic(candidate)
        for candidate in candidates[: max(0, min(int(limit), 20))]
        if isinstance(candidate, dict)
    ]
