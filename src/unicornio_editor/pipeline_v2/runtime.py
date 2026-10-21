"""Minimal production bridge: buffered runner, writer, journal and v2-run."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from collections.abc import Collection
from time import monotonic
from typing import Any, Callable
from urllib.parse import urlparse, urlsplit, urlunsplit

# Keep the legacy symbol imported: existing integrations/tests patch this
# module-level name while the production path uses the canonical HTML helper.
from ..checklist import required_image_count, required_image_count_for_content
from ..content_quality import word_count
from ..manifest import build_ready_manifest, manifest_hash, serialize_manifest
from ..media.evidence import diverse_item_queries, editorial_subjects, item_query, post_subjects
from ..media.relevance import iter_content_images
from ..list_quality import detect_list_format
from ..seo.rank_math import build_meta
from ..state import STATE_READY, build_state_markers
from ..workflow import _execute_media_plan, validate_media_plan
from .lock import RunSessionLock
from .model import FeaturedProgress, FeaturedStatus, InlineMedia, MediaProgress, MediaSearchProgress, Phase, WorkState
from .media_accounting import account_visual_media, reconcile_working_visual_media
from .quarantine import load_quarantine
from .production import ProductionCandidateReader, validation_matches_candidate
from .production_stages import ProductionComposeStage, ProductionEditorialStage, ProductionMediaStage, ProductionValidateStage
from .runner import PipelineRunner
from .scheduler import _cooldown_expired, select


_DEFAULT_REQUIRED_IMAGE_COUNT = required_image_count


def _canonical_embedded_media_url(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return str(value or "").strip()
    return urlunsplit((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        parsed.path.rstrip("/") or "/",
        parsed.query,
        "",
    ))


def _coverage_query_key(value: Any) -> str:
    """Normalize query identity only for harmless whitespace/case differences."""
    return " ".join(str(value or "").split()).casefold()


def _media_audit_summary(media: MediaProgress, write: dict[str, Any]) -> dict[str, Any]:
    """Expose accepted-versus-applied media facts in one bounded post result."""
    accounting = account_visual_media(
        media,
        required_inline=media.required,
        featured=media.featured,
    )
    search = media.search
    return {
        "required_inline": accounting.required_inline,
        "accepted_working": accounting.accepted_working_count,
        "valid_distinct": accounting.valid_distinct_inline_count,
        "effective_missing": accounting.effective_missing,
        "featured_status": media.featured.status.value,
        "search": {
            "completed": search.completed,
            "completion_reason": search.completion_reason,
            "queries_attempted": search.queries_attempted,
            "queries_completed": search.queries_completed,
            "candidates_seen": search.candidates_seen,
            "candidates_rejected": search.candidates_rejected,
            "engines_attempted": list(search.engines_attempted),
            "engines_disabled": list(search.engines_disabled),
            "coverage": search.coverage,
        },
        "applied_media_ids": list(write.get("applied_media_ids") or []),
        "applied_inline_ids": list(write.get("applied_inline_ids") or []),
        "media_readback_status": str(write.get("media_readback_status") or "not_attempted"),
    }


def _coverage_run_is_valid(run: dict[str, Any], planned_query: str) -> bool:
    """Validate one provider-coverage proof against the query actually planned."""
    query = run.get("query")
    expected = run.get("expected_engines")
    reported = run.get("reported_engines")
    results = run.get("engine_results")
    planned = run.get("planned_queries")
    completed = run.get("completed_queries")
    failures = run.get("technical_failures")
    return bool(
        type(run.get("version")) is int
        and run.get("version") == 1
        and isinstance(query, str)
        and _coverage_query_key(query) == _coverage_query_key(planned_query)
        and run.get("verified") is True
        and run.get("strategy_complete") is True
        and isinstance(expected, list)
        and expected
        and all(isinstance(engine, str) and engine for engine in expected)
        and isinstance(reported, list)
        and all(isinstance(engine, str) and engine for engine in reported)
        and set(expected) <= set(reported)
        and isinstance(failures, list)
        and not failures
        and isinstance(planned, int)
        and not isinstance(planned, bool)
        and planned == 1
        and isinstance(completed, int)
        and not isinstance(completed, bool)
        and completed == planned
        and isinstance(results, dict)
        and all(results.get(engine) in {"ok", "no_results_legitimate"} for engine in expected)
    )


def _aggregate_active_coverage(
    search_runs: list[dict[str, Any]], planned_queries: list[str]
) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
    """Aggregate only a one-to-one set of valid proofs for this search pass."""
    proofs: list[dict[str, Any]] = []
    valid = bool(planned_queries) and len(search_runs) == len(planned_queries)
    for index, planned_query in enumerate(planned_queries):
        run = search_runs[index] if index < len(search_runs) else {}
        valid = valid and isinstance(run, dict) and (
            _coverage_query_key(run.get("_planned_query"))
            == _coverage_query_key(planned_query)
        )
        coverage = run.get("coverage") if isinstance(run, dict) else None
        if isinstance(coverage, dict):
            proof = dict(coverage)
        else:
            proof = {
                "query": planned_query,
                "verified": False,
                "strategy_complete": False,
                "technical_failures": ["coverage_missing"],
            }
        proofs.append(proof)
        valid = valid and _coverage_run_is_valid(proof, planned_query)

    for run in search_runs[len(planned_queries):]:
        coverage = run.get("coverage") if isinstance(run, dict) else None
        proofs.append(
            dict(coverage)
            if isinstance(coverage, dict)
            else {
                "query": str(run.get("_planned_query") or "") if isinstance(run, dict) else "",
                "verified": False,
                "strategy_complete": False,
                "technical_failures": ["unplanned_search_run"],
            }
        )

    queries = [str(proof.get("query") or "") for proof in proofs]
    unique_queries = len({_coverage_query_key(query) for query in queries}) == len(queries)
    valid = valid and unique_queries
    def engine_names_for(field: str) -> list[str]:
        return sorted({
            engine
            for proof in proofs
            for values in [proof.get(field)]
            if isinstance(values, list)
            for engine in values
            if isinstance(engine, str) and engine
        })

    engine_names = engine_names_for("expected_engines")
    if valid:
        active = {
            "version": 1,
            "verified": True,
            "strategy_complete": True,
            "queries": queries,
            "expected_engines": engine_names,
            "reported_engines": engine_names_for("reported_engines"),
            "engine_results": {
                str(proof["query"]): dict(proof["engine_results"])
                for proof in proofs
            },
            "technical_failures": [],
            "planned_queries": len(planned_queries),
            "completed_queries": len(planned_queries),
            "accepted_visual_representatives": sum(
                proof.get("accepted_visual_representatives")
                for proof in proofs
                if type(proof.get("accepted_visual_representatives")) is int
                and proof["accepted_visual_representatives"] >= 0
            ),
        }
    else:
        active = {
            "version": 1,
            "verified": False,
            "strategy_complete": False,
            "reason": "active_strategy_incomplete",
            "queries": queries,
            "engine_results": {
                str(proof.get("query") or (
                    planned_queries[index] if index < len(planned_queries) else ""
                )): dict(proof.get("engine_results") or {})
                for index, proof in enumerate(proofs)
                if isinstance(proof.get("engine_results"), dict)
            },
        }
    return active, proofs, valid


def _attach_candidate_vision_evidence(
    candidate: dict[str, Any], vision_evidence: dict[str, Any]
) -> dict[str, Any]:
    """Carry preflight/APPLY visual evidence into the exact registry candidate."""
    evidence = candidate.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {}
        candidate["evidence"] = evidence
    evidence["vision_decision"] = dict(vision_evidence)
    evidence["vision_policy_version"] = str(vision_evidence.get("policy_version") or "")
    evidence["vision_image_sha256"] = str(vision_evidence.get("image_sha256") or "")
    if (
        vision_evidence.get("status") == "UNRELATED"
        and vision_evidence.get("verdict") == "reject"
    ):
        evidence["verdict"] = "unrelated"
    return candidate


def _embedded_media_urls(content: str) -> list[str]:
    """Return only URLs used by actual img elements, never plain text URLs."""
    return [
        _canonical_embedded_media_url(str(item.get("src") or ""))
        for item in iter_content_images(str(content or ""))
        if str(item.get("src") or "").strip()
    ]


def _assert_candidate_media_contract(
    content: str,
    media: MediaProgress | None,
    *,
    title: str = "",
) -> None:
    """Reject inline HTML that is not present in the approved V2 media contract."""
    allowed_urls = {
        _canonical_embedded_media_url(str(item.media_url or ""))
        for item in getattr(media, "inline", ())
        if str(item.media_url or "").strip()
    }
    unexpected_urls = [
        url for url in _embedded_media_urls(content)
        if url not in allowed_urls
    ]
    if unexpected_urls:
        raise RuntimeError(
            "unexpected inline media in candidate contract: "
            + ", ".join(unexpected_urls[:5])
        )
    if title and media is not None:
        from .media_accounting import account_visual_media
        from ..media.evidence import post_subjects

        rows = post_subjects(title=title, content_html=content)
        canonical_items = any(
            str(getattr(item, "item_identity", "") or "").strip()
            for item in getattr(media, "inline", ())
        )
        if len(rows) > 1 and canonical_items:
            accounting = account_visual_media(
                media,
                required_inline=max(len(rows), int(getattr(media, "required", 0) or 0)),
                listicle_items=rows,
            )
            if accounting.uncovered_listicle_items:
                raise RuntimeError(
                    "approved candidate does not cover list items: "
                    + ", ".join(accounting.uncovered_listicle_items)
                )


def _required_image_count_for_editorial(content: str, *, title: str = "") -> int:
    """Use the canonical helper while honoring legacy test integrations.

    A few callers patch ``runtime.required_image_count`` to model a fixed
    editorial quota.  Keep that seam working; normal production execution
    goes through the HTML-aware canonical helper.
    """
    body = str(content or "")
    if required_image_count is not _DEFAULT_REQUIRED_IMAGE_COUNT:
        return required_image_count(word_count(body), title=title, content=body)
    return required_image_count_for_content(body, title=title)


def _media_rejection_history(root: Path, post_id: int) -> dict[str, Any]:
    """Read prior media rejection evidence without changing checkpoints."""
    from ..media.vision_policy import VISION_POLICY_VERSION

    reasons: set[str] = set()
    urls: set[str] = set()
    fingerprints: set[str] = set()
    queries: set[str] = set()
    revalidation_urls: set[str] = set()
    revalidation_fingerprints: set[str] = set()
    paths = [
        root / "backups" / str(post_id) / "editorial.validation.json",
        root / "backups" / str(post_id) / "editorial.candidate.json",
    ]
    paths.extend(root.glob(f"work/batches/*/media.manifest.json"))
    for path in paths:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        rows = list(payload.get("failures") or [])
        rows.extend(payload.get("media_plan_results") or [])
        rows.extend(payload.get("rejected") or [])
        for post in payload.get("posts") or []:
            if isinstance(post, dict) and int(post.get("post_id") or 0) == int(post_id):
                rows.extend(post.get("audit_candidates") or [])
        for row in rows:
            if not isinstance(row, dict):
                continue
            parent_reason = row.get("rejected_reason") or row.get("detail") or row.get("reason")
            parent_disposition = str(row.get("disposition") or "").casefold()
            rows.extend(
                {
                    **item,
                    "disposition": item.get("disposition") or parent_disposition,
                    "rejected_reason": item.get("rejected_reason") or parent_reason,
                    # ``invalid_media`` is derived evidence. Preserve the
                    # parent's original cause so a guard label such as
                    # ``previously_rejected_candidate`` cannot hide whether
                    # the source was actually transient or definitive.
                    "original_reason": (
                        item.get("original_reason")
                        or item.get("original_rejected_reason")
                        or row.get("original_reason")
                        or row.get("original_rejected_reason")
                        or row.get("detail")
                        or row.get("reason")
                    ),
                    "original_rejected_reason": (
                        item.get("original_rejected_reason")
                        or row.get("original_rejected_reason")
                    ),
                    "original_gate": item.get("original_gate") or row.get("original_gate"),
                    "evidence": item.get("evidence") or row.get("evidence"),
                }
                for item in (row.get("invalid_media") or []) if isinstance(item, dict)
            )
            verdict = str((row.get("evidence") or {}).get("verdict") or "").casefold()
            raw_disposition = str(row.get("disposition") or "").casefold()
            if raw_disposition == "defer_retry":
                continue
            raw_reason_value = str(row.get("rejected_reason") or row.get("reason") or row.get("detail") or "").casefold()
            # This is a derived guard, not original gate evidence.  Do not let
            # its own label become the reason for a permanent blacklist.
            derived_rejection = "previously_rejected_candidate" in raw_reason_value or verdict == "previously_rejected_candidate"
            evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
            evidence_verdict = str(evidence.get("verdict") or "").casefold()
            original_cause = " ".join(
                str(row.get(key) or "")
                for key in (
                    "original_reason", "original_rejected_reason", "original_gate",
                    "rejected_reason", "detail", "reason",
                )
            ).casefold()
            definitive_causes = (
                "source_mismatch", "duplicate_existing_frame", "duplicate_frame",
                "unrelated", "width_below_minimum",
            )
            transient_causes = (
                "timeout", "network_error", "network", "source_page_unreachable",
                "unreachable", "tempor", "cdn", "source_page_unavailable",
                "source_asset_unavailable", "source_scan_incomplete", "source_decode_failed",
            )
            has_definitive_cause = bool(
                evidence_verdict in {"reject", "source_mismatch", "duplicate_existing_frame", "duplicate_frame", "unrelated"}
                or any(token in original_cause for token in definitive_causes)
            )
            has_transient_cause = any(token in original_cause for token in transient_causes)
            if has_transient_cause and not has_definitive_cause:
                continue
            if derived_rejection and not has_definitive_cause:
                continue
            # ``unresolved_source`` is an inconclusive provider/provenance
            # result, not proof that a URL is permanently invalid. Keep it
            # eligible for a later acquisition unless another explicit,
            # definitive cause accompanies the record.
            unresolved_source = (
                evidence_verdict == "unresolved_source"
                or "unresolved_source" in raw_reason_value
            )
            if unresolved_source and not has_definitive_cause:
                continue
            evidence_score_value = evidence.get("score")
            try:
                weak_evidence = evidence_score_value is not None and int(evidence_score_value) < 4
            except (TypeError, ValueError):
                weak_evidence = False
            source_comparison_proved = bool(
                row.get("source_comparison_proved")
                or evidence.get("source_comparison_proved")
            )
            needs_revalidation = (
                evidence_verdict == "source_mismatch" and not source_comparison_proved
            ) or (
                evidence_verdict == "reject"
                and weak_evidence
                and not evidence.get("vision_decision")
            )
            if needs_revalidation:
                for key in ("url", "media_url", "direct_image_url", "source_image_url"):
                    value = str(row.get(key) or "").strip()
                    if value:
                        revalidation_urls.add(_canonical_embedded_media_url(value))
                for key in ("sha256", "phash", "visual_group_id"):
                    value = str(row.get(key) or "").strip()
                    if value:
                        revalidation_fingerprints.add(value)
                continue
            is_rejection = bool(
                row.get("rejected_reason")
                or raw_disposition in {"reject_final", "defer_retry"}
                or verdict in {"reject", "source_mismatch", "duplicate_existing_frame", "duplicate_frame", "unrelated", "previously_rejected_candidate"}
                or any(token in raw_reason_value for token in ("duplicate", "rejeit", "unrelated", "inacess", "diverg"))
            )
            if not is_rejection:
                continue
            raw_reason = str(
                row.get("original_reason")
                or row.get("original_rejected_reason")
                or row.get("detail")
                or row.get("reason")
                or row.get("rejected_reason")
                or ""
            ).strip()
            lowered = raw_reason.casefold()
            if "source_mismatch" in lowered:
                reason = "source_mismatch"
            elif "duplicate_existing_frame" in lowered or "duplicate" in lowered or "duplicat" in lowered:
                reason = "duplicate_existing_frame"
            elif "width_below_minimum" in lowered or "px wide" in lowered or "resolu" in lowered:
                reason = "width_below_minimum"
            elif "source_page_unreachable" in lowered or "inacessível" in lowered or "unreachable" in lowered:
                reason = "source_page_unreachable"
            elif "source_bytes_mismatch" in lowered or "bytes" in lowered and "diverg" in lowered:
                reason = "source_bytes_mismatch"
            elif "unrelated" in lowered or "sem relação" in lowered:
                reason = "unrelated"
            elif "baseline_identity_unavailable" in lowered:
                reason = "baseline_identity_unavailable"
            else:
                reason = raw_reason
            if reason:
                reasons.add(reason)
            vision_decision = evidence.get("vision_decision") if isinstance(evidence.get("vision_decision"), dict) else {}
            historical_policy = str(
                row.get("vision_policy_version")
                or evidence.get("vision_policy_version")
                or vision_decision.get("policy_version")
                or ""
            )
            # UNRELATED is a semantic/model judgment, not immutable evidence
            # about the bytes. Legacy decisions without a compatible policy
            # version remain auditable but cannot permanently blacklist the
            # candidate; deterministic failures (duplicates, source mismatch,
            # etc.) continue to be retained exactly as before.
            stale_semantic_rejection = (
                reason == "unrelated"
                and historical_policy != VISION_POLICY_VERSION
            )
            if not stale_semantic_rejection:
                for key in ("url", "media_url", "direct_image_url", "source_image_url"):
                    value = str(row.get(key) or "").strip()
                    if value:
                        urls.add(_canonical_embedded_media_url(value))
                for key in ("sha256", "phash", "visual_group_id"):
                    value = str(row.get(key) or "").strip()
                    if value:
                        fingerprints.add(value)
            query = str(row.get("search_query") or row.get("query") or "").strip()
            if query:
                queries.add(query.casefold())
    return {
        "reasons": reasons,
        "urls": urls,
        "fingerprints": fingerprints,
        "queries": queries,
        "revalidation_urls": revalidation_urls,
        "revalidation_fingerprints": revalidation_fingerprints,
    }


def _adaptive_query_suffix(history: dict[str, Any], *, role: str) -> tuple[str, str]:
    reasons = {str(value).casefold() for value in history.get("reasons") or ()}
    if "duplicate_existing_frame" in reasons or "duplicat" in " ".join(reasons):
        return " alternate scene different composition", "avoid_previous_duplicate"
    if "width_below_minimum" in reasons or "dimens" in " ".join(reasons):
        return " original high resolution", "prefer_verified_high_resolution"
    if "source_page_unreachable" in reasons or "source_bytes_mismatch" in reasons:
        return " official source press kit", "seek_alternative_verifiable_source"
    if "unrelated" in reasons:
        return " official artwork character specific", "increase_entity_specificity"
    if "baseline_identity_unavailable" in reasons:
        return " alternate official artwork", "avoid_unverified_baseline"
    if role == "featured":
        return " official key art wide", "fill_featured_deficit"
    return "", ""


class BufferedStateStore:
    def __init__(self, initial):
        self.state = initial

    def load(self, post_id: int):
        return self.state

    def commit(self, post_id: int, state):
        self.state = state


def _parse_admission_datetime(value: Any) -> datetime | None:
    """Return an aware UTC datetime or ``None`` for an unusable value."""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _original_post_datetime(context: dict[str, Any]) -> datetime | None:
    """Read the immutable WordPress publication date used for admission."""
    post = context.get("post")
    values = []
    if isinstance(post, dict):
        values.extend((post.get("date_gmt"), post.get("date")))
    values.append(context.get("date"))
    for value in values:
        parsed = _parse_admission_datetime(value)
        if parsed is not None:
            return parsed
    return None


def load_historical_cohort(path: str | Path | None) -> tuple[dict[int, dict[str, Any]], str | None]:
    """Load an optional frozen admission cohort without touching WordPress.

    Schema v1 is intentionally small and auditable::

        {"version": 1, "posts": [{"post_id": 123,
          "original_datetime": "...", "classification": "historical"}]}

    A malformed configured manifest is an admission error, not a reason to
    fall back to mutable WordPress dates.
    """
    if not path:
        return {}, None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {}, f"historical cohort could not be read: {exc}"
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return {}, "historical cohort schema version must be 1"
    rows = payload.get("posts")
    if not isinstance(rows, list):
        return {}, "historical cohort posts must be a list"
    result: dict[int, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            return {}, "historical cohort contains a non-object post"
        try:
            post_id = int(row["post_id"])
        except (KeyError, TypeError, ValueError):
            return {}, "historical cohort post_id must be an integer"
        if post_id < 1 or post_id in result:
            return {}, "historical cohort has duplicate or invalid post_id"
        original = _parse_admission_datetime(row.get("original_datetime"))
        classification = str(row.get("classification") or "").strip().lower()
        if original is None or classification not in {"historical", "admitted"}:
            return {}, "historical cohort row has invalid date or classification"
        result[post_id] = {
            "original_datetime": original,
            "classification": classification,
        }
    return result, None


def load_v2_quarantine(path: str | Path) -> tuple[dict[int, dict[str, str]], str | None]:
    """Read explicit post quarantines; malformed/missing policy fails closed."""
    return load_quarantine(path)


def admit_v2_candidates(
    snapshot: list[tuple[int, dict[str, Any]]],
    admission_after: Any,
    admission_allowlist: tuple[int, ...] = (),
    historical_cohort: dict[int, dict[str, Any]] | None = None,
    cohort_error: str | None = None,
    cohort_configured: bool | None = None,
) -> tuple[list[tuple[int, dict[str, Any]]], dict[str, Any]]:
    """Admit only posts created at/after the fixed V2 production cutoff.

    The decision deliberately ignores phase and lifecycle state. Once a post
    crosses the cutoff it remains eligible for its normal V2 retry lifecycle;
    terminal/cooldown handling is left to the scheduler. Missing configuration
    or post dates fail closed and are exposed in the audit report.
    """
    cutoff = _parse_admission_datetime(admission_after)
    allowlist = {int(post_id) for post_id in admission_allowlist}
    admitted: list[tuple[int, dict[str, Any]]] = []
    historical_ids: list[int] = []
    allowlisted_ids: list[int] = []
    blocked_ids: list[int] = []
    missing_date_ids: list[int] = []
    admission_reasons: list[dict[str, Any]] = []

    for post_id, context in snapshot:
        if post_id in allowlist:
            admitted.append((post_id, context))
            allowlisted_ids.append(post_id)
            admission_reasons.append({"post_id": post_id, "decision": "admitted", "reason": "explicit_allowlist"})
            continue
        if cohort_error:
            blocked_ids.append(post_id)
            admission_reasons.append({"post_id": post_id, "decision": "blocked", "reason": "invalid_historical_cohort"})
            continue
        if cutoff is None:
            blocked_ids.append(post_id)
            admission_reasons.append({"post_id": post_id, "decision": "blocked", "reason": "cutoff_missing"})
            continue
        frozen = (historical_cohort or {}).get(post_id)
        post_date = frozen.get("original_datetime") if frozen else _original_post_datetime(context)
        if frozen and frozen.get("classification") == "historical":
            historical_ids.append(post_id)
            admission_reasons.append({"post_id": post_id, "decision": "excluded", "reason": "frozen_historical_cohort"})
        elif post_date is None:
            missing_date_ids.append(post_id)
            admission_reasons.append({"post_id": post_id, "decision": "blocked", "reason": "date_missing"})
            continue
        elif post_date >= cutoff:
            admitted.append((post_id, context))
            reason = "frozen_cohort" if frozen else "wordpress_date"
            admission_reasons.append({"post_id": post_id, "decision": "admitted", "reason": reason})
        else:
            historical_ids.append(post_id)
            reason = "frozen_historical_cohort" if frozen else "wordpress_date_before_cutoff"
            admission_reasons.append({"post_id": post_id, "decision": "excluded", "reason": reason})

    return admitted, {
        "admission_configured": cutoff is not None,
        "admission_after": cutoff.isoformat() if cutoff is not None else None,
        "historical_excluded": len(historical_ids),
        "historical_excluded_ids": historical_ids,
        "historical_allowlisted": len(allowlisted_ids),
        "historical_allowlisted_ids": allowlisted_ids,
        "admission_blocked": len(blocked_ids),
        "admission_blocked_ids": blocked_ids,
        "admission_missing_date": len(missing_date_ids),
        "admission_missing_date_ids": missing_date_ids,
        "admission_reasons": admission_reasons,
        "historical_cohort_configured": (
            bool(historical_cohort) or bool(cohort_error)
            if cohort_configured is None
            else bool(cohort_configured)
        ),
        "historical_cohort_error": cohort_error,
    }


def _write_v2_media_checkpoint(root: Path, post_id: int, media: MediaProgress) -> None:
    """Persist only the reconciled V2 media state for crash recovery."""
    target = Path(root) / "backups" / str(post_id) / "editorial.partial.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(media.to_dict(), ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    temporary.replace(target)


def _write_v2_journal_checkpoint(
    root: Path, post_id: int, status: str, state, *, run_id: str | None = None
) -> None:
    target = Path(root) / "work" / "v2-journal" / f"{post_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "status": status,
                "post_id": post_id,
                "state": state.to_dict(),
                **({"run_id": run_id} if run_id else {}),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _normalize_executable_candidate(candidate: dict[str, Any], subject: str) -> dict[str, Any]:
    result = dict(candidate)
    source = str(result.get("source_page_url") or "").strip()
    host = (urlparse(source).hostname or "").strip()
    result["author"] = result.get("author") or result.get("publisher") or host or "Fonte original"
    result["license"] = result.get("license") or "Uso com crédito"
    result["license_url"] = result.get("license_url") or source
    result["captured_at"] = result.get("captured_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")
    result["credit_text"] = result.get("credit_text") or f"Crédito da imagem: {host or 'fonte original'}"
    result["alt_text"] = result.get("alt_text") or subject
    return result


class ProductionMediaResolver:
    """Reuse the existing media-resolve core and execute only the deficit."""
    def __init__(self, client, config, root: Path):
        self.client, self.config, self.root = client, config, Path(root)

    def _recover_existing_featured(
        self,
        context: dict[str, Any],
        editorial: dict[str, Any],
        previous: MediaProgress,
    ) -> FeaturedProgress:
        """Normalize a relevant WordPress featured into V2 media state."""
        if previous.featured.status is FeaturedStatus.VALID:
            return previous.featured
        if previous.featured.status is FeaturedStatus.VISION_REJECTED:
            return FeaturedProgress(FeaturedStatus.VISION_REJECTED, None, None)
        unavailable = FeaturedProgress(previous.featured.status, None, None)
        post = context.get("post") or {}
        existing_id = post.get("featured_media") if isinstance(post, dict) else None
        if not isinstance(existing_id, int) or existing_id <= 0:
            return unavailable
        try:
            from ..workflow import _normalize_existing_featured

            normalized_id = _normalize_existing_featured(
                self.client,
                self.config,
                post,
                editorial,
                root=self.root,
            )
        except Exception:
            return unavailable
        if not isinstance(normalized_id, int) or normalized_id <= 0:
            return unavailable
        media_url = ""
        try:
            media_url = str(self.client.get_media(normalized_id).get("source_url") or "")
        except Exception:
            pass
        return FeaturedProgress(FeaturedStatus.VALID, normalized_id, media_url)

    def __call__(self, context, state, editorial, previous):
        html = str(editorial.get("cleaned_html") or "")
        title = str(context.get("title") or "")
        focus_keyword = str((editorial.get("seo") or {}).get("focus_keyword") or "")
        total_required = _required_image_count_for_editorial(html, title=title)
        registry_media: list[dict[str, Any]] = []
        try:
            from ..media.registry import load_v2_media
            registry_media = load_v2_media(self.root, post_id=int(context["post_id"]))
        except Exception:
            registry_media = []
        current_inline = list(previous.inline)
        post_content_urls = set(_embedded_media_urls(str(((context.get("post") or {}).get("content") or {}).get("raw") or "")))
        used_slots = {item.slot for item in current_inline}
        known_ids = {item.media_id for item in current_inline}
        for row in registry_media:
            media_id = int(row.get("wordpress_media_id") or 0)
            media_url = _canonical_embedded_media_url(str(row.get("canonical_url") or ""))
            if (
                row.get("role") != "inline" or not media_id or media_id in known_ids
                or row.get("status") != "applied" or media_url not in post_content_urls
            ):
                continue
            try:
                attachment = self.client.get_media(media_id)
                attachment_url = _canonical_embedded_media_url(str((attachment or {}).get("source_url") or ""))
            except Exception:
                continue
            if attachment_url != media_url:
                continue
            slot = next((index for index, url in enumerate(_embedded_media_urls(
                str(((context.get("post") or {}).get("content") or {}).get("raw") or "")
            )) if url == media_url and index not in used_slots), len(current_inline))
            while slot in used_slots:
                slot += 1
            current_inline.append(InlineMedia(
                media_id, str(row.get("canonical_url") or ""), slot, subject=str(row.get("subject") or ""),
                section_slot=row.get("section_slot"), phash=str(row.get("phash") or ""),
                sha256=str(row.get("sha256") or ""), visual_group_id=str(row.get("visual_group_id") or ""),
                visual_verification=dict(row.get("visual_verification") or {}),
            ))
            known_ids.add(media_id)
            used_slots.add(slot)
        previous = replace(previous, inline=tuple(current_inline))
        registry_featured = next(
            (row for row in registry_media if row.get("role") == "featured" and row.get("wordpress_media_id")),
            None,
        )
        if registry_featured:
            try:
                attachment = self.client.get_media(int(registry_featured["wordpress_media_id"]))
                if _canonical_embedded_media_url(str((attachment or {}).get("source_url") or "")) != _canonical_embedded_media_url(str(registry_featured.get("canonical_url") or "")):
                    registry_featured = None
            except Exception:
                registry_featured = None
        featured = (
            FeaturedProgress(
                FeaturedStatus.VALID,
                int(registry_featured["wordpress_media_id"]),
                str(registry_featured.get("canonical_url") or ""),
                str(registry_featured.get("sha256") or ""),
                str(registry_featured.get("phash") or ""),
                str(registry_featured.get("visual_group_id") or ""),
                dict(registry_featured.get("visual_verification") or {}),
            )
            if registry_featured and previous.featured.status not in {FeaturedStatus.VALID, FeaturedStatus.VISION_REJECTED, FeaturedStatus.INVALID}
            and int((context.get("post") or {}).get("featured_media") or 0) == int(registry_featured["wordpress_media_id"])
            else self._recover_existing_featured(context, editorial, previous)
        )
        from .visual_media import materialize_media_identity

        recovered_media = materialize_media_identity(
            self.client,
            self.config,
            self.root,
            MediaProgress(
                required=total_required,
                inline=tuple(current_inline),
                featured=featured,
                search=previous.search,
                enrichment_round=previous.enrichment_round,
                waiver_applied=previous.waiver_applied,
                waiver_reason=previous.waiver_reason,
            ),
            post_id=int(context.get("post_id") or 0) or None,
            run_id=str(context.get("v2_run_id") or context.get("run_id") or ""),
        )
        current_inline = list(recovered_media.inline)
        featured = recovered_media.featured
        current_featured = featured

        def persist_progress() -> None:
            snapshot = replace(
                recovered_media, inline=tuple(current_inline), featured=current_featured,
                accepted_count=None,
            )
            # Publish in memory first: even an I/O error cannot roll back an
            # upload that has already been confirmed by WordPress.
            context["_v2_media_progress"] = snapshot
            _write_v2_media_checkpoint(self.root, int(context["post_id"]), snapshot)

        persist_progress()
        media_reconciliation = reconcile_working_visual_media(
            recovered_media,
            required_inline=total_required,
            featured=featured,
        )
        media_accounting = media_reconciliation.accounting
        valid_inline_ids = set(media_accounting.valid_inline_ids)

        def log_media_accounting(accounting, *, stage: str) -> None:
            try:
                from ..observability import append_telemetry

                append_telemetry(
                    self.root,
                    "v2_media_accounting",
                    post_id=int(context["post_id"]),
                    run_id=str(context.get("v2_run_id") or context.get("run_id") or ""),
                    stage=stage,
                    required=accounting.required_inline,
                    accepted_working=accounting.accepted_working_count,
                    valid_distinct=accounting.valid_distinct_inline_count,
                    duplicates_confirmed=accounting.confirmed_duplicate_count,
                    unverified=accounting.unverified_count,
                    effective_missing=accounting.effective_missing,
                    featured_status=accounting.featured_identity_status,
                    baseline_complete=accounting.baseline_complete,
                    valid_inline_ids=list(accounting.valid_inline_ids),
                    duplicate_inline_ids=list(accounting.duplicate_inline_ids),
                    unverified_inline_ids=list(accounting.unverified_inline_ids),
                )
            except Exception:  # noqa: BLE001 - observability is fail-soft
                pass

        log_media_accounting(media_accounting, stage="after_identity_recovery")
        featured_needed = 0 if featured.status is FeaturedStatus.VALID else 1
        current_featured = featured

        from ..cli import _resolve_media_batch

        subject_rows = post_subjects(
            title=title,
            content_html=html,
            focus_keyword=focus_keyword,
            game_name=editorial.get("game_name"),
            categories=editorial.get("categories") or context.get("categories") or (),
        )
        source_subjects = editorial_subjects(
            title=title,
            content_html=html,
            focus_keyword=focus_keyword,
            game_name=editorial.get("game_name"),
            categories=editorial.get("categories") or context.get("categories") or (),
        )
        from ..media.vision_policy import resolve_editorial_identity
        seo_identity = editorial.get("seo") if isinstance(editorial.get("seo"), dict) else {}
        vision_editorial = {
            **editorial,
            "title": title,
            "cleaned_html": html,
            "seo": {**seo_identity, "focus_keyword": focus_keyword},
        }
        identity = resolve_editorial_identity(vision_editorial)
        primary_subject = str(identity.get("primary_subject") or "").strip()
        explicit_focus = focus_keyword.strip().casefold() not in {
            "", "game", "games", "jogo", "jogos", "videogame", "anime", "filme", "series",
        }
        has_canonical_subject = bool(str(editorial.get("game_name") or "").strip() or explicit_focus)
        if primary_subject and has_canonical_subject:
            source_subjects = [primary_subject, *[
                str(value).strip()
                for value in identity.get("secondary_entities") or []
                if str(value).strip().casefold() != primary_subject.casefold()
            ]]
        is_listicle = detect_list_format(
            title,
            html,
            editorial.get("categories") or context.get("categories") or (),
        ) is not None or len(subject_rows) > 1
        if is_listicle:
            media_reconciliation = reconcile_working_visual_media(
                recovered_media,
                required_inline=total_required,
                featured=featured,
                listicle_items=subject_rows,
            )
            media_accounting = media_reconciliation.accounting
            valid_inline_ids = set(media_accounting.valid_inline_ids)
            log_media_accounting(media_accounting, stage="before_acquisition")
        try:
            from ..observability import append_telemetry

            append_telemetry(
                self.root,
                "v2_media_reconciliation",
                post_id=int(context["post_id"]),
                run_id=str(context.get("v2_run_id") or context.get("run_id") or ""),
                valid_inline_ids=list(media_accounting.valid_inline_ids),
                duplicate_inline_ids=list(media_accounting.duplicate_inline_ids),
                unverified_inline_ids=list(media_accounting.unverified_inline_ids),
                deficit_before_attachment_count=max(0, total_required - len(current_inline)),
                effective_missing=media_accounting.effective_missing,
                read_only=True,
            )
        except Exception:  # noqa: BLE001 - telemetry cannot change acquisition
            pass
        if not is_listicle and source_subjects:
            # Keep listicle coverage driven by its H2 subjects.  For ordinary
            # news, use the first concrete visual entity for discovery rather
            # than a grammatical phrase such as "ex-gerentes".
            chosen_subject = primary_subject if has_canonical_subject else source_subjects[0]
            subject_rows = [{
                "item": None,
                "heading": chosen_subject,
                "subject": chosen_subject,
            }]

        def add_query_context(base: str) -> str:
            if is_listicle:
                return base
            primary_subject = str((subject_rows[0] if subject_rows else {}).get("subject") or "")
            primary_tokens = set(primary_subject.casefold().split())
            ignored_context = {"review", "reviews", "sketch", "noticia", "news", "game", "jogo"}
            context_terms = [
                value for value in source_subjects[1:]
                if value
                and value.casefold() != primary_subject.casefold()
                and value.casefold() not in {"nintendo", "switch", "steam", "playstation", "xbox"}
                and value.casefold() not in ignored_context
                and not (
                    set(value.casefold().split()) - primary_tokens
                ).issubset(ignored_context)
            ]
            return " ".join(dict.fromkeys([base, *context_terms[:3]]))

        def contextual_query(subject: str, *, extra: str = "") -> str:
            return add_query_context(item_query(subject, title, extra=extra))
        covered_items = {
            str(item.item_identity or item.item_number)
            for item in current_inline
            if item.media_id in valid_inline_ids and (item.item_identity or item.item_number is not None)
        }
        covered_subjects = {
            item.subject.casefold().strip()
            for item in current_inline
            if item.media_id in valid_inline_ids and item.subject.strip()
        }
        missing_rows = [
            (index, row) for index, row in enumerate(subject_rows)
            if not is_listicle
            or (
                str(row.get("item_identity") or row.get("item")) not in covered_items
                and str(row.get("subject") or "").casefold().strip() not in covered_subjects
            )
        ]
        inline_needed = media_accounting.effective_missing
        if is_listicle:
            # A migrated listicle may have accepted media without item_number.
            # It must cover every item and still satisfy the word-count
            # minimum when that is larger than the item count.
            inline_needed = max(inline_needed, len(missing_rows))
        if inline_needed and not is_listicle:
            # Validate the current editorial structure before spending any
            # provider calls or uploads.  A structure failure is routed back
            # to EDITORIAL so the next attempt can reorganize the copy; this
            # never fabricates paragraphs or weakens the spacing policy.
            from ..media.inserter import MediaInsertionError, normal_media_urls_to_insert, plan_normal_media_insertions
            from .errors import StageError
            from .model import BlockerCode

            try:
                future_urls = [item.media_url for item in current_inline if item.media_id in valid_inline_ids]
                future_urls.extend(
                    f"__v2_pending_media_{index}__" for index in range(inline_needed)
                )
                insertions = normal_media_urls_to_insert(
                    html,
                    future_urls,
                    str(current_featured.media_url or ""),
                )
                plan_normal_media_insertions(html, [{"media_url": url} for url in insertions])
            except MediaInsertionError as exc:
                raise StageError(
                    BlockerCode.STRUCTURE,
                    Phase.EDITORIAL,
                    f"media_preflight:{exc.code}: {exc}",
                ) from exc
        if inline_needed == 0 and featured_needed == 0:
            log_media_accounting(media_accounting, stage="acquisition_not_needed")
            return MediaProgress(
                required=total_required,
                inline=tuple(current_inline),
                featured=current_featured,
                search=previous.search,
                enrichment_round=previous.enrichment_round,
                waiver_applied=previous.waiver_applied,
                waiver_reason=previous.waiver_reason,
            )
        rejection_history = _media_rejection_history(self.root, int(context["post_id"]))
        from ..media.vision_policy import featured_vision_context_key
        vision_context_key = featured_vision_context_key(vision_editorial)
        inline_suffix, inline_reason = _adaptive_query_suffix(rejection_history, role="inline")
        featured_suffix, featured_reason = _adaptive_query_suffix(rejection_history, role="featured")
        subject_queries: list[tuple[str, str]] = []
        seen_queries: set[str] = set()
        subject_meta: dict[str, dict[str, Any]] = {}

        def add_query(subject: str, query: str) -> None:
            subject = " ".join(str(subject or "").split()).strip()
            query = " ".join(str(query or "").split()).strip()
            if query and query.casefold() not in seen_queries:
                subject_queries.append((subject or query, query))
                seen_queries.add(query.casefold())

        for index, row in missing_rows:
            subject = str(row.get("subject") or "").strip()
            if subject:
                subject_meta[subject] = {**row, "section_slot": row.get("section_slot", index)}
            # Listicles diversify naturally by item. For a normal article with
            # one unresolved subject, vary the visual role instead of asking
            # engines for the same cover/key art on every retry.
            queries = (
                [add_query_context(query) for query in diverse_item_queries(subject, title, extra=focus_keyword)]
                if not is_listicle and len(missing_rows) == 1
                else [contextual_query(subject, extra=focus_keyword)]
            )
            for query in queries:
                add_query(subject, f"{query}{inline_suffix}")
        if focus_keyword and not is_listicle:
            main_subject = str((subject_rows[0] if subject_rows else {}).get("subject") or focus_keyword)
            add_query(main_subject, f"{contextual_query(focus_keyword)}{inline_suffix}")
        if not subject_queries and not is_listicle:
            add_query(title, f"{item_query(title, title, extra=focus_keyword)}{inline_suffix}")

        from ..observability import append_telemetry

        if inline_reason or featured_reason:
            append_telemetry(
                self.root, "media_adaptive_planning", post_id=int(context["post_id"]),
                inline_deficit=max(0, inline_needed), featured_deficit=featured_needed,
                inline_reason=inline_reason, featured_reason=featured_reason,
                prior_rejection_count=len(rejection_history["reasons"]),
                prior_candidate_urls=len(rejection_history["urls"]),
            )

        search_runs: list[dict[str, Any]] = []
        # One shared allowance for every query/plan of this post.  The visual
        # comparator itself increments per HTTP call (LOW and optional HIGH).
        visual_comparison_budget = [0]
        enrichment_round = (
            previous.enrichment_round + 1
            if getattr(state, "phase", None) is Phase.MEDIA
            else previous.enrichment_round
        )

        def resolve_query(subject: str, query: str, needed: int, role: str) -> list[dict[str, Any]]:
            if needed <= 0:
                return []
            digest = hashlib.sha1(query.encode("utf-8", "ignore")).hexdigest()[:10]
            batch = {
                "schema_version": 1,
                "batch_id": f"v2-{context['post_id']}-{role}-{digest}",
                "posts": [{
                    "post_id": int(context["post_id"]),
                    "subject": subject,
                    "query": query,
                    "needed": needed,
                    "limit": max(needed, 3),
                    "engine": "auto",
                    "size": "xga",
                    "ratio": "w",
                    "existing_media_urls": [item.media_url for item in current_inline],
                    "existing_media_phashes": {
                        item.media_url: item.phash
                        for item in current_inline
                        if item.phash
                    },
                }],
            }
            try:
                blocked_urls = {int(context["post_id"]): set(rejection_history["urls"])}
                blocked_fingerprints = {int(context["post_id"]): set(rejection_history["fingerprints"])}
                resolved = _resolve_media_batch(
                    self.client, self.config, self.root, batch, full=True, allow_reuse=True,
                    excluded_urls=blocked_urls,
                    excluded_fingerprints=blocked_fingerprints,
                )
            except Exception:
                from ..media.google_browser import cleanup_browser_artifacts

                cleanup_browser_artifacts()
                raise
            row = (resolved.get("posts") or [{}])[0]
            if role == "inline":
                search_run = row.get("search")
                search_run = dict(search_run) if isinstance(search_run, dict) else {
                    "completed": False,
                    "completion_reason": "SEARCH_INCOMPLETE",
                }
                search_run["_planned_query"] = query
                search_runs.append(search_run)
            candidates: list[dict[str, Any]] = []
            for reused in row.get("reuse") or []:
                if not isinstance(reused, dict):
                    continue
                candidates.append({
                    **reused,
                    "direct_image_url": reused.get("direct_image_url") or reused.get("url"),
                    "source_page_url": reused.get("source_page_url") or reused.get("source"),
                    "media_library_id": reused.get("media_library_id") or reused.get("media_id"),
                    "subject": subject,
                    "news_context": str(identity.get("news_context") or ""),
                    "search_query": query,
                    "role": role,
                    "evidence": reused.get("evidence") or {"verdict": "deterministic_match"},
                    "item_number": (subject_meta.get(subject) or {}).get("item"),
                    "item_identity": (subject_meta.get(subject) or {}).get("item_identity"),
                    "item_title": (subject_meta.get(subject) or {}).get("item_title") or subject,
                    "item_group": (subject_meta.get(subject) or {}).get("item_group") or "",
                    "html_position": (subject_meta.get(subject) or {}).get("html_position"),
                    "section_heading": (subject_meta.get(subject) or {}).get("heading") or subject,
                    "section_slot": (subject_meta.get(subject) or {}).get("section_slot"),
                })
            for candidate in row.get("audit_candidates") or []:
                if not isinstance(candidate, dict):
                    continue
                candidate = dict(candidate)
                candidate["subject"] = subject
                candidate["news_context"] = str(identity.get("news_context") or "")
                candidate["search_query"] = query
                candidate["role"] = role
                metadata = subject_meta.get(subject) or {}
                candidate["item_number"] = metadata.get("item")
                candidate["item_identity"] = metadata.get("item_identity")
                candidate["item_title"] = metadata.get("item_title") or subject
                candidate["item_group"] = metadata.get("item_group") or ""
                candidate["html_position"] = metadata.get("html_position")
                candidate["section_heading"] = metadata.get("heading") or subject
                candidate["section_slot"] = metadata.get("section_slot")
                candidates.append(candidate)
            filtered: list[dict[str, Any]] = []
            for candidate in candidates:
                candidate_url = _canonical_embedded_media_url(
                    str(candidate.get("direct_image_url") or candidate.get("media_url") or "")
                )
                if candidate_url and candidate_url in rejection_history["urls"]:
                    candidate["rejected_reason"] = "previously_rejected_candidate"
                    continue
                filtered.append(candidate)
            return filtered

        def accept_result(result: dict[str, Any], plan_item: dict[str, Any]) -> None:
            nonlocal current_featured, current_inline
            if result.get("status") not in {None, "accepted", "ok"}:
                return
            if not result.get("media_id") or not result.get("media_url"):
                return
            if result.get("featured"):
                current_featured = FeaturedProgress.from_dict({**result, "status": "valid"})
            else:
                media_id = int(result["media_id"])
                existing = next((item for item in current_inline if item.media_id == media_id), None)
                slot = existing.slot if existing else int(result.get("paragraph_index", 0))
                occupied = {item.slot for item in current_inline if item.media_id != media_id}
                while slot in occupied:
                    slot += 3
                item = InlineMedia.from_dict({**plan_item, **result, "slot": slot})
                current_inline = [item if old.media_id == media_id else old for old in current_inline]
                if existing is None:
                    current_inline.append(item)
            persist_progress()

        def execute_plan(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
            """Execute one candidate batch and merge only final accepted media."""
            nonlocal current_featured, current_inline
            if not plan:
                return []
            try:
                checked = validate_media_plan(
                    self.client,
                    {**editorial, "media_plan": plan},
                    config=self.config,
                    root=self.root,
                    post_title=title,
                    post_id=int(context["post_id"]),
                    existing_featured_id=(
                        current_featured.media_id
                        if current_featured.status is FeaturedStatus.VALID
                        else None
                    ),
                )
            except Exception:
                from ..media.google_browser import cleanup_browser_artifacts

                cleanup_browser_artifacts()
                raise
            vision_errors = [
                row for row in checked.get("featured_vision", [])
                if isinstance(row, dict) and row.get("technical")
            ]
            if vision_errors:
                from ..media.vision_gate import VisionGateError
                from ..media.google_browser import cleanup_browser_artifacts

                cleanup_browser_artifacts()
                raise VisionGateError(
                    str(vision_errors[0].get("reason") or "vision provider error")
                )
            try:
                baseline_inline_assets = [item.to_dict() for item in current_inline]
                baseline_featured_asset = (
                    current_featured.to_dict()
                    if current_featured.status is FeaturedStatus.VALID
                    and current_featured.media_id
                    and current_featured.media_url
                    else None
                )
                results, _featured_id, _featured_credit = _execute_media_plan(
                    {**editorial, "media_plan": plan},
                    self.config,
                    self.client,
                    self.root,
                    preflight=checked,
                    post_id=int(context["post_id"]),
                    post_title=title,
                    previous_inline_phashes=tuple(
                        item.phash for item in current_inline if item.phash
                    ),
                    previous_inline_visual_assets=baseline_inline_assets,
                    featured_visual_asset=baseline_featured_asset,
                    visual_comparison_budget=visual_comparison_budget,
                    on_accepted=lambda result: accept_result(result, plan[0]),
                )
            finally:
                from ..media.google_browser import cleanup_browser_artifacts

                cleanup_browser_artifacts()
            for result in results:
                accept_result(result, plan[0])
            try:
                from ..media.registry import record_v2_candidate_decision
                run_id = str(context.get("v2_run_id") or "")
                for index, result in enumerate(results):
                    item = plan[index] if index < len(plan) else plan[0]
                    vision_evidence = result.get("vision_evidence")
                    if isinstance(vision_evidence, dict):
                        _attach_candidate_vision_evidence(item, vision_evidence)
                    if result.get("source_comparison_proved") is True:
                        item["source_comparison_proved"] = True
                    status = str(result.get("status") or "").casefold()
                    detail = str(result.get("detail") or result.get("reason") or "")
                    explicit_reason = str(result.get("reason_code") or "").casefold().strip()
                    temporary = bool(result.get("recoverable")) or any(token in detail.casefold() for token in ("timeout", "tempor", "network", "cdn", "unreachable"))
                    definitive = any(token in detail.casefold() for token in (
                        "duplicate", "duplicat", "unrelated", "sem relação", "width_below_minimum",
                        "resolution", "resolu", "source_bytes_mismatch", "mismatch",
                    ))
                    accepted = status in {"accepted", "ok"}
                    reason_code = explicit_reason or (
                        "candidate_accepted" if accepted else
                        "transient_provider_failure" if temporary or not definitive else
                        "candidate_rejected"
                    )
                    record_v2_candidate_decision(
                        self.root, post_id=int(context["post_id"]), run_id=run_id,
                        candidate=item, role=str(item.get("role") or ("featured" if item.get("is_featured") else "inline")),
                        disposition="accepted" if accepted else "defer_retry" if temporary or not definitive else "reject_final",
                        reason_code=reason_code,
                    )
            except Exception as exc:
                # Registry writes are deliberately fail-soft, but a missing
                # decision must remain visible without logging candidate URLs,
                # payloads, or provider messages.
                try:
                    from ..observability import append_telemetry

                    append_telemetry(
                        self.root,
                        "v2_media_registry_decision_failed",
                        post_id=int(context["post_id"]),
                        run_id=str(context.get("v2_run_id") or ""),
                        operation="record_candidate_decision",
                        exception_type=type(exc).__name__,
                        candidate_count=len(results),
                    )
                except Exception:  # noqa: BLE001 - telemetry remains fail-soft
                    pass
            # Persist identity materialised for legacy accepted assets too.
            by_id = {int(row.get("media_id")): row for row in baseline_inline_assets if row.get("media_id")}
            current_inline = [
                item if not by_id.get(item.media_id) else InlineMedia(
                    item.media_id, item.media_url, item.slot, item.alt_text, item.credit_text,
                    item.subject, item.item_number, item.section_heading, item.section_slot,
                    item.width, item.height, str(by_id[item.media_id].get("phash") or item.phash),
                    str(by_id[item.media_id].get("sha256") or item.sha256),
                    str(by_id[item.media_id].get("visual_group_id") or item.visual_group_id),
                    dict(by_id[item.media_id].get("visual_verification") or item.visual_verification),
                    item.item_identity,
                    item.item_title,
                    item.item_group,
                    item.html_position,
                ) for item in current_inline
            ]
            if current_featured.status is FeaturedStatus.VALID and baseline_featured_asset is not None:
                current_featured = FeaturedProgress(
                    current_featured.status, current_featured.media_id, current_featured.media_url,
                    str(baseline_featured_asset.get("sha256") or current_featured.sha256),
                    str(baseline_featured_asset.get("phash") or current_featured.phash),
                    str(baseline_featured_asset.get("visual_group_id") or current_featured.visual_group_id),
                    dict(baseline_featured_asset.get("visual_verification") or current_featured.visual_verification),
                )
            # Crash-safe incremental progress: an accepted asset is durable
            # before the resolver moves on to the next candidate/query.
            persist_progress()
            return results

        def record_non_executable_candidates(
            candidates: list[dict[str, Any]],
            approved: list[dict[str, Any]],
            *,
            role: str,
        ) -> None:
            """Persist a terminal decision for candidates not sent to upload gates."""
            approved_ids = {
                str(item.get("candidate_id") or "")
                for item in approved
                if str(item.get("candidate_id") or "")
            }
            try:
                from ..media.registry import record_v2_candidate_decision

                for candidate in candidates:
                    candidate_id = str(candidate.get("candidate_id") or "")
                    if not candidate_id or candidate_id in approved_ids:
                        continue
                    evidence = candidate.get("evidence")
                    evidence = evidence if isinstance(evidence, dict) else {}
                    verdict = str(evidence.get("verdict") or "").casefold()
                    rejected_reason = str(candidate.get("rejected_reason") or "").casefold()
                    gate = str(evidence.get("gate") or "").casefold()
                    signal = rejected_reason or verdict or gate
                    definitive_signals = (
                        "source_mismatch", "duplicate", "unrelated", "width_below_minimum",
                        "resolution", "source_bytes_mismatch", "identity_mismatch",
                    )
                    definitive_verdicts = {
                        "reject", "source_mismatch", "duplicate_existing_frame",
                        "duplicate_frame", "unrelated",
                    }
                    is_definitive = verdict in definitive_verdicts or any(
                        value in signal for value in definitive_signals
                    )
                    disposition = "reject_final" if is_definitive else "defer_retry"
                    if verdict == "deterministic_match" and not rejected_reason:
                        # The evidence matched, but the candidate was not executable;
                        # missing contract/provenance data is not a permanent rejection.
                        disposition = "defer_retry"
                        signal = "incomplete_provenance"
                    elif not signal:
                        signal = "incomplete_provenance"
                    record_v2_candidate_decision(
                        self.root,
                        post_id=int(context["post_id"]),
                        run_id=str(context.get("v2_run_id") or context.get("run_id") or ""),
                        candidate=candidate,
                        role=role,
                        disposition=disposition,
                        reason_code=signal[:120],
                    )
            except Exception:  # noqa: BLE001 - registry audit must not change acquisition
                pass

        source_url = str(context.get("original_link") or "").strip()
        if not source_url:
            post = context.get("post") or {}
            source_url = (
                str((post.get("meta") or {}).get("original_link") or "").strip()
                if isinstance(post, dict)
                else ""
            )
        source_stats: dict[str, int] = {}
        source_candidates_cache: list[dict[str, Any]] | None = None

        def article_source_candidates() -> list[dict[str, Any]]:
            nonlocal source_candidates_cache
            if source_candidates_cache is not None:
                return source_candidates_cache
            source_candidates_cache = []
            if not source_url:
                return source_candidates_cache
            try:
                from ..media.source_verify import discover_article_source_candidates

                primary_subject = (
                    source_subjects[0]
                    if source_subjects
                    else (subject_queries[0][0] if subject_queries else title)
                )
                source_candidates_cache = discover_article_source_candidates(
                    source_url,
                    subject=primary_subject,
                    subjects=source_subjects,
                    limit=max(8, max(inline_needed, featured_needed) * 4),
                    stats=source_stats,
                )
            except Exception as exc:
                from ..observability import record_unexpected_error, append_telemetry

                record_unexpected_error(
                    self.root, exc, post_id=int(context["post_id"]),
                    run_id=str(context.get("v2_run_id") or context.get("run_id") or ""),
                    file="pipeline_v2/runtime.py", field="source_discovery",
                )
                append_telemetry(
                    self.root, "media_source_discovery_failed",
                    post_id=int(context["post_id"]),
                    failure_kind="source_extractor_error",
                    candidates_found=int(source_stats.get("raw_assets") or 0),
                )
                source_candidates_cache = []
            return source_candidates_cache

        if featured_needed:
            featured_row = subject_rows[0] if subject_rows else {}
            featured_subject = str(featured_row.get("subject") or title)
            source_feature_candidates: list[dict[str, Any]] = []
            discovered_source_candidates = article_source_candidates()
            for raw_candidate in discovered_source_candidates:
                candidate = dict(raw_candidate)
                if current_featured.status is FeaturedStatus.VALID:
                    break
                candidate["subject"] = featured_subject
                candidate["news_context"] = str(identity.get("news_context") or "")
                candidate["role"] = "featured"
                candidate["search_query"] = "article_source featured"
                candidate_url = _canonical_embedded_media_url(
                    str(candidate.get("direct_image_url") or "")
                )
                if candidate_url and candidate_url in rejection_history["urls"]:
                    continue
                try:
                    from ..media.library_index import find_by_source_url

                    existing = find_by_source_url(
                        self.root, str(candidate.get("direct_image_url") or "")
                    )
                    if existing and existing.get("media_id"):
                        candidate["media_library_id"] = int(existing["media_id"])
                        candidate["already_in_library"] = True
                except Exception:
                    pass
                source_feature_candidates.append(candidate)
            self._resolve_ambiguous_vision(
                source_feature_candidates, post_id=int(context["post_id"]),
                context_key=vision_context_key,
            )
            approved_source_features = self._approved_unique(source_feature_candidates)
            record_non_executable_candidates(
                source_feature_candidates, approved_source_features, role="featured"
            )
            for candidate in approved_source_features:
                if current_featured.status is FeaturedStatus.VALID:
                    break
                execute_plan([self._plan_item(candidate, featured_subject, 0, True)])
            append_telemetry(
                self.root,
                "v2_featured_source_discovery",
                post_id=int(context["post_id"]),
                run_id=str(context.get("v2_run_id") or context.get("run_id") or ""),
                candidates_found=len(discovered_source_candidates),
                candidates_considered=len(source_feature_candidates),
                candidates_executable=len(approved_source_features),
                outcome=(
                    "accepted_from_source" if current_featured.status is FeaturedStatus.VALID
                    else "source_exhausted"
                ),
            )

        featured_queries: list[tuple[str, str]] = []
        if featured_needed:
            featured_row = subject_rows[0] if subject_rows else {}
            featured_subject = str(featured_row.get("subject") or title)
            featured_query = contextual_query(featured_subject, extra=focus_keyword)
            featured_queries = [
                (featured_subject, f"{featured_query} key art{featured_suffix}"),
                (featured_subject, f"{featured_query}{featured_suffix}"),
            ]
            for subject, query in featured_queries:
                if current_featured.status is FeaturedStatus.VALID:
                    break
                candidates = resolve_query(subject, query, 1, "featured")
                self._resolve_ambiguous_vision(
                    candidates, post_id=int(context["post_id"]), context_key=vision_context_key
                )
                approved_candidates = self._approved_unique(candidates)
                record_non_executable_candidates(
                    candidates, approved_candidates, role="featured"
                )
                for candidate in approved_candidates:
                    if current_featured.status is FeaturedStatus.VALID:
                        break
                    execute_plan([
                        self._plan_item(candidate, candidate.get("subject") or title, 0, True)
                    ])

        covered_item_keys = {
            str(item.item_identity or item.item_number or item.subject).casefold()
            for item in current_inline
            if item.media_id in valid_inline_ids and (item.item_number is not None or item.subject)
        }
        # Historical rejected media stays auditable in the working set; its
        # slots remain occupied until COMPOSE builds the eligible projection.
        used_slots = {item.slot for item in current_inline}

        def inline_remaining() -> int:
            current_accounting = account_visual_media(
                current_inline,
                required_inline=total_required,
                featured=current_featured,
                listicle_items=subject_rows if is_listicle else (),
            )
            if is_listicle:
                return current_accounting.effective_missing
            return current_accounting.effective_missing

        inline_queries_attempted: list[str] = []
        if inline_needed:
            # The article's own Fonte is the first acquisition strategy.  Its
            # assets already have provenance by construction; they still pass
            # through the regular executable media-plan gates below.
            if source_url:
                source_attempted = 0
                source_accepted = 0
                source_reused = 0
                source_candidates = article_source_candidates()
                for candidate in source_candidates:
                    if inline_remaining() <= 0:
                        break
                    candidate_url = _canonical_embedded_media_url(str(candidate.get("direct_image_url") or ""))
                    if candidate_url and candidate_url in rejection_history["urls"]:
                        candidate["rejected_reason"] = "previously_rejected_candidate"
                        continue
                    source_attempted += 1
                    subject = str(candidate.get("subject") or (source_subjects[0] if source_subjects else (subject_queries[0][0] if subject_queries else title)))
                    candidate["subject"] = subject
                    metadata = subject_meta.get(subject) or {}
                    candidate["item_number"] = metadata.get("item")
                    candidate["item_identity"] = metadata.get("item_identity")
                    candidate["item_title"] = metadata.get("item_title") or subject
                    candidate["item_group"] = metadata.get("item_group") or ""
                    candidate["html_position"] = metadata.get("html_position")
                    candidate["section_heading"] = metadata.get("heading") or subject
                    candidate["section_slot"] = metadata.get("section_slot")
                    try:
                        from ..media.library_index import find_by_source_url

                        existing = find_by_source_url(self.root, str(candidate.get("direct_image_url") or ""))
                        if existing and existing.get("media_id"):
                            candidate["media_library_id"] = int(existing["media_id"])
                            candidate["already_in_library"] = True
                    except Exception:
                        # The library index is an optimization; source
                        # acquisition remains correct when it is unavailable.
                        pass
                    approved = self._approved_unique([candidate])
                    for source_candidate in approved:
                        if inline_remaining() <= 0:
                            break
                        slot = 0
                        while slot in used_slots:
                            slot += 3
                        before = len(current_inline)
                        execute_plan([
                            self._plan_item(source_candidate, subject, slot, False)
                        ])
                        if len(current_inline) > before:
                            source_accepted += 1
                            if source_candidate.get("media_library_id"):
                                source_reused += 1
                            current_valid_ids = set(account_visual_media(
                                current_inline,
                                required_inline=total_required,
                                featured=current_featured,
                                listicle_items=subject_rows if is_listicle else (),
                            ).valid_inline_ids)
                            if current_inline[-1].media_id in current_valid_ids:
                                used_slots.add(slot)
                                covered_item_keys.add(str(subject).casefold())
                from ..observability import append_telemetry

                append_telemetry(
                    self.root,
                    "media_source_summary",
                    post_id=int(context["post_id"]),
                    source_page=source_url[:500],
                    source_raw_found=int(source_stats.get("raw_assets") or 0),
                    source_after_dom_filter=int(source_stats.get("editorial_assets") or 0),
                    source_filtered=int(source_stats.get("filtered_assets") or 0),
                    source_attempted=source_attempted,
                    source_accepted=source_accepted,
                    reused=source_reused,
                    uploaded=max(0, source_accepted - source_reused),
                )
            # External acquisition is only used for the residual deficit.
            for subject, query in subject_queries:
                remaining = inline_remaining()
                if remaining <= 0:
                    break
                inline_queries_attempted.append(query)
                candidates = resolve_query(
                    subject,
                    query,
                    1 if is_listicle else remaining,
                    "inline",
                )
                self._resolve_ambiguous_vision(
                    candidates, post_id=int(context["post_id"]), context_key=vision_context_key
                )
                for candidate in self._approved_unique(candidates):
                    if inline_remaining() <= 0:
                        break
                    candidate_key = str(
                        candidate.get("item_identity")
                        or candidate.get("item_number")
                        or candidate.get("subject")
                        or ""
                    ).casefold()
                    if is_listicle and candidate_key in covered_item_keys:
                        continue
                    media_key = str(
                        candidate.get("media_library_id")
                        or candidate.get("direct_image_url")
                        or ""
                    )
                    if media_key in {
                        str(item.media_id) for item in current_inline
                    }:
                        continue
                    if is_listicle:
                        slot = int(candidate.get("section_slot", 0))
                    else:
                        slot = 0
                        while slot in used_slots:
                            slot += 3
                    before = len(current_inline)
                    execute_plan([
                        self._plan_item(candidate, candidate.get("subject") or title, slot, False)
                    ])
                    if len(current_inline) > before and current_inline[-1].media_id in set(
                        account_visual_media(
                            current_inline,
                            required_inline=total_required,
                            featured=current_featured,
                            listicle_items=subject_rows if is_listicle else (),
                        ).valid_inline_ids
                    ):
                        covered_item_keys.add(candidate_key)
                        if not is_listicle:
                            used_slots.add(slot)
        if search_runs:
            capacity_reached = inline_remaining() <= 0
            queries_planned = len(search_runs) if capacity_reached else len(subject_queries)
            queries_attempted = len(search_runs)
            queries_completed = sum(
                run.get("completed") is True for run in search_runs
            )
            completion_reasons = [
                str(run.get("completion_reason") or "")
                for run in search_runs
                if str(run.get("completion_reason") or "")
            ]
            if completion_reasons and all(reason == "TARGET_REACHED" for reason in completion_reasons):
                completion_reason = "TARGET_REACHED"
            elif any(reason == "PROVIDER_ERROR" for reason in completion_reasons):
                completion_reason = "PROVIDER_ERROR"
            elif completion_reasons and all(
                reason in {"EXHAUSTED_VERIFIED", "EXHAUSTED"}
                for reason in completion_reasons
            ):
                # Preserve legacy EXHAUSTED in persisted history, but only the
                # new verified value can authorize a waiver downstream.
                completion_reason = (
                    "EXHAUSTED_VERIFIED"
                    if all(reason == "EXHAUSTED_VERIFIED" for reason in completion_reasons)
                    else "EXHAUSTED"
                )
            elif any(reason == "SEARCH_INCOMPLETE" for reason in completion_reasons):
                completion_reason = "SEARCH_INCOMPLETE"
            else:
                completion_reason = "INTERRUPTED" if completion_reasons else ""
            search_completed = bool(
                queries_planned > 0
                and queries_attempted >= queries_planned
                and queries_completed == queries_planned
            )
            previous_search = previous.search
            active_coverage, coverage_runs, coverage_complete = _aggregate_active_coverage(
                search_runs,
                inline_queries_attempted,
            )
            previous_coverage = previous_search.coverage if isinstance(previous_search.coverage, dict) else {}
            previous_history = list(previous_coverage.get("attempt_history") or [])
            if not previous_history:
                previous_history = list(previous_coverage.get("runs") or [])
            attempt_history = [*previous_history, *coverage_runs][-20:]
            if not search_runs:
                active_coverage = dict(previous_coverage.get("active_coverage") or {})
            final_distinct = {
                item_id for item_id in account_visual_media(
                    current_inline,
                    required_inline=total_required,
                    featured=current_featured,
                    listicle_items=subject_rows if is_listicle else (),
                ).valid_inline_ids
            }
            completed_reason_set = {"EXHAUSTED_VERIFIED", "EXHAUSTED", "TARGET_REACHED"}
            target_reached_with_complete_execution = bool(
                search_completed
                and any(run.get("completion_reason") == "TARGET_REACHED" for run in search_runs)
                and all(run.get("completion_reason") in completed_reason_set for run in search_runs)
                and all(run.get("completed") is True for run in search_runs)
                and len(final_distinct) >= total_required
            )
            if target_reached_with_complete_execution:
                # Keep target attainment distinct from exhaustion proof. All
                # queries completed and the final visual quota is satisfied,
                # but incomplete coverage still cannot authorize a waiver.
                search_completed = True
                completion_reason = "TARGET_REACHED"
            if not coverage_complete and search_runs and completion_reason != "TARGET_REACHED":
                search_completed = False
                completion_reason = "SEARCH_INCOMPLETE"
            coverage = {
                "version": 1,
                "attempt_history": attempt_history,
                "active_coverage": active_coverage,
                "verified": active_coverage.get("verified") is True,
            }
            search_progress = MediaSearchProgress(
                completed=search_completed,
                # Exhaustion is finalized below, after the executable media
                # plan has produced its final WebP pHashes.  Discovery-level
                # candidates may collapse into duplicates during conversion.
                exhausted=False,
                queries_attempted=previous_search.queries_attempted + queries_attempted,
                engines_attempted=tuple(dict.fromkeys(
                    [*previous_search.engines_attempted,
                     *(
                         str(engine)
                         for run in search_runs
                         for engine in (run.get("engines_attempted") or [])
                     )]
                )),
                engines_disabled=tuple(dict.fromkeys(
                    [*previous_search.engines_disabled,
                     *(
                         str(engine)
                         for run in search_runs
                         for engine in (run.get("engines_disabled") or [])
                     )]
                )),
                candidates_seen=previous_search.candidates_seen + sum(
                    int(run.get("candidates_seen") or 0) for run in search_runs
                ),
                candidates_rejected=previous_search.candidates_rejected + sum(
                    int(run.get("candidates_rejected") or 0) for run in search_runs
                ),
                distinct_valid_frames=0,
                queries_planned=previous_search.queries_planned + queries_planned,
                queries_completed=previous_search.queries_completed + queries_completed,
                completion_reason=completion_reason or previous_search.completion_reason,
                coverage=coverage,
            )
            search_progress = replace(
                search_progress,
                exhausted=bool(
                    search_completed
                    and completion_reason != "TARGET_REACHED"
                    and len(final_distinct) < total_required
                ),
                distinct_valid_frames=len(final_distinct),
            )
        else:
            search_progress = previous.search
        final_accounting = account_visual_media(
            current_inline,
            required_inline=total_required,
            featured=current_featured,
            listicle_items=subject_rows if is_listicle else (),
        )
        log_media_accounting(final_accounting, stage="after_acquisition")
        return MediaProgress(
            required=total_required,
            inline=tuple({item.media_id: item for item in current_inline}.values()),
            featured=current_featured,
            search=search_progress,
            enrichment_round=enrichment_round,
        )

    @staticmethod
    def _has_provenance(candidate: dict[str, Any]) -> bool:
        return all(candidate.get(key) for key in (
            "source_page_url", "direct_image_url", "author", "license",
            "license_url", "captured_at", "credit_text", "alt_text",
        ))

    @classmethod
    def _is_approved(cls, candidate: dict[str, Any]) -> bool:
        return (
            (candidate.get("evidence") or {}).get("verdict") == "deterministic_match"
            and cls._has_provenance(candidate)
        )

    @classmethod
    def _approved_unique(cls, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in candidates:
            candidate = _normalize_executable_candidate(candidate, str(candidate.get("subject") or ""))
            key = str(candidate.get("media_library_id") or candidate.get("direct_image_url") or "")
            if key and key not in seen and cls._is_approved(candidate):
                seen.add(key)
                result.append(candidate)
        return result

    @staticmethod
    def _unique_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate.get("media_library_id") or candidate.get("direct_image_url") or "")
            if key and key not in seen:
                seen.add(key)
                result.append(candidate)
        return result

    def _resolve_ambiguous_vision(
        self,
        candidates: list[dict[str, Any]],
        *,
        post_id: int | None = None,
        context_key: str = "",
    ) -> None:
        vision_candidates = []
        for index, candidate in enumerate(candidates):
            url = candidate.get("direct_image_url") or candidate.get("image_url")
            verdict = (candidate.get("evidence") or {}).get("verdict")
            if url and verdict in {
                "ambiguous", "insufficient_source_context", "needs_vision", "inconclusive",
            }:
                vision_candidates.append({
                    "candidate_id": str(candidate.get("candidate_id") or f"v2-{index}"),
                    "source_image_url": str(url),
                    "subject": str(candidate.get("subject") or ""),
                    "context": str(candidate.get("news_context") or ""),
                    "require_key_art": bool(candidate.get("role") == "featured"),
                })
        if not vision_candidates or not getattr(self.config, "vision_enabled", False) or not getattr(self.config, "vision_api_key", ""):
            return
        from ..media.vision_gate import (
            VisionInputUnavailable,
            prepare_vision_image_input,
            verify_image_subject_batch,
        )
        from ..observability import append_telemetry

        prepared_candidates = []
        for item in vision_candidates:
            try:
                vision_input = prepare_vision_image_input(
                    item["source_image_url"],
                    timeout=self.config.http_timeout,
                    url_policy=getattr(self.config, "remote_url_policy", "audit"),
                )
            except VisionInputUnavailable as exc:
                candidate = next(
                    (
                        value for value in candidates
                        if str(value.get("candidate_id") or "") == item["candidate_id"]
                    ),
                    None,
                )
                if candidate is not None:
                    candidate["rejected_reason"] = "vision_input_unavailable"
                    candidate["evidence"] = {
                        **(candidate.get("evidence") or {}),
                        "score": 0,
                        "local_score": 0,
                        "gate": "vision_input",
                        "verdict": "vision_input_unavailable",
                        "needs_vision": False,
                        "reason": "imagem indisponível para validação visual",
                    }
                    candidate["evidence_score"] = 0
                    candidate["needs_vision"] = False
                append_telemetry(
                    self.root,
                    "media_vision_input_unavailable",
                    post_id=post_id,
                    candidate_id=item["candidate_id"],
                    source_image_url=item["source_image_url"],
                    reason_code="vision_input_unavailable",
                    detail=str(exc)[:200],
                )
                continue
            prepared_candidates.append({
                **item,
                "image_url": vision_input,
            })

        if not prepared_candidates:
            return

        budget = max(0, int(getattr(self.config, "vision_max_low", 0)))
        low_items = prepared_candidates[:budget]
        skipped = max(0, len(prepared_candidates) - len(low_items))
        if skipped:
            append_telemetry(
                self.root,
                "vision_budget_exhausted",
                candidates=len(vision_candidates),
                low_budget=budget,
                skipped=skipped,
            )
        if not low_items:
            return

        low_decisions: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(low_items), 20):
            low_decisions.update(verify_image_subject_batch(
                items=low_items[offset:offset + 20],
                api_key=self.config.vision_api_key,
                base_url=self.config.vision_base_url,
                model=self.config.vision_model,
                timeout=self.config.http_timeout,
                detail=self.config.vision_detail,
                allow_high=False,
                root=self.root,
            ))
        high_items = [
            item for item in low_items
            if item.get("require_key_art")
            and low_decisions.get(item["candidate_id"], {}).get("verdict") == "inconclusive"
        ]
        high_decisions: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(high_items), 20):
            high_decisions.update(verify_image_subject_batch(
                items=high_items[offset:offset + 20],
                api_key=self.config.vision_api_key,
                base_url=self.config.vision_base_url,
                model=self.config.vision_model,
                timeout=self.config.http_timeout,
                detail="high",
                allow_high=False,
                root=self.root,
            ))
        from ..media.vision_cache import set_cached_decision, vision_model_identity
        from ..media.vision_policy import VISION_POLICY_VERSION
        from ..media.vision_gate import image_input_sha256
        by_candidate_id = {str(item["candidate_id"]): item for item in low_items}
        for candidate in candidates:
            source_url = str(candidate.get("direct_image_url") or "")
            item = by_candidate_id.get(str(candidate.get("candidate_id") or ""))
            decision = low_decisions.get(item["candidate_id"]) if item else None
            if item and item["candidate_id"] in high_decisions:
                decision = high_decisions[item["candidate_id"]]
            if decision:
                image_sha256 = image_input_sha256(str(item.get("image_url") or ""))
                decision_record = {
                    key: decision[key]
                    for key in (
                        "status", "confidence", "visual_type", "relation", "editorial_fit",
                        "role_fit", "dimensions_source", "verdict", "reason",
                        "detail_used", "low_status", "low_confidence", "low_visual_type",
                    )
                    if key in decision
                }
                decision_record.update({
                    "image_sha256": image_sha256,
                    "image_role": "featured" if candidate.get("role") == "featured" else "inline",
                    "policy_version": VISION_POLICY_VERSION,
                    "decision_source": "vision_provider_batch",
                    "model_identity": vision_model_identity(self.config.vision_base_url, self.config.vision_model),
                    "context_key": context_key,
                    "subject": str(candidate.get("subject") or ""),
                })
                evidence = candidate.get("evidence")
                if not isinstance(evidence, dict):
                    evidence = {}
                    candidate["evidence"] = evidence
                evidence.update({
                    "vision_decision": decision_record,
                    "vision_image_sha256": image_sha256,
                    "vision_policy_version": VISION_POLICY_VERSION,
                })
                if decision.get("verdict") == "accept" and image_sha256:
                    set_cached_decision(
                        self.root,
                        source_url,
                        str(candidate.get("subject") or ""),
                        decision_record,
                        image_sha256=image_sha256,
                        policy=(
                            "featured-key-art-v1"
                            if candidate.get("role") == "featured"
                            else "subject-match-v1"
                        ),
                        model_identity=vision_model_identity(self.config.vision_base_url, self.config.vision_model),
                        context_key=context_key,
                    )
                if decision.get("verdict") == "accept":
                    evidence.update({"verdict": "deterministic_match"})
                    candidate["needs_vision"] = False
                elif decision.get("status") == "UNRELATED" and decision.get("verdict") == "reject":
                    evidence["verdict"] = "unrelated"
                    candidate["rejected_reason"] = "UNRELATED visual policy decision"

    @staticmethod
    def _plan_item(candidate: dict[str, Any], subject: str, slot: int, featured: bool) -> dict[str, Any]:
        return {
            **candidate,
            "paragraph_index": slot,
            "is_featured": featured,
            "alt_text": candidate.get("alt_text") or subject,
            "credit_text": candidate.get("credit_text") or f"Crédito da imagem: {subject}",
            "width": 1200,
            "height": 800,
            "item_identity": candidate.get("item_identity") or "",
            "item_title": candidate.get("item_title") or subject,
            "item_group": candidate.get("item_group") or candidate.get("group_heading") or "",
            "html_position": candidate.get("html_position"),
        }


class WordPressWriterV2:
    def __init__(self, client, root: Path, *, policy_version: int = 1):
        self.client, self.root, self.policy_version = client, Path(root), policy_version

    def commit(self, post_id: int, context: dict[str, Any], proposed_state, outcome):
        journal_dir = self.root / "work" / "v2-journal"
        journal_dir.mkdir(parents=True, exist_ok=True)
        candidate_path = self.root / "backups" / str(post_id) / "editorial.candidate.json"
        candidate = json.loads(candidate_path.read_text(encoding="utf-8")) if candidate_path.is_file() else {}
        expected_run_id = str(context.get("v2_run_id") or "").strip()
        requires_fresh_candidate = bool(context.get("candidate_must_be_fresh"))
        candidate_run_id = str(candidate.get("_v2_run_id") or "").strip() if isinstance(candidate, dict) else ""
        candidate_fresh = not requires_fresh_candidate or (
            bool(expected_run_id) and candidate_run_id == expected_run_id
        )
        if not candidate_fresh:
            # A failed COMPOSE must never fall through to a candidate from a
            # previous attempt. Keep the lifecycle/state write available, but
            # make all derived editorial writes unavailable for this commit.
            candidate = {}
        content = str(candidate.get("content") or "")
        validation_path = candidate_path.with_name("editorial.validation.json")
        try:
            validation = json.loads(validation_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            validation = {}
        validation_fresh = isinstance(validation, dict) and validation_matches_candidate(
            validation, candidate, expected_run_id,
        )
        seo = candidate.get("seo") or {}
        post = context.get("post") or self.client.get_post(post_id)
        meta = post.get("meta") if isinstance(post.get("meta"), dict) else {}
        final_state = proposed_state
        state_json = json.dumps(proposed_state.to_dict(), ensure_ascii=False, separators=(",", ":"))
        payload_meta: dict[str, Any] = {"_hermes_work_state": state_json}
        ready_hash = ""
        if outcome.type.value == "ready" and content:
            seo_meta = build_meta({key: str(seo.get(key) or "") for key in ("title", "meta_description", "focus_keyword")})
            payload_meta.update(seo_meta)
            manifest = build_ready_manifest(post_id=post_id, content=content, featured_media=candidate.get("featured_media"), seo=seo, original_link=context.get("original_link"), editorial=candidate.get("editorial"), policy_version=self.policy_version)
            ready_hash = manifest_hash(manifest)
            payload_meta.update({"_hermes_ready_manifest": serialize_manifest(manifest), "_hermes_ready_hash": ready_hash})
            payload_meta.update(build_state_markers(STATE_READY, ready_hash=ready_hash, policy_version=self.policy_version))
            latest = self.root / "backups" / str(post_id) / "editorial.latest.json"
            latest.parent.mkdir(parents=True, exist_ok=True)
            latest.write_text(json.dumps(candidate.get("editorial") or {}, ensure_ascii=False, indent=2), encoding="utf-8")
        update: dict[str, Any] = {"meta": dict(payload_meta)}
        import html as _html

        candidate_title = str(candidate.get("title") or "").strip()
        current_title = str((post.get("title") or {}).get("raw") or "").strip()
        if outcome.type.value == "ready" and candidate_title and candidate_title != current_title:
            update["title"] = {"raw": candidate_title}

        expected_inline_urls = [
            str(item.media_url or "").strip()
            for item in getattr(
                MediaProgress.from_dict(candidate.get("media"))
                if outcome.type.value == "ready" and isinstance(candidate.get("media"), dict)
                else getattr(proposed_state, "media", None),
                "inline",
                (),
            )
            if str(item.media_url or "").strip()
        ]
        proposed_media = getattr(proposed_state, "media", None)
        candidate_media = (
            MediaProgress.from_dict(candidate.get("media"))
            if outcome.type.value == "ready" and isinstance(candidate.get("media"), dict)
            else proposed_media
        )
        candidate_media_present = outcome.type.value == "ready" and isinstance(candidate.get("media"), dict)
        if outcome.type.value == "ready" and content:
            _assert_candidate_media_contract(
                content,
                candidate_media if candidate_media_present else proposed_media,
                title=str(candidate.get("title") or context.get("title") or ""),
            )
        if candidate_media_present:
            proposed_inline_by_id = {
                int(item.media_id): item
                for item in getattr(proposed_media, "inline", ())
                if getattr(item, "media_id", None)
            }
            candidate_inline = tuple(getattr(candidate_media, "inline", ()))
            candidate_inline_ids = [int(item.media_id) for item in candidate_inline]
            if len(candidate_inline_ids) != len(set(candidate_inline_ids)):
                raise RuntimeError("approved candidate contains repeated inline media ids")
            if any(
                media_id not in proposed_inline_by_id
                or proposed_inline_by_id[media_id].to_dict() != item.to_dict()
                for media_id, item in zip(candidate_inline_ids, candidate_inline)
            ):
                raise RuntimeError("approved candidate media diverges from validated V2 state")
            candidate_accounting = account_visual_media(candidate_media)
            candidate_featured = getattr(candidate_media, "featured", None)
            duplicate_rows = [
                item for item in candidate_inline
                if str((item.visual_verification or {}).get("decision") or "")
                in {"SAME_IMAGE", "SAME_ART_CROP"}
            ]
            content_urls = _embedded_media_urls(content)
            final_content_url = content_urls[-1] if content_urls else ""
            listicle = detect_list_format(
                str(candidate.get("title") or context.get("title") or ""), content,
            ) is not None
            permitted_list_repeat = (
                listicle
                and len(duplicate_rows) == 1
                and getattr(candidate_featured, "media_id", None) is not None
                and str((duplicate_rows[0].visual_verification or {}).get("duplicate_of") or "")
                == str(candidate_featured.media_id)
                and _canonical_embedded_media_url(str(duplicate_rows[0].media_url or ""))
                == final_content_url
            )
            permitted_duplicate_ids = {duplicate_rows[0].media_id} if permitted_list_repeat else set()
            if (
                len(candidate_accounting.valid_inline_ids) + len(permitted_duplicate_ids)
                != len(candidate_inline_ids)
                or candidate_accounting.confirmed_duplicate_count != len(permitted_duplicate_ids)
                or candidate_accounting.unverified_count
            ):
                raise RuntimeError("approved candidate contains ineligible visual media")
            proposed_featured = getattr(proposed_media, "featured", None)
            if (
                getattr(candidate_featured, "media_id", None)
                != getattr(proposed_featured, "media_id", None)
                or getattr(candidate_featured, "media_id", None) != candidate.get("featured_media")
            ):
                raise RuntimeError("approved candidate featured media diverges from validated V2 state")
        media_ids = {
            int(item.media_id)
            for item in getattr(proposed_media, "inline", ())
            if getattr(item, "media_id", None)
        }
        proposed_featured = getattr(proposed_media, "featured", None)
        if getattr(proposed_featured, "media_id", None):
            media_ids.add(int(proposed_featured.media_id))
        media_ids = sorted(media_ids)
        media_records = [
            {
                "role": "inline",
                "media_id": int(item.media_id),
                "media_url": str(item.media_url or ""),
                "sha256": str(item.sha256 or ""),
                "phash": str(item.phash or ""),
                "visual_group_id": str(item.visual_group_id or ""),
                "visual_verification": dict(item.visual_verification or {}),
                "destination": "pending_apply",
            }
            for item in getattr(proposed_media, "inline", ())
            if getattr(item, "media_id", None)
        ]
        if getattr(proposed_featured, "media_id", None):
            media_records.insert(0, {
                "role": "featured",
                "media_id": int(proposed_featured.media_id),
                "media_url": str(proposed_featured.media_url or ""),
                "sha256": str(proposed_featured.sha256 or ""),
                "phash": str(proposed_featured.phash or ""),
                "visual_group_id": str(proposed_featured.visual_group_id or ""),
                "visual_verification": dict(proposed_featured.visual_verification or {}),
                "destination": "pending_apply",
            })
        applied_inline_ids = {
            int(item.media_id)
            for item in getattr(candidate_media, "inline", ())
            if getattr(item, "media_id", None)
        }
        applied_featured = getattr(candidate_media, "featured", None)
        applied_media_ids = set(applied_inline_ids)
        if getattr(applied_featured, "media_id", None):
            applied_media_ids.add(int(applied_featured.media_id))
        apply_media_records = [
            {
                "role": "inline",
                "media_id": int(item.media_id),
                "media_url": str(item.media_url or ""),
                "readback_status": "not_attempted",
                "destination": "apply_pending_readback",
            }
            for item in getattr(candidate_media, "inline", ())
            if getattr(item, "media_id", None)
        ]
        if getattr(applied_featured, "media_id", None):
            apply_media_records.insert(0, {
                "role": "featured",
                "media_id": int(applied_featured.media_id),
                "media_url": str(applied_featured.media_url or ""),
                "readback_status": "not_attempted",
                "destination": "apply_pending_readback",
            })
        applied_media_ids = set(applied_media_ids)
        will_apply_media = bool(content) and outcome.type.value == "ready"
        for record in media_records:
            if will_apply_media and int(record.get("media_id") or 0) in applied_media_ids:
                record["destination"] = "apply_pending_readback"
            elif str((record.get("visual_verification") or {}).get("decision") or "") in {
                "SAME_IMAGE", "SAME_ART_CROP",
            }:
                record["destination"] = "rejected_duplicate"
        media_state = "accepted_working" if media_ids else "apply_not_attempted"
        media_readback_status = "not_attempted"
        journal = journal_dir / f"{post_id}.json"
        intent = {
            "status": "prepared", "post_id": post_id,
            "run_id": expected_run_id,
            "cycle_id": str(context.get("v2_cycle_id") or ""),
            "cycle_round": context.get("v2_cycle_round"),
            "validation_fresh": validation_fresh,
            "state": proposed_state.to_dict(), "ready_hash": ready_hash,
            "candidate_hash": hashlib.sha256(content.encode()).hexdigest(),
            "candidate_fresh": candidate_fresh,
            "candidate_run_id": candidate_run_id or None,
            "detail": outcome.detail or context.get("provider_reason"),
            "media_ids": media_ids,
            "media_records": media_records,
            "apply_media_records": apply_media_records,
            "media_state": media_state,
            "media_readback_status": media_readback_status,
        }
        if not candidate_fresh and outcome.type.value == "ready":
            raise RuntimeError("stale editorial candidate cannot produce READY")
        journal.write_text(json.dumps(intent, ensure_ascii=False, indent=2), encoding="utf-8")
        # A partial candidate is a checkpoint, never public/editor-facing
        # content.  Persist only lifecycle/media state until the complete
        # candidate passes every final gate and reaches READY.
        apply_inline = bool(content) and outcome.type.value == "ready"
        if apply_inline:
            update["content"] = {"raw": content}
        if outcome.type.value == "ready" and content:
            update["featured_media"] = candidate.get("featured_media")
        current = post.get("content") or {}
        ready_two_phase = outcome.type.value == "ready" and bool(content)

        def assert_media_readback(readback_post: dict[str, Any]) -> None:
            nonlocal apply_media_records
            readback_content = _html.unescape(str((readback_post.get("content") or {}).get("raw") or ""))
            embedded_readback_urls = set(_embedded_media_urls(readback_content))
            missing_inline = [
                url for url in expected_inline_urls
                if _canonical_embedded_media_url(url) not in embedded_readback_urls
            ]
            if missing_inline:
                raise RuntimeError(
                    "inline media read-back mismatch: " + ", ".join(missing_inline[:5])
                )
            if not expected_inline_urls and embedded_readback_urls:
                raise RuntimeError(
                    "unexpected inline media in read-back without accepted V2 media"
                )
            for record in apply_media_records:
                role = str(record.get("role") or "")
                if role == "featured":
                    confirmed = readback_post.get("featured_media") == record.get("media_id")
                else:
                    confirmed = _canonical_embedded_media_url(str(record.get("media_url") or "")) in embedded_readback_urls
                record["readback_status"] = "confirmed" if confirmed else "failed"
                record["destination"] = (
                    "applied_readback_confirmed" if confirmed else "apply_readback_failed"
                )
                if not confirmed:
                    detail = f" for media_id={record.get('media_id')}"
                    if role == "featured":
                        raise RuntimeError("featured media read-back mismatch" + detail)
                    raise RuntimeError("inline media read-back mismatch" + detail)
            applied_ids = set(applied_media_ids)
            excluded_duplicate_urls = [
                str(record.get("media_url") or "")
                for record in media_records
                if record.get("role") == "inline"
                and int(record.get("media_id") or 0) not in applied_ids
                and str((record.get("visual_verification") or {}).get("decision") or "")
                in {"SAME_IMAGE", "SAME_ART_CROP"}
            ]
            embedded_canonical = {
                _canonical_embedded_media_url(url) for url in _embedded_media_urls(readback_content)
            }
            if any(
                _canonical_embedded_media_url(url) in embedded_canonical
                for url in excluded_duplicate_urls if url
            ):
                raise RuntimeError("confirmed duplicate media remained in WordPress read-back")
            expected_featured = getattr(getattr(proposed_state, "media", None), "featured", None)
            expected_featured_id = getattr(expected_featured, "media_id", None)
            if expected_featured_id is not None and readback_post.get("featured_media") != expected_featured_id:
                raise RuntimeError("featured media read-back mismatch")

        def persist_media_readback_failure(exc: Exception) -> None:
            """Keep an attempted-but-unconfirmed apply auditable and retryable."""
            for record in apply_media_records:
                if record.get("readback_status") != "confirmed":
                    record["readback_status"] = "failed"
                    record["destination"] = "apply_readback_failed"
            destinations = {
                int(record.get("media_id") or 0): str(record.get("destination") or "")
                for record in apply_media_records
            }
            for record in media_records:
                destination = destinations.get(int(record.get("media_id") or 0))
                if destination:
                    record["destination"] = destination
            try:
                journal.write_text(
                    json.dumps(
                        {
                            **intent,
                            "status": "media_readback_failed",
                            "media_state": "apply_readback_failed",
                            "media_readback_status": "failed",
                            "media_records": media_records,
                            "apply_media_records": apply_media_records,
                            "detail": str(exc)[:400],
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            except Exception:
                # The WordPress readback failure remains the primary error; a
                # local journal failure must not hide it.
                pass

        if ready_two_phase:
            # Keep the WordPress lifecycle pending while media is verified. If
            # the second metadata write fails, the post remains eligible for
            # the next V2 run instead of being stranded as READY.
            media_update: dict[str, Any] = {"content": {"raw": content}}
            if outcome.type.value == "ready":
                media_update["featured_media"] = candidate.get("featured_media")
            intent["media_state"] = "apply_started"
            journal.write_text(
                json.dumps({**intent, "status": "media_committing"}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            try:
                self.client.update_post(post_id, media_update)
                media_readback_post = self.client.get_post(post_id)
                if not isinstance(media_readback_post, dict):
                    raise RuntimeError("WordPress media read-back missing")
                assert_media_readback(media_readback_post)
            except Exception as exc:
                persist_media_readback_failure(exc)
                raise
            destinations = {
                int(record.get("media_id") or 0): str(record.get("destination") or "")
                for record in apply_media_records
            }
            for record in media_records:
                destination = destinations.get(int(record.get("media_id") or 0))
                if destination:
                    record["destination"] = destination
            if candidate_media_present:
                final_state = replace(
                    proposed_state,
                    media=replace(
                        proposed_media,
                        inline=tuple(candidate_media.inline),
                        featured=candidate_media.featured,
                        accepted_count=None,
                    ),
                )
                state_json = json.dumps(final_state.to_dict(), ensure_ascii=False, separators=(",", ":"))
                payload_meta["_hermes_work_state"] = state_json
                update["meta"] = dict(payload_meta)
                intent["state"] = final_state.to_dict()
            journal.write_text(
                json.dumps({**intent, "status": "committing", "media_state": "inline_readback_confirmed", "media_readback_status": "confirmed", "apply_media_records": apply_media_records}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.client.update_post(post_id, update)
            readback = self.client.get_post(post_id)
            changed = True
        elif (
            (not apply_inline or update.get("content", {}).get("raw") == current.get("raw"))
            and "title" not in update
            and meta.get("_hermes_work_state") == state_json
        ):
            readback = post
            changed = False
        else:
            journal.write_text(
                json.dumps({**intent, "status": "committing"}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.client.update_post(post_id, update)
            readback = self.client.get_post(post_id)
            changed = True
        if not isinstance(readback, dict):
            raise RuntimeError("WordPress read-back missing")
        readback_meta = readback.get("meta") or {}
        if readback_meta.get("_hermes_work_state") != state_json:
            raise RuntimeError("V2 state read-back mismatch")
        if outcome.type.value == "human_required":
            # Keep the WP editor's manual-review queue synchronized with the
            # canonical V2 lifecycle.  The native WP status must remain
            # awaiting_human, otherwise a human moving it back to pending has
            # no durable queue marker and the UI hides the work.
            if readback.get("status") != "awaiting_human":
                self.client.move_to_status(post_id, "awaiting_human")
                readback = self.client.get_post(post_id)
            if readback.get("status") != "awaiting_human":
                raise RuntimeError("Awaiting Human status read-back mismatch")
        media_readback = False
        if apply_inline or outcome.type.value == "ready":
            try:
                assert_media_readback(readback)
            except Exception as exc:
                persist_media_readback_failure(exc)
                raise
            destinations = {
                int(record.get("media_id") or 0): str(record.get("destination") or "")
                for record in apply_media_records
            }
            for record in media_records:
                destination = destinations.get(int(record.get("media_id") or 0))
                if destination:
                    record["destination"] = destination
            readback_content = _html.unescape(str((readback.get("content") or {}).get("raw") or ""))
            embedded_readback_urls = set(_embedded_media_urls(readback_content))
            missing_inline = []
            expected_featured_id = getattr(getattr(proposed_state, "media", None), "featured", None)
            expected_featured_id = getattr(expected_featured_id, "media_id", None)
            featured_readback = (
                expected_featured_id is None
                or readback.get("featured_media") == expected_featured_id
            )
            media_readback = not missing_inline and featured_readback
            try:
                from ..observability import append_telemetry

                embedded_candidate_urls = set(_embedded_media_urls(_html.unescape(content)))
                append_telemetry(
                    self.root,
                    "media_apply_readback",
                    post_id=int(post_id),
                    accepted_media=len(expected_inline_urls),
                    inline_applied=sum(
                        _canonical_embedded_media_url(url)
                        in embedded_candidate_urls
                        for url in expected_inline_urls
                    ),
                    inline_readback=sum(
                        _canonical_embedded_media_url(url) in embedded_readback_urls
                        for url in expected_inline_urls
                    ),
                    readback=True,
                    partial=outcome.type.value != "ready",
                )
            except Exception as exc:
                from ..observability import record_unexpected_error

                record_unexpected_error(
                    self.root, exc, post_id=int(post_id),
                    run_id=str(context.get("v2_run_id") or context.get("run_id") or ""),
                    file="pipeline_v2/runtime.py", field="media_apply_readback",
                )
        if outcome.type.value == "ready":
            if (readback.get("content") or {}).get("raw") != content:
                raise RuntimeError("candidate content read-back mismatch")
            if readback_meta.get("_hermes_ready_hash") != ready_hash:
                raise RuntimeError("ready hash read-back mismatch")
        if outcome.type.value == "ready" and candidate_title:
            readback_title = str((readback.get("title") or {}).get("raw") or "").strip()
            if readback_title != candidate_title:
                raise RuntimeError("candidate title read-back mismatch")
        if outcome.type.value == "ready" and not media_readback:
            raise RuntimeError("media read-back mismatch: featured or inline media diverged")
        if media_readback:
            media_state = "applied_readback_confirmed"
            media_readback_status = "confirmed"
        journal.write_text(json.dumps({**intent, "status": "committed", "readback": True, "media_state": media_state, "media_readback_status": media_readback_status, "apply_media_records": apply_media_records}, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "wordpress_changed": changed,
            "readback": True,
            "state_readback": True,
            "media_readback": media_readback,
            "media_readback_status": media_readback_status,
            "media_state": media_state,
            "media_ids": media_ids,
            "applied_media_ids": sorted(applied_media_ids) if media_readback else [],
            "applied_inline_ids": sorted(applied_inline_ids) if media_readback else [],
            "state": final_state.to_dict(),
            "ready_hash": ready_hash,
            "candidate_fresh": candidate_fresh,
        }


def run_v2(
    client,
    config,
    root: Path,
    *,
    limit: int = 1,
    post_id: int | None = None,
    preview: bool = False,
    expected_selected_ids: list[int] | None = None,
    retry_post_ids: Collection[int] = (),
    excluded_post_ids: Collection[int] = (),
    max_new_posts: int | None = None,
    lock_held: bool = False,
    cycle_id: str | None = None,
    cycle_round: int | None = None,
) -> dict[str, Any]:
    if limit < 1:
        raise ValueError("limit must be positive")
    if max_new_posts is not None and max_new_posts < 0:
        raise ValueError("max_new_posts must not be negative")
    lock = None if lock_held else RunSessionLock(Path(root) / "work" / "v2-run.lock")
    if lock is not None and not lock.acquire():
        return {"selected": 0, "locked": True, "details": []}
    try:
        reader = ProductionCandidateReader(client, root)
        snapshot = reader.snapshot(page_size=max(10, limit))
        now = datetime.now(timezone.utc)
        cohort_path = getattr(config, "v2_historical_cohort_file", None)
        historical_cohort, cohort_error = load_historical_cohort(cohort_path)
        admitted, admission = admit_v2_candidates(
            snapshot,
            getattr(config, "v2_admission_after", None),
            getattr(config, "v2_admission_allowlist", ()),
            historical_cohort=historical_cohort,
            cohort_error=cohort_error,
            cohort_configured=bool(cohort_path),
        )
        def manually_reopened(context: dict[str, Any]) -> bool:
            """A human WP status move explicitly requeues V2 work.

            HUMAN_REQUIRED remains the canonical lifecycle until the editor
            moves the custom WP status back to ``pending``. That transition is
            the manual approval to re-enter the normal V2 loop; attempts and
            media progress are intentionally preserved.
            """
            return (
                context["v2_state"].state.value == "human_required"
                and (context.get("post") or {}).get("status") == "pending"
                and bool(
                    ((context.get("post") or {}).get("meta") or {}).get(
                        "_hermes_human_reopened_at"
                    )
                )
                and ((context.get("post") or {}).get("meta") or {}).get(
                    "_hermes_human_reopened_from"
                ) == "awaiting_human"
            )

        admitted_pending = [
            (post_id, context)
            for post_id, context in admitted
            if context["v2_state"].state.value == "pending" or manually_reopened(context)
        ]
        candidates = [
            (post_id, context) for post_id, context in admitted_pending
            if manually_reopened(context)
            or (
                context["v2_state"].state.value == "pending"
                and _cooldown_expired(context["v2_state"].retry.next_at, now)
            )
        ]
        pre_quarantine_candidates = candidates
        quarantine_path = Path(root) / "config" / "v2-quarantine.json"
        quarantines, quarantine_error = load_v2_quarantine(quarantine_path)
        quarantine_candidates = [
            (candidate_id, context) for candidate_id, context in candidates
            if candidate_id in quarantines
        ]
        if quarantine_error:
            # The quarantine file is a safety policy. Missing or malformed
            # policy must never silently reopen the editorial queue.
            candidates = []
        else:
            candidates = [
                (candidate_id, context) for candidate_id, context in candidates
                if candidate_id not in quarantines
            ]
        excluded_ids = {int(post_id) for post_id in excluded_post_ids}
        candidates = [
            (candidate_id, context)
            for candidate_id, context in candidates
            if int(candidate_id) not in excluded_ids
        ]

        class _SnapshotStore:
            def __init__(self, items):
                self.states = {post_id: context["v2_state"] for post_id, context in items}

            def load(self, post_id):
                return self.states[post_id]

        requested_admission = None
        requested_pending = None
        requested_eligible = None
        requested_quarantine = None
        if post_id is not None:
            requested_admission = any(candidate_id == post_id for candidate_id, _ in admitted)
            requested_pending = any(candidate_id == post_id for candidate_id, _ in admitted_pending)
            requested_eligible = any(candidate_id == post_id for candidate_id, _ in candidates)
            requested_quarantine = quarantines.get(post_id)
            # An explicit post request is never allowed to fall back to another
            # queue item.  The same admission/cooldown filters still apply.
            selected = [item for item in candidates if item[0] == post_id] if requested_eligible else []
        else:
            selected = select(
                candidates,
                _SnapshotStore(candidates),
                limit=limit,
                retry_post_ids=retry_post_ids,
                excluded_post_ids=excluded_ids,
                max_new_posts=max_new_posts,
            )
        scheduled_selected = selected
        expected_ids = None
        selection_drift = False
        selection_mismatch = False
        if expected_selected_ids is not None:
            expected_ids = [int(item) for item in expected_selected_ids]
            candidates_by_id = {
                int(candidate_id): (candidate_id, context)
                for candidate_id, context in candidates
            }
            expected_ids_are_valid = (
                len(expected_ids) <= limit
                and len(expected_ids) == len(set(expected_ids))
                and all(item in candidates_by_id for item in expected_ids)
            )
            if expected_ids_are_valid:
                # The preview authorizes these exact candidates and their order.
                # A new queue item may legitimately change scheduler ordering
                # between the read-only preview and write-mode; it must not
                # silently replace an already authorized post.
                selected = [candidates_by_id[item] for item in expected_ids]
                selection_drift = [int(item[0]) for item in scheduled_selected] != expected_ids
            else:
                selection_mismatch = True
        pending = [context for _post_id, context in admitted_pending]
        eligible_ids = {post_id for post_id, _context in candidates}
        cooldown = [
            (post_id, context) for post_id, context in admitted_pending
            if context["v2_state"].state.value == "pending"
            and post_id not in eligible_ids
            and post_id not in quarantines
            and not quarantine_error
        ]
        blockers: dict[str, int] = {}
        for context in pending:
            blocker = context["v2_state"].blocker.value if context["v2_state"].blocker else "none"
            blockers[blocker] = blockers.get(blocker, 0) + 1
        queue = {
            "scanned": len(snapshot),
            "pending_total": len(snapshot),
            "admitted_pending": len(admitted_pending),
            "admitted_total": len(admitted),
            "pending": len(pending),
            **admission,
            "eligible": len(candidates),
            "pre_quarantine_eligible": len(pre_quarantine_candidates),
            "quarantined": len(quarantine_candidates),
            "quarantined_ids": sorted(quarantines),
            "quarantined_posts": [
                {"post_id": candidate_id, **quarantines[candidate_id]}
                for candidate_id, _context in quarantine_candidates
            ],
            "quarantine_policy_error": quarantine_error,
            "quarantine_fail_closed": bool(quarantine_error),
            "cycle_excluded_ids": sorted(excluded_ids),
            "cycle_retry_ids": sorted(int(post_id) for post_id in retry_post_ids),
            "cycle_max_new_posts": max_new_posts,
            "cooldown": len(cooldown),
            "ready": sum(
                1 for _post_id, context in admitted
                if context["v2_state"].state.value == "ready"
            ),
            "human_required": sum(
                1 for _post_id, context in admitted
                if context["v2_state"].state.value == "human_required"
            ),
            "eligible_ids": sorted(eligible_ids),
            "cooldown_ids": [post_id for post_id, _context in cooldown],
            "selected_ids": [post_id for post_id, _context in selected],
            "selection_revalidated": expected_ids is not None and not selection_mismatch,
            "selection_drift": selection_drift,
            "by_blocker": blockers,
            "next_eligible_at": min(
                (
                    context["v2_state"].retry.next_at
                    for _post_id, context in cooldown
                    if context["v2_state"].retry.next_at
                ),
                default=None,
            ),
            "no_progress": {
                str(post_id): context["v2_state"].retry.no_progress
                for post_id, context in admitted
                if context["v2_state"].retry.no_progress
            },
            "media": {
                str(post_id): {
                    "required": context["v2_state"].media.required,
                    "accepted": context["v2_state"].media.valid_distinct_inline_count,
                    "accepted_working": context["v2_state"].media.accepted,
                    "missing": context["v2_state"].media.effective_missing,
                    "featured": context["v2_state"].media.featured.status.value,
                }
                for post_id, context in admitted
                if context["v2_state"].state.value == "pending"
            },
        }
        if post_id is not None:
            queue["requested_post_id"] = post_id
            queue["requested_admitted"] = requested_admission
            queue["requested_pending"] = requested_pending
            queue["requested_eligible"] = requested_eligible
            queue["requested_quarantine"] = requested_quarantine
            queue["request_reason"] = (
                "quarantine_policy_invalid"
                if quarantine_error
                else "quarantined"
                if requested_quarantine
                else
                "eligible"
                if requested_eligible
                else "not_admitted"
                if not requested_admission
                else "state_not_pending"
                if not requested_pending
                else "cooldown_active"
            )
        if selection_mismatch:
            return {
                "selected": len(selected),
                "completed": 0,
                "failed": 0,
                "locked": False,
                "preview": False,
                "selection_mismatch": True,
                "expected_selected_ids": expected_ids,
                "actual_selected_ids": [int(item[0]) for item in scheduled_selected],
                "queue": queue,
                "details": [],
            }
        if preview:
            return {
                "selected": len(selected),
                "completed": 0,
                "failed": 0,
                "locked": False,
                "preview": True,
                "post_id": post_id,
                "queue": queue,
                "details": [
                    {
                        "post_id": selected_id,
                        "state": context["v2_state"].state.value,
                        "phase": context["v2_state"].phase.value,
                        "blocker": context["v2_state"].blocker.value if context["v2_state"].blocker else None,
                        "cooldown": context["v2_state"].retry.next_at,
                    }
                    for selected_id, context in selected
                ],
            }
        details = []
        completed = 0
        failed = 0
        stages = {
            "editorial": ProductionEditorialStage(client, config, root),
            "media": ProductionMediaStage(
                client, config, root,
                resolver=ProductionMediaResolver(client, config, root),
            ),
            "compose": ProductionComposeStage(config, root),
            "validate": ProductionValidateStage(client, config, root),
        }
        for post_id, context in selected:
            context["root"] = root
            initial = context["v2_state"]
            run_id = uuid.uuid4().hex
            context["v2_run_id"] = run_id
            context["v2_cycle_id"] = str(cycle_id or "")
            context["v2_cycle_round"] = int(cycle_round) if cycle_round is not None else None
            context["candidate_must_be_fresh"] = initial.phase is not Phase.VALIDATE
            post_started_at = monotonic()
            buffered = BufferedStateStore(initial)
            try:
                from ..observability import append_telemetry

                append_telemetry(
                    root,
                    "v2_funnel",
                    stage="selected",
                    post_id=int(post_id),
                    run_id=run_id,
                    cycle_id=str(cycle_id or ""),
                    cycle_round=cycle_round,
                    initial_state=initial.state.value,
                )
            except Exception:
                pass
            try:
                # Mark the transaction before any provider/media work. If the
                # process dies after an upload, recovery must not mistake the
                # previous committed journal for a completed run.
                _write_v2_journal_checkpoint(root, post_id, "running", initial, run_id=run_id)
                try:
                    from ..media.registry import record_v2_observation
                    record_v2_observation(
                        root, post_id=post_id, run_id=run_id, state=initial,
                        phase=initial.phase.value, outcome="started",
                        checkpoint_reference=f"work/v2-journal/{post_id}.json",
                    )
                except Exception as exc:
                    from ..observability import record_unexpected_error
                    try:
                        record_unexpected_error(
                            root, exc, post_id=int(post_id), run_id=run_id,
                            file="pipeline_v2/runtime.py", field="media_registry.start",
                        )
                    except Exception:
                        pass
                outcome = PipelineRunner(buffered, stages, config=config).run_one(post_id, context)
                _write_v2_media_checkpoint(root, post_id, buffered.state.media)
                _write_v2_journal_checkpoint(
                    root, post_id, "validated", buffered.state, run_id=run_id
                )
                error_path = root / "backups" / str(post_id) / "editorial.error.json"
                if outcome.blocker is not None and outcome.blocker.value == "provider_error" and error_path.is_file():
                    try:
                        context["provider_reason"] = json.loads(error_path.read_text(encoding="utf-8")).get("reason")
                    except (OSError, ValueError):
                        context["provider_reason"] = "provider_error"
                try:
                    write = WordPressWriterV2(
                        client, root, policy_version=config.policy_version
                    ).commit(post_id, context, buffered.state, outcome)
                except Exception as exc:
                    _write_v2_journal_checkpoint(
                        root, post_id, "writer_failed", buffered.state, run_id=run_id
                    )
                    try:
                        from ..observability import append_telemetry

                        append_telemetry(
                            root,
                            "v2_writer_failure",
                            post_id=int(post_id),
                            run_id=run_id,
                            detail=str(exc)[:400],
                        )
                    except Exception:  # noqa: BLE001 - telemetry cannot mask failure
                        pass
                    raise
                canonical_state = write.get("state")
                if isinstance(canonical_state, dict):
                    buffered.state = WorkState.from_dict(canonical_state)
                    _write_v2_media_checkpoint(root, post_id, buffered.state.media)
                completed += 1
                try:
                    from ..media.registry import invalidate_v2_media, record_v2_observation
                    observed_state = buffered.state
                    applied = bool(write.get("media_readback"))
                    if applied:
                        candidate_path = root / "backups" / str(post_id) / "editorial.candidate.json"
                        candidate_payload = json.loads(candidate_path.read_text(encoding="utf-8"))
                        applied_media = MediaProgress.from_dict(candidate_payload.get("media"))
                        previous_ids = {item.media_id for item in initial.media.inline}
                        applied_inline_ids = {item.media_id for item in applied_media.inline}
                        invalidate_v2_media(
                            root,
                            post_id=int(post_id),
                            media_ids=sorted(previous_ids - applied_inline_ids),
                            role="inline",
                        )
                        observed_state = replace(
                            buffered.state,
                            media=replace(
                                buffered.state.media,
                                inline=applied_media.inline,
                                featured=applied_media.featured,
                                accepted_count=None,
                            ),
                        )
                    record_v2_observation(
                        root, post_id=post_id, run_id=run_id, state=observed_state,
                        phase=buffered.state.phase.value, outcome=outcome.type.value,
                        checkpoint_reference=f"work/v2-journal/{post_id}.json",
                        applied=applied,
                    )
                except Exception as exc:
                    from ..observability import record_unexpected_error
                    try:
                        record_unexpected_error(
                            root, exc, post_id=int(post_id), run_id=run_id,
                            file="pipeline_v2/runtime.py", field="media_registry.finish",
                        )
                    except Exception:
                        pass
                details.append({
                    "post_id": post_id,
                    "initial_state": initial.state.value,
                    "phase": buffered.state.phase.value,
                    "outcome": outcome.type.value,
                    "blocker": outcome.blocker.value if outcome.blocker else None,
                    "detail": outcome.detail or context.get("provider_reason"),
                    "run_id": run_id,
                    "cycle_id": str(cycle_id or ""),
                    "cycle_round": cycle_round,
                    "duration_ms": round((monotonic() - post_started_at) * 1000, 1),
                    "stage_durations_ms": dict(context.get("_v2_stage_durations_ms") or {}),
                    "media": _media_audit_summary(buffered.state.media, write),
                    **write,
                })
                try:
                    from ..observability import append_telemetry

                    funnel_fields = {
                        "post_id": int(post_id),
                        "run_id": run_id,
                        "outcome": outcome.type.value,
                        "blocker": outcome.blocker.value if outcome.blocker else None,
                        "cycle_id": str(cycle_id or ""),
                        "cycle_round": cycle_round,
                    }
                    append_telemetry(root, "v2_funnel", stage="processed", **funnel_fields)
                    written_state = write.get("state")
                    if (
                        outcome.type.value == "ready"
                        and isinstance(written_state, dict)
                        and written_state.get("state") == "ready"
                    ):
                        append_telemetry(root, "v2_funnel", stage="ready", **funnel_fields)
                except Exception:
                    pass
            except Exception as exc:  # noqa: BLE001 - isolate one post
                failed += 1
                details.append({
                    "post_id": post_id,
                    "initial_state": initial.state.value,
                    "outcome": "error",
                    "blocker": "internal_error",
                    "detail": str(exc)[:400],
                    "run_id": run_id,
                    "cycle_id": str(cycle_id or ""),
                    "cycle_round": cycle_round,
                    "duration_ms": round((monotonic() - post_started_at) * 1000, 1),
                    "stage_durations_ms": dict(context.get("_v2_stage_durations_ms") or {}),
                })
                try:
                    from ..observability import append_telemetry

                    append_telemetry(
                        root,
                        "v2_funnel",
                        stage="failed",
                        post_id=int(post_id),
                        run_id=run_id,
                        cycle_id=str(cycle_id or ""),
                        cycle_round=cycle_round,
                        blocker="internal_error",
                    )
                except Exception:
                    pass
                continue
        funnel = {
            "selected": len(selected),
            "processed": completed,
            "ready": sum(1 for detail in details if detail.get("outcome") == "ready"),
            "failed": failed,
        }
        return {
            "selected": len(selected),
            "completed": completed,
            "failed": failed,
            "locked": False,
            "queue": queue,
            "funnel": funnel,
            "details": details,
        }
    finally:
        if lock is not None:
            lock.release()
