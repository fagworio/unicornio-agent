"""V2 orchestration over injected stages; no provider or WP imports."""

from dataclasses import replace
from time import monotonic
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from ..content_quality import _keyword_in_text, rendered_text
from .classifier import MEDIA_BLOCKERS, blocker_for_gate, classify, classify_stage_error, editorial_decision
from .errors import StageError
from .model import BlockerCode, CURRENT_RETRY_POLICY_VERSION, FeaturedProgress, FeaturedStatus, InlineMedia, LifecycleState, MediaProgress, Outcome, OutcomeType, Phase, RetryInfo, WorkState


class PipelineRunner:
    def __init__(self, state_store: Any, stages: dict[str, Callable[..., dict[str, Any]]], config: Any | None = None):
        required = {"editorial", "media", "compose", "validate"}
        missing = required - set(stages)
        if missing:
            raise ValueError(f"missing stages: {sorted(missing)}")
        self.state_store = state_store
        self.stages = stages
        self.config = config

    def _policy(self) -> dict[str, int]:
        config = self.config
        return {
            "max_media_no_progress": int(getattr(
                config,
                "max_media_no_progress_attempts",
                getattr(config, "max_media_search_attempts", 2),
            )),
            "max_rework_attempts": int(getattr(config, "max_rework_attempts", 3)),
            "cooldown_minutes": int(getattr(config, "rework_cooldown_minutes", 30)),
        }

    @staticmethod
    def _media_attempt_incomplete(media: MediaProgress) -> bool:
        if media.visual_identity_pending:
            return True
        search = getattr(media, "search", None)
        if search is None:
            return False
        coverage = search.coverage if isinstance(search.coverage, dict) else {}
        source_discovery = coverage.get("source_discovery")
        if isinstance(source_discovery, dict) and source_discovery.get("retryable") is True:
            return True
        return bool(
            not search.completed
            and int(search.queries_planned or 0) > int(search.queries_completed or 0)
        )

    @staticmethod
    def _editorial_preflight(editorial: dict[str, Any], context: dict[str, Any]) -> None:
        """Reject an invalid focus keyword before any media side effect."""
        raw_seo = editorial.get("seo")
        seo = raw_seo if isinstance(raw_seo, dict) else {}
        keyword = str(seo.get("focus_keyword") or "").strip()
        if not keyword:
            if isinstance(raw_seo, dict) and any(
                key in raw_seo for key in ("title", "meta_description", "focus_keyword")
            ):
                raise StageError(
                    BlockerCode.TEXT_QUALITY,
                    Phase.EDITORIAL,
                    "focus keyword is required (title_match=false, content_match=false)",
                )
            return
        title = str(seo.get("title") or editorial.get("title") or context.get("title") or "")
        html = str(editorial.get("cleaned_html") or editorial.get("content") or "")
        title_match = bool(title and _keyword_in_text(keyword, title))
        content_text = rendered_text(html)
        content_match = bool(content_text and _keyword_in_text(keyword, content_text))
        if not (title_match and content_match):
            raise StageError(
                BlockerCode.TEXT_QUALITY,
                Phase.EDITORIAL,
                "focus keyword must occur naturally in title and content "
                f"(title_match={str(title_match).lower()}, content_match={str(content_match).lower()})",
            )

    def run_one(self, post_id: int, context: dict[str, Any]) -> Outcome:
        previous = self.state_store.load(post_id)
        context = context if isinstance(context, dict) else {}

        def call_stage(name: str, stage: Callable[..., Any], *args: Any) -> Any:
            started_at = monotonic()
            try:
                return stage(*args)
            finally:
                durations = context.setdefault("_v2_stage_durations_ms", {})
                durations[name] = round((monotonic() - started_at) * 1000, 1)

        editorial = context.get("editorial") or context.get("draft")
        run_editorial = previous.phase in {Phase.RELEVANCE, Phase.EDITORIAL}
        if run_editorial:
            try:
                editorial = call_stage("editorial", self.stages["editorial"], context, previous)
            except StageError as exc:
                editorial = {}
                outcome = classify_stage_error(previous, exc, **self._policy())
                self.state_store.commit(post_id, self._next_state(previous, editorial, previous.media, outcome))
                return outcome
        if not isinstance(editorial, dict):
            editorial = {}
        if previous.phase in {Phase.MEDIA, Phase.COMPOSE, Phase.VALIDATE} and not editorial:
            outcome = classify_stage_error(
                previous,
                StageError(BlockerCode.MANIFEST_INVALID, previous.phase, "persisted editorial draft is missing"),
                **self._policy(),
            )
            self.state_store.commit(post_id, self._next_state(previous, editorial, previous.media, outcome))
            return outcome
        if previous.phase in {Phase.RELEVANCE, Phase.EDITORIAL} and not editorial:
            outcome = classify_stage_error(
                previous,
                StageError(BlockerCode.MANIFEST_INVALID, previous.phase, "editorial stage returned no document"),
                **self._policy(),
            )
            self.state_store.commit(post_id, self._next_state(previous, editorial, previous.media, outcome))
            return outcome
        if editorial_decision(editorial) not in {"skip", "uncertain"}:
            try:
                self._editorial_preflight(editorial, context)
            except StageError as exc:
                outcome = classify_stage_error(previous, exc, **self._policy())
                self.state_store.commit(post_id, self._next_state(previous, editorial, previous.media, outcome))
                return outcome
        media: MediaProgress = previous.media
        media_completed = False
        media_stage_error = False
        recovered_media: MediaProgress | None = None
        validation: dict[str, Any] | None = None
        media_base_no_progress = (
            previous.retry.no_progress
            if previous.phase is Phase.MEDIA
            else 0
        )
        stage_error_no_progress = media_base_no_progress
        decision = editorial.get("decision")
        if decision in {"skip", "uncertain"}:
            outcome = classify(previous, editorial, previous.media, {"passed": False, "failures": []}, **self._policy())
        else:
            try:
                if previous.phase in {Phase.RELEVANCE, Phase.EDITORIAL, Phase.MEDIA}:
                    media = call_stage("media", self.stages["media"], context, previous, editorial)
                    if not isinstance(media, MediaProgress):
                        raise TypeError("MediaStage must return MediaProgress")
                    media_completed = True
                if previous.phase is Phase.COMPOSE and not editorial:
                    raise StageError(BlockerCode.MANIFEST_INVALID, Phase.COMPOSE, "compose requires persisted editorial")
                if previous.phase is Phase.VALIDATE and not context.get("candidate"):
                    raise StageError(BlockerCode.MANIFEST_INVALID, Phase.VALIDATE, "validate requires persisted candidate")
                candidate = context.get("candidate") if previous.phase is Phase.VALIDATE else None
                if candidate is None and previous.phase is not Phase.VALIDATE:
                    candidate = call_stage("compose", self.stages["compose"], context, editorial, media)
                elif candidate is None:
                    candidate = context.get("draft") or {}
                validation = call_stage("validate", self.stages["validate"], context, candidate)
                media_decision = ((validation.get("checklist") or {}).get("media_decision")
                                  if isinstance(validation, dict) else None)
                if isinstance(media_decision, dict) and media_decision.get("waiver_applied"):
                    media = replace(
                        media,
                        waiver_applied=True,
                        waiver_reason=str(media_decision.get("waiver_reason") or ""),
                    )
                    try:
                        from ..observability import append_telemetry

                        append_telemetry(
                            getattr(self.config, "root", None) or context.get("root") or ".",
                            "media_waiver_applied",
                            post_id=int(post_id),
                            reason=str(media_decision.get("waiver_reason") or ""),
                            enrichment_round=int(media_decision.get("enrichment_round") or 0),
                            search_completed=bool(media_decision.get("search_completed")),
                            search_exhausted=bool(media_decision.get("search_exhausted")),
                            search_completion_reason=str(
                                media_decision.get("search_completion_reason") or ""
                            ),
                        )
                    except Exception:  # noqa: BLE001 - telemetry cannot change lifecycle
                        pass
                progress = self._media_progressed(previous.media, media) if media_completed else False
                search_incomplete = (
                    media_completed and self._media_attempt_incomplete(media)
                )
                current_no_progress = (
                    (0 if progress else media_base_no_progress + 1)
                    if media_completed
                    and not search_incomplete
                    else media_base_no_progress
                )
                outcome = classify(previous, editorial, media, validation, no_progress=current_no_progress, **self._policy())
            except StageError as exc:
                media_stage_error = exc.phase is Phase.MEDIA
                recovered_media = exc.media_progress
                if media_stage_error and exc.blocker in MEDIA_BLOCKERS:
                    recovered_progress = (
                        isinstance(recovered_media, MediaProgress)
                        and self._media_progressed(previous.media, recovered_media)
                    )
                    stage_error_no_progress = (
                        0 if recovered_progress else media_base_no_progress + 1
                    )
                outcome = classify_stage_error(
                    previous,
                    exc,
                    no_progress=stage_error_no_progress if media_stage_error else None,
                    **self._policy(),
                )
        # Reconcile first, then measure progress. A newly uploaded asset that
        # the checklist rejects is not progress and must consume the bounded
        # media no-progress budget. Reclassify when that correction changes the
        # counter (the old order incorrectly granted progress before removal).
        # A technical MEDIA failure has no trustworthy identity for the asset
        # that failed. The failed attempt cannot invalidate media accepted in
        # a previous run; only checklist identities may do that.
        if media_stage_error:
            effective_media = recovered_media if isinstance(recovered_media, MediaProgress) else previous.media
        else:
            effective_media = self._reconcile_media_for_outcome(
                media,
                outcome,
                validation,
                previous_media=previous.media,
            )
        technical_media_error = any(
            isinstance(failure, dict)
            and str(failure.get("blocker") or "") in {
                BlockerCode.PROVIDER_ERROR.value,
                BlockerCode.INTERNAL_ERROR.value,
            }
            and str(failure.get("phase") or "") == Phase.MEDIA.value
            for failure in (validation or {}).get("failures", [])
        )
        if technical_media_error:
            effective_media = media
        # Only a completed acquisition/validation attempt consumes this budget.
        # Operational failures retain both progress and the existing counter.
        search_incomplete = (
            media_completed and self._media_attempt_incomplete(media)
        )
        media_attempt = media_completed and not technical_media_error and not search_incomplete
        progress = self._media_progressed(previous.media, effective_media) if media_attempt else False
        current_no_progress = (
            stage_error_no_progress
            if media_stage_error and outcome.blocker in MEDIA_BLOCKERS
            else (
                (0 if progress else media_base_no_progress + 1)
                if media_attempt else media_base_no_progress
            )
        )
        if media_completed and validation is not None:
            corrected = classify(previous, editorial, effective_media, validation, no_progress=current_no_progress, **self._policy())
            if corrected != outcome:
                outcome = corrected
                effective_media = self._reconcile_media_for_outcome(
                    media,
                    outcome,
                    validation,
                    previous_media=previous.media,
                )
        self.state_store.commit(
            post_id,
            self._next_state(
                previous,
                editorial,
                effective_media,
                outcome,
                no_progress=current_no_progress,
                media_reconciled=True,
            ),
        )
        return outcome

    @staticmethod
    def _media_progress(previous: WorkState, media: MediaProgress) -> MediaProgress:
        return media

    @staticmethod
    def _identity_verified(item: InlineMedia | FeaturedProgress | None) -> bool:
        if item is None:
            return False
        verification = item.visual_verification if isinstance(item.visual_verification, dict) else {}
        return bool(
            str(getattr(item, "sha256", "") or "")
            and str(getattr(item, "phash", "") or "")
            and str(getattr(item, "visual_group_id", "") or "")
            and str(verification.get("decision") or "") in {"INITIAL", "DIFFERENT"}
        )

    @staticmethod
    def _media_progressed(previous: MediaProgress, media: MediaProgress) -> bool:
        if media.valid_distinct_inline_count > previous.valid_distinct_inline_count:
            return True
        if (
            previous.featured.status is not FeaturedStatus.VALID
            and media.featured.status is FeaturedStatus.VALID
        ):
            return True
        if not PipelineRunner._identity_verified(previous.featured) and PipelineRunner._identity_verified(media.featured):
            return True
        previous_inline = {item.media_id: item for item in previous.inline}
        return any(
            PipelineRunner._identity_verified(item)
            and not PipelineRunner._identity_verified(previous_inline.get(item.media_id))
            for item in media.inline
        )

    @staticmethod
    def _reconcile_media_for_outcome(
        media: MediaProgress,
        outcome: Outcome,
        validation: dict[str, Any] | None = None,
        *,
        previous_media: MediaProgress | None = None,
    ) -> MediaProgress:
        """Remove progress that a validation blocker has explicitly invalidated.

        Keeping rejected assets makes the next media pass believe that the
        deficit is already satisfied.  Featured failures invalidate only the
        featured slot. Inline failures remove only assets with an explicit,
        matching identity; an ambiguous checklist never clears the baseline.
        ``INLINE_MISSING`` is intentionally preserved: it means the existing
        accepted assets are still valid and only more images are needed.
        """
        blocker = outcome.blocker
        validation_blockers = {
            mapped
            for failure in (validation or {}).get("failures", []) or []
            if isinstance(failure, dict)
            for mapped in [blocker_for_gate(failure.get("gate"))]
            if mapped is not None
        }
        featured_blockers = {
            BlockerCode.FEATURED_MISSING,
            BlockerCode.FEATURED_INVALID,
            BlockerCode.FEATURED_VISION,
        }
        active_featured = featured_blockers.intersection(
            {blocker} | validation_blockers
        )
        if active_featured:
            status = FeaturedStatus.MISSING
            if BlockerCode.FEATURED_VISION in active_featured:
                status = FeaturedStatus.VISION_REJECTED
            elif BlockerCode.FEATURED_INVALID in active_featured:
                status = FeaturedStatus.INVALID
            media = MediaProgress(
                required=media.required,
                inline=media.inline,
                featured=FeaturedProgress(status, None, None),
                search=media.search,
                enrichment_round=media.enrichment_round,
                waiver_applied=media.waiver_applied,
                waiver_reason=media.waiver_reason,
                layout_policy=media.layout_policy,
                closing_featured_repeat={},
                visual_identity_pending=media.visual_identity_pending,
            )

        inline_blockers = {
            BlockerCode.MEDIA_INVALID,
            BlockerCode.MEDIA_DUPLICATE,
            BlockerCode.MEDIA_ORIGIN,
        }
        active_inline = inline_blockers.intersection({blocker} | validation_blockers)
        if not active_inline:
            return media

        invalid_media: list[dict[str, Any]] = []
        for failure in (validation or {}).get("failures", []) or []:
            if not isinstance(failure, dict):
                continue
            if blocker_for_gate(failure.get("gate")) in inline_blockers:
                invalid_media.extend(
                    item for item in (failure.get("invalid_media") or [])
                    if isinstance(item, dict)
                )
        safe_previous_inline = previous_media.inline if previous_media is not None else media.inline
        if not invalid_media:
            # A blocker without an asset identity cannot authorize destructive
            # reconciliation. Preserve the previous baseline and retain only
            # newly acquired media that already carries a complete positive
            # identity. This avoids dropping verified progress when another
            # asset merely lacks a structured invalid_media descriptor.
            merged = list(safe_previous_inline)
            positions = {item.media_id: index for index, item in enumerate(merged)}
            for item in media.inline:
                if item.media_id in positions:
                    # A recovery pass may enrich an old checkpoint in place.
                    # Keep the previous identity when the new observation is
                    # inconclusive, but retain a complete positive identity.
                    if PipelineRunner._identity_verified(item):
                        merged[positions[item.media_id]] = item
                    continue
                if PipelineRunner._identity_verified(item):
                    merged.append(item)
            return MediaProgress(required=media.required, inline=tuple(merged), featured=media.featured, search=media.search, enrichment_round=media.enrichment_round, waiver_applied=media.waiver_applied, waiver_reason=media.waiver_reason, layout_policy=media.layout_policy, closing_featured_repeat={}, visual_identity_pending=media.visual_identity_pending)

        def same_asset(item: InlineMedia, invalid: dict[str, Any]) -> bool:
            if invalid.get("media_id") is not None:
                try:
                    return item.media_id == int(invalid["media_id"])
                except (TypeError, ValueError):
                    return False
            invalid_url = str(invalid.get("url") or invalid.get("media_url") or "").strip()
            if not invalid_url:
                # Slot is a placement ordinal, not an asset identity. It is
                # intentionally never sufficient for destructive cleanup.
                return False

            def normalize_url(value: str) -> str:
                parsed = urlsplit(value.strip())
                if not parsed.scheme or not parsed.netloc:
                    return value.strip()
                return urlunsplit((
                    parsed.scheme.lower(),
                    parsed.netloc.lower(),
                    parsed.path.rstrip("/") or "/",
                    parsed.query,
                    "",
                ))

            return normalize_url(item.media_url) == normalize_url(invalid_url)

        all_inline: dict[int, InlineMedia] = {
            item.media_id: item for item in (previous_media.inline if previous_media else ())
        }
        all_inline.update({item.media_id: item for item in media.inline})
        matched_any = any(
            same_asset(item, invalid)
            for item in all_inline.values()
            for invalid in invalid_media
        )
        if not matched_any:
            # An unrecognized descriptor is not a safe identity. Discard only
            # the current untrusted delta and preserve the previous baseline.
            return MediaProgress(required=media.required, inline=safe_previous_inline, featured=media.featured, search=media.search, enrichment_round=media.enrichment_round, waiver_applied=media.waiver_applied, waiver_reason=media.waiver_reason, layout_policy=media.layout_policy, closing_featured_repeat={}, visual_identity_pending=media.visual_identity_pending)
        remaining = tuple(
            item for item in all_inline.values()
            if not any(same_asset(item, invalid) for invalid in invalid_media)
        )
        return MediaProgress(required=media.required, inline=remaining, featured=media.featured, search=media.search, enrichment_round=media.enrichment_round, waiver_applied=media.waiver_applied, waiver_reason=media.waiver_reason, layout_policy=media.layout_policy, closing_featured_repeat={}, visual_identity_pending=media.visual_identity_pending)

    @staticmethod
    def _next_state(
        previous: WorkState,
        editorial: dict[str, Any],
        media: MediaProgress,
        outcome: Outcome,
        *,
        no_progress: int | None = None,
        validation: dict[str, Any] | None = None,
        media_reconciled: bool = False,
    ) -> WorkState:
        # run_one passes the effective media explicitly. Keeping the
        # compatibility default lets older direct callers still ask this
        # helper to reconcile raw media, without ever reconciling the same
        # result twice in the production path.
        progress = (
            PipelineRunner._media_progress(previous, media)
            if media_reconciled
            else PipelineRunner._reconcile_media_for_outcome(
                PipelineRunner._media_progress(previous, media),
                outcome,
                validation,
                previous_media=previous.media,
            )
        )
        target_phase = outcome.phase or previous.phase
        no_progress = previous.retry.no_progress if no_progress is None else no_progress
        if target_phase is not Phase.MEDIA:
            no_progress = 0
        phase_attempts = (
            previous.retry.phase_attempts + 1
            if target_phase is previous.phase
            else 1
        )
        if outcome.type is OutcomeType.READY:
            return WorkState(state=LifecycleState.READY, phase=Phase.VALIDATE, retry=previous.retry, relevance_approved=True, media=progress)
        if outcome.type is OutcomeType.SKIPPED:
            return WorkState(state=LifecycleState.SKIPPED, phase=Phase.RELEVANCE, retry=previous.retry, media=progress)
        if outcome.type is OutcomeType.HUMAN_REQUIRED:
            return WorkState(state=LifecycleState.HUMAN_REQUIRED, phase=target_phase, blocker=outcome.blocker, retry=RetryInfo(previous.retry.attempts, no_progress, outcome.next_at, CURRENT_RETRY_POLICY_VERSION, phase_attempts), relevance_approved=previous.relevance_approved, media=progress)
        return WorkState(
            state=LifecycleState.PENDING,
            phase=target_phase,
            blocker=outcome.blocker,
            retry=RetryInfo(previous.retry.attempts + 1, no_progress, outcome.next_at, CURRENT_RETRY_POLICY_VERSION, phase_attempts),
            relevance_approved=editorial_decision(editorial) == "process" or previous.relevance_approved,
            media=progress,
        )
