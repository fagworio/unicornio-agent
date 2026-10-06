"""V2 orchestration over injected stages; no provider or WP imports."""

from typing import Any, Callable

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
                "max_media_search_attempts",
                getattr(config, "max_partial_no_progress_attempts", 2),
            )),
            "max_rework_attempts": int(getattr(config, "max_rework_attempts", 3)),
            "cooldown_minutes": int(getattr(config, "rework_cooldown_minutes", 30)),
        }

    def run_one(self, post_id: int, context: dict[str, Any]) -> Outcome:
        previous = self.state_store.load(post_id)
        context = context if isinstance(context, dict) else {}
        editorial = context.get("editorial") or context.get("draft")
        run_editorial = previous.phase in {Phase.RELEVANCE, Phase.EDITORIAL}
        if run_editorial:
            try:
                editorial = self.stages["editorial"](context, previous)
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
        media: MediaProgress = previous.media
        media_completed = False
        media_stage_error = False
        validation: dict[str, Any] | None = None
        media_base_no_progress = (
            previous.retry.no_progress
            if previous.phase is Phase.MEDIA
            else 0
        )
        decision = editorial.get("decision")
        if decision in {"skip", "uncertain"}:
            outcome = classify(previous, editorial, previous.media, {"passed": False, "failures": []}, **self._policy())
        else:
            try:
                if previous.phase in {Phase.RELEVANCE, Phase.EDITORIAL, Phase.MEDIA}:
                    media = self.stages["media"](context, previous, editorial)
                    if not isinstance(media, MediaProgress):
                        raise TypeError("MediaStage must return MediaProgress")
                    media_completed = True
                if previous.phase is Phase.COMPOSE and not editorial:
                    raise StageError(BlockerCode.MANIFEST_INVALID, Phase.COMPOSE, "compose requires persisted editorial")
                if previous.phase is Phase.VALIDATE and not context.get("candidate"):
                    raise StageError(BlockerCode.MANIFEST_INVALID, Phase.VALIDATE, "validate requires persisted candidate")
                candidate = context.get("candidate") if previous.phase is Phase.VALIDATE else None
                if candidate is None and previous.phase is not Phase.VALIDATE:
                    candidate = self.stages["compose"](context, editorial, media)
                elif candidate is None:
                    candidate = context.get("draft") or {}
                validation = self.stages["validate"](context, candidate)
                progress = self._media_progressed(previous.media, media) if media_completed else False
                current_no_progress = (
                    (0 if progress else media_base_no_progress + 1)
                    if media_completed
                    else media_base_no_progress
                )
                outcome = classify(previous, editorial, media, validation, no_progress=current_no_progress, **self._policy())
            except StageError as exc:
                media_stage_error = exc.blocker in MEDIA_BLOCKERS and exc.phase is Phase.MEDIA
                outcome = classify_stage_error(
                    previous,
                    exc,
                    no_progress=media_base_no_progress + 1 if media_stage_error else None,
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
            effective_media = previous.media
        else:
            effective_media = self._reconcile_media_for_outcome(
                media,
                outcome,
                validation,
                previous_media=previous.media,
            )
        media_attempt = media_completed or (
            media_stage_error
            or (previous.phase is Phase.MEDIA and outcome.blocker in MEDIA_BLOCKERS)
        )
        progress = self._media_progressed(previous.media, effective_media) if media_attempt else False
        current_no_progress = (
            (0 if progress else media_base_no_progress + 1)
            if media_attempt else media_base_no_progress
        )
        if (
            media_attempt
            and outcome.type is OutcomeType.RETRY
            and current_no_progress >= self._policy()["max_media_no_progress"]
            and outcome.blocker in MEDIA_BLOCKERS
        ):
            outcome = Outcome.human_required(
                outcome.phase or Phase.MEDIA,
                outcome.blocker,
                detail=outcome.detail,
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
    def _media_progressed(previous: MediaProgress, media: MediaProgress) -> bool:
        return (
            media.accepted > previous.accepted
            or (
                previous.featured.status is not FeaturedStatus.VALID
                and media.featured.status is FeaturedStatus.VALID
            )
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
            # reconciliation. Keep the last known-good assets and discard only
            # the untrusted delta from this attempt.
            return MediaProgress(required=media.required, inline=safe_previous_inline, featured=media.featured)

        def same_asset(item: InlineMedia, invalid: dict[str, Any]) -> bool:
            if invalid.get("media_id") is not None:
                try:
                    if item.media_id == int(invalid["media_id"]):
                        return True
                except (TypeError, ValueError):
                    pass
            invalid_url = str(invalid.get("url") or invalid.get("media_url") or "").strip()
            if invalid_url and item.media_url.strip() == invalid_url:
                return True
            if invalid.get("slot") is not None:
                try:
                    if item.slot == int(invalid["slot"]):
                        return True
                except (TypeError, ValueError):
                    pass
            return False

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
            return MediaProgress(required=media.required, inline=safe_previous_inline, featured=media.featured)
        remaining = tuple(
            item for item in all_inline.values()
            if not any(same_asset(item, invalid) for invalid in invalid_media)
        )
        return MediaProgress(required=media.required, inline=remaining, featured=media.featured)

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
