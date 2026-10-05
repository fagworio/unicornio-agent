"""V2 orchestration over injected stages; no provider or WP imports."""

from typing import Any, Callable

from .classifier import classify, classify_stage_error
from .errors import StageError
from .model import FeaturedProgress, LifecycleState, MediaProgress, Outcome, OutcomeType, Phase, RetryInfo, WorkState


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
        editorial = context.get("editorial") if isinstance(context, dict) else None
        run_editorial = previous.phase in {Phase.RELEVANCE, Phase.EDITORIAL}
        if run_editorial:
            try:
                editorial = self.stages["editorial"](context, previous)
            except StageError as exc:
                editorial = {}
                outcome = classify_stage_error(previous, exc)
                self.state_store.commit(post_id, self._next_state(previous, editorial, previous.media, outcome))
                return outcome
        editorial = editorial or {"decision": "process"}
        media: MediaProgress = previous.media
        decision = editorial.get("decision")
        if decision in {"skip", "uncertain"}:
            outcome = classify(previous, editorial, previous.media, {"passed": False, "failures": []})
        else:
            try:
                if previous.phase in {Phase.RELEVANCE, Phase.EDITORIAL, Phase.MEDIA}:
                    media = self.stages["media"](context, previous, editorial)
                    if not isinstance(media, MediaProgress):
                        raise TypeError("MediaStage must return MediaProgress")
                candidate = context.get("candidate") if previous.phase is Phase.VALIDATE else None
                if candidate is None and previous.phase is not Phase.VALIDATE:
                    candidate = self.stages["compose"](context, editorial, media)
                elif candidate is None:
                    candidate = context.get("draft") or {}
                validation = self.stages["validate"](context, candidate)
                outcome = classify(previous, editorial, media, validation)
            except StageError as exc:
                outcome = classify_stage_error(previous, exc)
        self.state_store.commit(post_id, self._next_state(previous, editorial, media, outcome))
        return outcome

    @staticmethod
    def _media_progress(previous: WorkState, media: MediaProgress) -> MediaProgress:
        return media

    @staticmethod
    def _next_state(previous: WorkState, editorial: dict[str, Any], media: MediaProgress, outcome: Outcome) -> WorkState:
        progress = PipelineRunner._media_progress(previous, media)
        media_progressed = (
            media.accepted > previous.media.accepted
            or media.featured.status != previous.media.featured.status
            or media.featured.media_id != previous.media.featured.media_id
        )
        no_progress = 0 if media_progressed else previous.retry.no_progress
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
            relevance_approved=editorial.get("decision") == "process" or previous.relevance_approved,
            media=progress,
        )
