"""Minimal production bridge: buffered runner, writer, journal and v2-run."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from ..checklist import required_image_count
from ..content_quality import word_count
from ..manifest import build_ready_manifest, manifest_hash, serialize_manifest
from ..media.evidence import editorial_subjects, item_query, post_subjects
from ..list_quality import detect_list_format
from ..seo.rank_math import build_meta
from ..state import STATE_READY, build_state_markers
from ..workflow import _execute_media_plan, validate_media_plan
from .lock import RunSessionLock
from .model import FeaturedProgress, FeaturedStatus, InlineMedia, MediaProgress, MediaSearchProgress, Phase
from .production import ProductionCandidateReader
from .production_stages import ProductionComposeStage, ProductionEditorialStage, ProductionMediaStage, ProductionValidateStage
from .runner import PipelineRunner
from .scheduler import _cooldown_expired, select


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


def admit_v2_candidates(
    snapshot: list[tuple[int, dict[str, Any]]],
    admission_after: Any,
) -> tuple[list[tuple[int, dict[str, Any]]], dict[str, Any]]:
    """Admit only posts created at/after the fixed V2 production cutoff.

    The decision deliberately ignores phase and lifecycle state. Once a post
    crosses the cutoff it remains eligible for its normal V2 retry lifecycle;
    terminal/cooldown handling is left to the scheduler. Missing configuration
    or post dates fail closed and are exposed in the audit report.
    """
    cutoff = _parse_admission_datetime(admission_after)
    admitted: list[tuple[int, dict[str, Any]]] = []
    historical_ids: list[int] = []
    blocked_ids: list[int] = []
    missing_date_ids: list[int] = []

    for post_id, context in snapshot:
        if cutoff is None:
            blocked_ids.append(post_id)
            continue
        post_date = _original_post_datetime(context)
        if post_date is None:
            missing_date_ids.append(post_id)
            continue
        if post_date >= cutoff:
            admitted.append((post_id, context))
        else:
            historical_ids.append(post_id)

    return admitted, {
        "admission_configured": cutoff is not None,
        "admission_after": cutoff.isoformat() if cutoff is not None else None,
        "historical_excluded": len(historical_ids),
        "historical_excluded_ids": historical_ids,
        "admission_blocked": len(blocked_ids),
        "admission_blocked_ids": blocked_ids,
        "admission_missing_date": len(missing_date_ids),
        "admission_missing_date_ids": missing_date_ids,
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


def _write_v2_journal_checkpoint(root: Path, post_id: int, status: str, state) -> None:
    target = Path(root) / "work" / "v2-journal" / f"{post_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {"status": status, "post_id": post_id, "state": state.to_dict()},
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
        total_required = required_image_count(word_count(html), title=title, content=html)
        inline_needed = max(0, total_required - previous.accepted)
        featured = self._recover_existing_featured(context, editorial, previous)
        featured_needed = 0 if featured.status is FeaturedStatus.VALID else 1

        from ..cli import _resolve_media_batch

        subject_rows = post_subjects(
            title=title,
            content_html=html,
            focus_keyword=focus_keyword,
            game_name=editorial.get("game_name"),
        )
        source_subjects = editorial_subjects(
            title=title,
            content_html=html,
            focus_keyword=focus_keyword,
            game_name=editorial.get("game_name"),
        )
        is_listicle = detect_list_format(title, html) is not None
        covered_items = {
            str(item.item_number)
            for item in previous.inline
            if item.item_number is not None
        }
        covered_subjects = {
            item.subject.casefold().strip()
            for item in previous.inline
            if item.subject.strip()
        }
        missing_rows = [
            (index, row) for index, row in enumerate(subject_rows)
            if not is_listicle
            or (
                str(row.get("item")) not in covered_items
                and str(row.get("subject") or "").casefold().strip() not in covered_subjects
            )
        ]
        if is_listicle:
            # A migrated listicle may have accepted media without item_number.
            # It must cover every item and still satisfy the word-count
            # minimum when that is larger than the item count.
            inline_needed = max(total_required - previous.accepted, len(missing_rows))
        if inline_needed == 0 and featured_needed == 0:
            return MediaProgress(
                required=total_required,
                inline=previous.inline,
                featured=featured,
                search=previous.search,
                enrichment_round=previous.enrichment_round,
                waiver_applied=previous.waiver_applied,
                waiver_reason=previous.waiver_reason,
            )
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
            add_query(subject, item_query(subject, title, extra=focus_keyword))
        if focus_keyword and not is_listicle:
            main_subject = str((subject_rows[0] if subject_rows else {}).get("subject") or focus_keyword)
            add_query(main_subject, item_query(focus_keyword, title))
        if not subject_queries and not is_listicle:
            add_query(title, item_query(title, title, extra=focus_keyword))

        search_runs: list[dict[str, Any]] = []
        current_inline = list(previous.inline)
        current_featured = featured
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
                resolved = _resolve_media_batch(
                    self.client, self.config, self.root, batch, full=True, allow_reuse=True
                )
            except Exception:
                from ..media.google_browser import cleanup_browser_artifacts

                cleanup_browser_artifacts()
                raise
            row = (resolved.get("posts") or [{}])[0]
            if role == "inline" and isinstance(row.get("search"), dict):
                search_runs.append(dict(row["search"]))
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
                    "search_query": query,
                    "role": role,
                    "evidence": reused.get("evidence") or {"verdict": "deterministic_match"},
                    "item_number": (subject_meta.get(subject) or {}).get("item"),
                    "section_heading": (subject_meta.get(subject) or {}).get("heading") or subject,
                    "section_slot": (subject_meta.get(subject) or {}).get("section_slot"),
                })
            for candidate in row.get("audit_candidates") or []:
                if not isinstance(candidate, dict):
                    continue
                candidate = dict(candidate)
                candidate["subject"] = subject
                candidate["search_query"] = query
                candidate["role"] = role
                metadata = subject_meta.get(subject) or {}
                candidate["item_number"] = metadata.get("item")
                candidate["section_heading"] = metadata.get("heading") or subject
                candidate["section_slot"] = metadata.get("section_slot")
                candidates.append(candidate)
            return candidates

        def execute_plan(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
            """Execute one candidate batch and merge only final accepted media."""
            nonlocal current_featured
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
                results, _featured_id, _featured_credit = _execute_media_plan(
                    {**editorial, "media_plan": plan},
                    self.config,
                    self.client,
                    self.root,
                    preflight=checked,
                    post_id=int(context["post_id"]),
                    previous_inline_phashes=tuple(
                        item.phash for item in current_inline if item.phash
                    ),
                )
            finally:
                from ..media.google_browser import cleanup_browser_artifacts

                cleanup_browser_artifacts()
            for result in results:
                status = result.get("status")
                if status and status not in {"accepted", "ok"}:
                    continue
                plan_item = plan[0]
                if result.get("media_id") and result.get("media_url") and not result.get("featured"):
                    current_inline.append(InlineMedia(
                        int(result["media_id"]),
                        str(result["media_url"]),
                        int(result.get("paragraph_index", 0)),
                        str(result.get("alt_text", "")),
                        str(result.get("credit_text", "")),
                        str(plan_item.get("subject") or ""),
                        plan_item.get("item_number"),
                        str(plan_item.get("section_heading") or ""),
                        plan_item.get("section_slot"),
                        int(result.get("width") or 1200),
                        int(result.get("height") or 800),
                        str(result.get("phash") or ""),
                    ))
                if result.get("featured") and result.get("media_id"):
                    current_featured = FeaturedProgress(
                        FeaturedStatus.VALID,
                        int(result["media_id"]),
                        str(result.get("media_url") or ""),
                    )
            # Crash-safe incremental progress: an accepted asset is durable
            # before the resolver moves on to the next candidate/query.
            _write_v2_media_checkpoint(
                self.root,
                int(context["post_id"]),
                MediaProgress(
                    required=total_required,
                    inline=tuple({item.media_id: item for item in current_inline}.values()),
                    featured=current_featured,
                    search=previous.search,
                    enrichment_round=enrichment_round,
                ),
            )
            return results

        featured_queries: list[tuple[str, str]] = []
        if featured_needed:
            featured_row = subject_rows[0] if subject_rows else {}
            featured_subject = str(featured_row.get("subject") or title)
            featured_query = item_query(featured_subject, title, extra=focus_keyword)
            featured_queries = [
                (featured_subject, f"{featured_query} key art"),
                (featured_subject, featured_query),
            ]
            for subject, query in featured_queries:
                if current_featured.status is FeaturedStatus.VALID:
                    break
                candidates = resolve_query(subject, query, 1, "featured")
                self._resolve_ambiguous_vision(candidates, post_id=int(context["post_id"]))
                for candidate in self._approved_unique(candidates):
                    if current_featured.status is FeaturedStatus.VALID:
                        break
                    execute_plan([
                        self._plan_item(candidate, candidate.get("subject") or title, 0, True)
                    ])

        covered_item_keys = {
            str(item.item_number or item.subject).casefold()
            for item in current_inline
            if item.item_number is not None or item.subject
        }
        used_slots = {item.slot for item in current_inline}

        def inline_remaining() -> int:
            if is_listicle:
                uncovered = sum(
                    1
                    for _index, row in missing_rows
                    if str(row.get("item") or row.get("subject") or "").casefold()
                    not in covered_item_keys
                )
                return max(uncovered, total_required - len(current_inline))
            return max(0, total_required - len(current_inline))

        if inline_needed:
            # The article's own Fonte is the first acquisition strategy.  Its
            # assets already have provenance by construction; they still pass
            # through the regular executable media-plan gates below.
            source_url = str(context.get("original_link") or "").strip()
            if not source_url:
                post = context.get("post") or {}
                source_url = (
                    str((post.get("meta") or {}).get("original_link") or "").strip()
                    if isinstance(post, dict)
                    else ""
                )
            if source_url:
                source_stats: dict[str, int] = {}
                source_attempted = 0
                source_accepted = 0
                source_reused = 0
                try:
                    from ..media.source_verify import discover_article_source_candidates

                    source_candidates = discover_article_source_candidates(
                        source_url,
                        subject=(source_subjects[0] if source_subjects else (subject_queries[0][0] if subject_queries else title)),
                        subjects=source_subjects,
                        limit=max(8, inline_remaining() * 4),
                        stats=source_stats,
                    )
                except Exception:
                    source_candidates = []
                for candidate in source_candidates:
                    if inline_remaining() <= 0:
                        break
                    source_attempted += 1
                    subject = str(candidate.get("subject") or (source_subjects[0] if source_subjects else (subject_queries[0][0] if subject_queries else title)))
                    candidate["subject"] = subject
                    metadata = subject_meta.get(subject) or {}
                    candidate["item_number"] = metadata.get("item")
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
                        used_slots.add(slot)
                        before = len(current_inline)
                        execute_plan([
                            self._plan_item(source_candidate, subject, slot, False)
                        ])
                        if len(current_inline) > before:
                            source_accepted += 1
                            if source_candidate.get("media_library_id"):
                                source_reused += 1
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
                candidates = resolve_query(
                    subject,
                    query,
                    1 if is_listicle else remaining,
                    "inline",
                )
                self._resolve_ambiguous_vision(candidates, post_id=int(context["post_id"]))
                for candidate in self._approved_unique(candidates):
                    if inline_remaining() <= 0:
                        break
                    candidate_key = str(
                        candidate.get("item_number") or candidate.get("subject") or ""
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
                        used_slots.add(slot)
                    before = len(current_inline)
                    execute_plan([
                        self._plan_item(candidate, candidate.get("subject") or title, slot, False)
                    ])
                    if len(current_inline) > before:
                        covered_item_keys.add(candidate_key)
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
            elif completion_reasons and all(reason == "EXHAUSTED" for reason in completion_reasons):
                completion_reason = "EXHAUSTED"
            else:
                completion_reason = "INTERRUPTED" if completion_reasons else ""
            search_completed = bool(
                queries_planned > 0
                and queries_attempted >= queries_planned
                and queries_completed == queries_planned
            )
            search_progress = MediaSearchProgress(
                completed=search_completed,
                # Exhaustion is finalized below, after the executable media
                # plan has produced its final WebP pHashes.  Discovery-level
                # candidates may collapse into duplicates during conversion.
                exhausted=False,
                queries_attempted=queries_attempted,
                engines_attempted=tuple(dict.fromkeys(
                    str(engine)
                    for run in search_runs
                    for engine in (run.get("engines_attempted") or [])
                )),
                engines_disabled=tuple(dict.fromkeys(
                    str(engine)
                    for run in search_runs
                    for engine in (run.get("engines_disabled") or [])
                )),
                candidates_seen=sum(int(run.get("candidates_seen") or 0) for run in search_runs),
                candidates_rejected=sum(int(run.get("candidates_rejected") or 0) for run in search_runs),
                distinct_valid_frames=0,
                queries_planned=queries_planned,
                queries_completed=queries_completed,
                completion_reason=completion_reason,
            )
            final_distinct = {
                str(item.phash or item.media_url)
                for item in current_inline
                if item.phash or item.media_url
            }
            search_progress = replace(
                search_progress,
                exhausted=bool(search_completed and len(final_distinct) < total_required),
                distinct_valid_frames=len(final_distinct),
            )
        else:
            search_progress = previous.search
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

    def _resolve_ambiguous_vision(self, candidates: list[dict[str, Any]], *, post_id: int | None = None) -> None:
        vision_candidates = []
        for index, candidate in enumerate(candidates):
            url = candidate.get("direct_image_url") or candidate.get("image_url")
            verdict = (candidate.get("evidence") or {}).get("verdict")
            if url and verdict in {"ambiguous", "needs_vision", "inconclusive"}:
                vision_candidates.append({
                    "candidate_id": str(candidate.get("candidate_id") or f"v2-{index}"),
                    "source_image_url": str(url),
                    "subject": str(candidate.get("subject") or ""),
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
        from ..media.vision_cache import set_cached_decision
        by_candidate_id = {str(item["candidate_id"]): item for item in low_items}
        for candidate in candidates:
            source_url = str(candidate.get("direct_image_url") or "")
            item = by_candidate_id.get(str(candidate.get("candidate_id") or ""))
            decision = low_decisions.get(item["candidate_id"]) if item else None
            if item and item["candidate_id"] in high_decisions:
                decision = high_decisions[item["candidate_id"]]
            if decision and decision.get("verdict") == "accept":
                set_cached_decision(
                    self.root,
                    source_url,
                    str(candidate.get("subject") or ""),
                    {"status": "MATCH", "confidence": float(decision.get("confidence") or 0), "visual_type": decision.get("visual_type") or "other"},
                )
                candidate.setdefault("evidence", {})["verdict"] = "deterministic_match"
                candidate["needs_vision"] = False

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
        }


class WordPressWriterV2:
    def __init__(self, client, root: Path, *, policy_version: int = 1):
        self.client, self.root, self.policy_version = client, Path(root), policy_version

    def commit(self, post_id: int, context: dict[str, Any], proposed_state, outcome):
        journal_dir = self.root / "work" / "v2-journal"
        journal_dir.mkdir(parents=True, exist_ok=True)
        candidate_path = self.root / "backups" / str(post_id) / "editorial.candidate.json"
        candidate = json.loads(candidate_path.read_text(encoding="utf-8")) if candidate_path.is_file() else {}
        content = str(candidate.get("content") or "")
        seo = candidate.get("seo") or {}
        post = context.get("post") or self.client.get_post(post_id)
        meta = dict(post.get("meta") or {})
        state_json = json.dumps(proposed_state.to_dict(), ensure_ascii=False, separators=(",", ":"))
        payload_meta: dict[str, Any] = {"_hermes_work_state": state_json}
        ready_hash = ""
        if outcome.type.value == "ready" and content:
            seo_meta = build_meta({key: str(seo.get(key) or "") for key in ("title", "meta_description", "focus_keyword")}, existing=meta)
            meta.update(seo_meta)
            manifest = build_ready_manifest(post_id=post_id, content=content, featured_media=candidate.get("featured_media"), seo=seo, original_link=context.get("original_link"), editorial=candidate.get("editorial"), policy_version=self.policy_version)
            ready_hash = manifest_hash(manifest)
            payload_meta.update({"_hermes_ready_manifest": serialize_manifest(manifest), "_hermes_ready_hash": ready_hash})
            payload_meta.update(build_state_markers(STATE_READY, ready_hash=ready_hash, policy_version=self.policy_version))
            latest = self.root / "backups" / str(post_id) / "editorial.latest.json"
            latest.parent.mkdir(parents=True, exist_ok=True)
            latest.write_text(json.dumps(candidate.get("editorial") or {}, ensure_ascii=False, indent=2), encoding="utf-8")
        journal = journal_dir / f"{post_id}.json"
        intent = {"status": "prepared", "post_id": post_id, "state": proposed_state.to_dict(), "ready_hash": ready_hash, "candidate_hash": hashlib.sha256(content.encode()).hexdigest(), "detail": outcome.detail or context.get("provider_reason")}
        journal.write_text(json.dumps(intent, ensure_ascii=False, indent=2), encoding="utf-8")
        update: dict[str, Any] = {"meta": {**meta, **payload_meta}}
        if outcome.type.value == "ready" and content:
            update["content"] = {"raw": content}
            update["featured_media"] = candidate.get("featured_media")
        current = post.get("content") or {}
        if update.get("content", {}).get("raw") == current.get("raw") and meta.get("_hermes_work_state") == state_json:
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
        if outcome.type.value == "ready":
            if (readback.get("content") or {}).get("raw") != content:
                raise RuntimeError("candidate content read-back mismatch")
            if readback_meta.get("_hermes_ready_hash") != ready_hash:
                raise RuntimeError("ready hash read-back mismatch")
            import html as _html

            expected_inline_urls = [
                str(item.media_url or "").strip()
                for item in getattr(getattr(proposed_state, "media", None), "inline", ())
                if str(item.media_url or "").strip()
            ]
            readback_content = _html.unescape(str((readback.get("content") or {}).get("raw") or ""))
            missing_inline = [
                url for url in expected_inline_urls
                if url not in readback_content
            ]
            if missing_inline:
                raise RuntimeError(
                    "inline media read-back mismatch: "
                    + ", ".join(missing_inline[:5])
                )
            try:
                from .observability import append_telemetry

                append_telemetry(
                    self.root,
                    "media_apply_readback",
                    post_id=int(post_id),
                    accepted_media=len(expected_inline_urls),
                    inline_applied=sum(url in _html.unescape(content) for url in expected_inline_urls),
                    inline_readback=sum(url in readback_content for url in expected_inline_urls),
                    readback=True,
                )
            except Exception:
                pass
        journal.write_text(json.dumps({**intent, "status": "committed", "readback": True}, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"wordpress_changed": changed, "readback": True, "ready_hash": ready_hash}


def run_v2(client, config, root: Path, *, limit: int = 1) -> dict[str, Any]:
    if limit < 1:
        raise ValueError("limit must be positive")
    lock = RunSessionLock(Path(root) / "work" / "v2-run.lock")
    if not lock.acquire():
        return {"selected": 0, "locked": True, "details": []}
    try:
        reader = ProductionCandidateReader(client, root)
        snapshot = reader.snapshot(page_size=max(10, limit))
        now = datetime.now(timezone.utc)
        admitted, admission = admit_v2_candidates(
            snapshot, getattr(config, "v2_admission_after", None)
        )
        admitted_pending = [
            (post_id, context)
            for post_id, context in admitted
            if context["v2_state"].state.value == "pending"
        ]
        candidates = [
            (post_id, context) for post_id, context in admitted_pending
            if context["v2_state"].state.value == "pending"
            and _cooldown_expired(context["v2_state"].retry.next_at, now)
        ]

        class _SnapshotStore:
            def __init__(self, items):
                self.states = {post_id: context["v2_state"] for post_id, context in items}

            def load(self, post_id):
                return self.states[post_id]

        selected = select(candidates, _SnapshotStore(candidates), limit=limit)
        pending = [context for _post_id, context in admitted_pending]
        eligible_ids = {post_id for post_id, _context in candidates}
        cooldown = [
            (post_id, context) for post_id, context in admitted_pending
            if context["v2_state"].state.value == "pending"
            and post_id not in eligible_ids
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
                    "accepted": context["v2_state"].media.accepted,
                    "missing": context["v2_state"].media.missing,
                    "featured": context["v2_state"].media.featured.status.value,
                }
                for post_id, context in admitted
                if context["v2_state"].state.value == "pending"
            },
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
            initial = context["v2_state"]
            buffered = BufferedStateStore(initial)
            try:
                # Mark the transaction before any provider/media work. If the
                # process dies after an upload, recovery must not mistake the
                # previous committed journal for a completed run.
                _write_v2_journal_checkpoint(root, post_id, "running", initial)
                outcome = PipelineRunner(buffered, stages, config=config).run_one(post_id, context)
                _write_v2_media_checkpoint(root, post_id, buffered.state.media)
                _write_v2_journal_checkpoint(
                    root, post_id, "validated", buffered.state
                )
                error_path = root / "backups" / str(post_id) / "editorial.error.json"
                if outcome.blocker is not None and outcome.blocker.value == "provider_error" and error_path.is_file():
                    try:
                        context["provider_reason"] = json.loads(error_path.read_text(encoding="utf-8")).get("reason")
                    except (OSError, ValueError):
                        context["provider_reason"] = "provider_error"
                write = WordPressWriterV2(
                    client, root, policy_version=config.policy_version
                ).commit(post_id, context, buffered.state, outcome)
                completed += 1
                details.append({
                    "post_id": post_id,
                    "initial_state": initial.state.value,
                    "phase": buffered.state.phase.value,
                    "outcome": outcome.type.value,
                    "blocker": outcome.blocker.value if outcome.blocker else None,
                    "detail": outcome.detail or context.get("provider_reason"),
                    **write,
                })
            except Exception as exc:  # noqa: BLE001 - isolate one post
                failed += 1
                details.append({
                    "post_id": post_id,
                    "initial_state": initial.state.value,
                    "outcome": "error",
                    "blocker": "internal_error",
                    "detail": str(exc)[:400],
                })
                continue
        return {
            "selected": len(selected),
            "completed": completed,
            "failed": failed,
            "locked": False,
            "queue": queue,
            "details": details,
        }
    finally:
        lock.release()
