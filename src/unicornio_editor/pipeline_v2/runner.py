"""V2 orchestration over injected stages; no provider or WP imports."""

from typing import Any, Callable

from .classifier import classify
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
        editorial = self.stages["editorial"](context, previous)
        media: dict[str, Any] = {}
        decision = editorial.get("decision")
        if decision in {"skip", "uncertain"}:
            outcome = classify(previous, editorial, {}, {"passed": False, "failures": []})
        else:
            media = self.stages["media"](context, previous, editorial)
            candidate = self.stages["compose"](context, editorial, media)
            validation = self.stages["validate"](context, candidate)
            outcome = classify(previous, editorial, media, validation)
        self.state_store.commit(post_id, self._next_state(previous, editorial, media, outcome))
        return outcome

    @staticmethod
    def _media_progress(previous: WorkState, media: dict[str, Any]) -> MediaProgress:
        inline = media.get("inline") if isinstance(media, dict) else None
        featured = media.get("featured") if isinstance(media, dict) else None
        if not isinstance(inline, dict):
            return previous.media
        required = int(inline.get("required", previous.media.required))
        accepted_raw = inline.get("accepted", [])
        accepted = accepted_raw if isinstance(accepted_raw, int) else len(accepted_raw or [])
        featured = featured if isinstance(featured, dict) else {}
        return MediaProgress(
            required=required,
            accepted=min(required, max(0, int(accepted))),
            featured=FeaturedProgress(
                status=str(featured.get("status", previous.media.featured.status)),
                media_id=featured.get("media_id", previous.media.featured.media_id),
                media_url=featured.get("media_url", previous.media.featured.media_url),
            ),
        )

    @staticmethod
    def _next_state(previous: WorkState, editorial: dict[str, Any], media: dict[str, Any], outcome: Outcome) -> WorkState:
        progress = PipelineRunner._media_progress(previous, media)
        if outcome.type is OutcomeType.READY:
            return WorkState(state=LifecycleState.READY, phase=Phase.VALIDATE, retry=previous.retry, relevance_approved=True, media=progress)
        if outcome.type is OutcomeType.SKIPPED:
            return WorkState(state=LifecycleState.SKIPPED, phase=Phase.RELEVANCE, retry=previous.retry, media=progress)
        if outcome.type is OutcomeType.HUMAN_REQUIRED:
            return WorkState(state=LifecycleState.HUMAN_REQUIRED, phase=outcome.phase or previous.phase, blocker=outcome.blocker, retry=previous.retry, relevance_approved=previous.relevance_approved, media=progress)
        return WorkState(
            state=LifecycleState.PENDING,
            phase=outcome.phase or previous.phase,
            blocker=outcome.blocker,
            retry=RetryInfo(previous.retry.attempts + 1, previous.retry.no_progress, outcome.next_at),
            relevance_approved=editorial.get("decision") == "process" or previous.relevance_approved,
            media=progress,
        )
