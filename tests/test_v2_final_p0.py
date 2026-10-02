from unicornio_editor.pipeline_v2.errors import StageError
from unicornio_editor.pipeline_v2.model import BlockerCode, FeaturedProgress, FeaturedStatus, InlineMedia, MediaProgress, OutcomeType, Phase, RetryInfo, WorkState
from unicornio_editor.pipeline_v2.classifier import classify
from unicornio_editor.pipeline_v2.runner import PipelineRunner


class Store:
    def __init__(self, state): self.state = state
    def load(self, post_id): return self.state
    def commit(self, post_id, state): self.state = state


def test_stage_error_preserves_declared_phase():
    state = WorkState(relevance_approved=True)
    outcome = classify(state, {"decision": "process"}, state.media, {"passed": False, "failures": [{"blocker": "provider_error", "phase": "editorial"}]})
    assert outcome.phase is Phase.EDITORIAL


def test_featured_status_is_closed_enum():
    assert FeaturedProgress("vision_rejected").status is FeaturedStatus.VISION_REJECTED


def test_retry_gets_cooldown_when_not_explicit():
    outcome = classify(WorkState(), {"decision": "process"}, {}, {"passed": False, "failures": [{"gate": "imagens_no_corpo"}]})
    assert outcome.type is OutcomeType.RETRY
    assert outcome.next_at is not None


def test_zero_is_a_valid_legacy_slot():
    assert InlineMedia(1, "u", 0).slot == 0


def test_compose_error_keeps_media_returned_by_media_stage():
    previous = WorkState(relevance_approved=True)
    store = Store(previous)
    progress = MediaProgress(4, (InlineMedia(101, "u", 0), InlineMedia(102, "u", 1), InlineMedia(103, "u", 2), InlineMedia(104, "u", 3)))
    stages = {
        "editorial": lambda *_: {"decision": "process"},
        "media": lambda *_: progress,
        "compose": lambda *_: (_ for _ in ()).throw(StageError(BlockerCode.PROVIDER_ERROR, Phase.COMPOSE, "compose timeout")),
        "validate": lambda *_: {},
    }
    PipelineRunner(store, stages).run_one(1, {})
    assert store.state.media == progress
    assert store.state.phase is Phase.COMPOSE
