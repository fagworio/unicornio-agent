"""V2 orchestration over injected stages; no provider or WP imports."""

from typing import Any, Callable

from .classifier import blocker_for_gate, classify, classify_stage_error, editorial_decision
from .errors import StageError
from .model import BlockerCode, FeaturedProgress, FeaturedStatus, LifecycleState, MediaProgress, Outcome, OutcomeType, Phase, RetryInfo, WorkState


class PipelineRunner:
    def __init__(self, state_store: Any, stages: dict[str, Callable[..., dict[str, Any]]]):
        required = {"editorial", "media", "compose", "validate"}
        missing = required - set(stages)
        if missing:
            raise ValueError(f"missing stages: {sorted(missing)}")
        self.state_store = state_store
        self.stages = stages

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
                outcome = classify_stage_error(previous, exc)
                self.state_store.commit(post_id, self._next_state(previous, editorial, previous.media, outcome))
                return outcome
        if not isinstance(editorial, dict):
            editorial = {}
        if previous.phase in {Phase.MEDIA, Phase.COMPOSE, Phase.VALIDATE} and not editorial:
            outcome = classify_stage_error(
                previous,
                StageError(BlockerCode.MANIFEST_INVALID, previous.phase, "persisted editorial draft is missing"),
            )
            self.state_store.commit(post_id, self._next_state(previous, editorial, previous.media, outcome))
            return outcome
        if previous.phase in {Phase.RELEVANCE, Phase.EDITORIAL} and not editorial:
            outcome = classify_stage_error(
                previous,
                StageError(BlockerCode.MANIFEST_INVALID, previous.phase, "editorial stage returned no document"),
            )
            self.state_store.commit(post_id, self._next_state(previous, editorial, previous.media, outcome))
            return outcome
        media: MediaProgress = previous.media
        media_completed = False
        validation: dict[str, Any] | None = None
        decision = editorial.get("decision")
        if decision in {"skip", "uncertain"}:
            outcome = classify(previous, editorial, previous.media, {"passed": False, "failures": []})
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
                current_no_progress = (0 if progress else previous.retry.no_progress + 1) if media_completed else previous.retry.no_progress
                outcome = classify(previous, editorial, media, validation, no_progress=current_no_progress)
            except StageError as exc:
                outcome = classify_stage_error(previous, exc)
        progress = self._media_progressed(previous.media, media) if media_completed else False
        current_no_progress = (0 if progress else previous.retry.no_progress + 1) if media_completed else previous.retry.no_progress
        self.state_store.commit(
            post_id,
            self._next_state(
                previous,
                editorial,
                media,
                outcome,
                no_progress=current_no_progress,
                validation=validation,
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
            or media.featured.status != previous.featured.status
            or media.featured.media_id != previous.featured.media_id
        )

    @staticmethod
    def _reconcile_media_for_outcome(
        media: MediaProgress,
        outcome: Outcome,
        validation: dict[str, Any] | None = None,
    ) -> MediaProgress:
        """Remove progress that a validation blocker has explicitly invalidated.

        Keeping rejected assets makes the next media pass believe that the
        deficit is already satisfied.  Featured failures invalidate only the
        featured slot; generic inline media failures conservatively clear all
        inline assets because the checklist does not identify a safe asset id.
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
        if not invalid_media:
            # Stage errors and legacy checklist payloads have no safe identity;
            # preserve no known-invalid inline asset rather than looping on it.
            return MediaProgress(required=media.required, inline=(), featured=media.featured)

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

        remaining = tuple(
            item for item in media.inline
            if not any(same_asset(item, invalid) for invalid in invalid_media)
        )
        if len(remaining) == len(media.inline):
            remaining = ()
        return MediaProgress(required=media.required, inline=remaining, featured=media.featured)

    @staticmethod
    def _next_state(previous: WorkState, editorial: dict[str, Any], media: MediaProgress, outcome: Outcome, *, no_progress: int | None = None, validation: dict[str, Any] | None = None) -> WorkState:
        progress = PipelineRunner._reconcile_media_for_outcome(
            PipelineRunner._media_progress(previous, media), outcome, validation
        )
        no_progress = previous.retry.no_progress if no_progress is None else no_progress
        if outcome.type is OutcomeType.READY:
            return WorkState(state=LifecycleState.READY, phase=Phase.VALIDATE, retry=previous.retry, relevance_approved=True, media=progress)
        if outcome.type is OutcomeType.SKIPPED:
            return WorkState(state=LifecycleState.SKIPPED, phase=Phase.RELEVANCE, retry=previous.retry, media=progress)
        if outcome.type is OutcomeType.HUMAN_REQUIRED:
            return WorkState(state=LifecycleState.HUMAN_REQUIRED, phase=outcome.phase or previous.phase, blocker=outcome.blocker, retry=RetryInfo(previous.retry.attempts, no_progress, outcome.next_at), relevance_approved=previous.relevance_approved, media=progress)
        return WorkState(
            state=LifecycleState.PENDING,
            phase=outcome.phase or previous.phase,
            blocker=outcome.blocker,
            retry=RetryInfo(previous.retry.attempts + 1, no_progress, outcome.next_at),
            relevance_approved=editorial_decision(editorial) == "process" or previous.relevance_approved,
            media=progress,
        )
