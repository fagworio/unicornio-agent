from datetime import datetime, timezone

from unicornio_editor.pipeline_v2.classifier import classify
from unicornio_editor.pipeline_v2.model import BlockerCode, InlineMedia, MediaProgress, Phase, RetryInfo, WorkState
from unicornio_editor.pipeline_v2.runner import PipelineRunner
from unicornio_editor.pipeline_v2.scheduler import select


class Store:
    def __init__(self, state):
        self.state = state

    def load(self, post_id):
        return self.state

    def commit(self, post_id, state):
        self.state = state


def test_media_retry_does_not_call_editorial_stage():
    previous = WorkState(
        phase=Phase.MEDIA,
        blocker=BlockerCode.INLINE_MISSING,
        relevance_approved=True,
        media=MediaProgress(2),
    )
    calls = []
    stages = {
        "editorial": lambda *_: (_ for _ in ()).throw(AssertionError("editorial must not run")),
        "media": lambda context, state, editorial: (calls.append(("media", editorial)) or MediaProgress(2)),
        "compose": lambda context, editorial, media: {},
        "validate": lambda context, candidate: {"passed": False, "failures": [{"gate": "imagens_no_corpo"}]},
    }
    result = PipelineRunner(Store(previous), stages).run_one(42, {"editorial": {"decision": "process"}})
    assert result.blocker is BlockerCode.INLINE_MISSING
    assert calls == [("media", {"decision": "process"})]


def test_two_consecutive_media_no_progress_escalates_human():
    previous = WorkState(
        phase=Phase.MEDIA,
        blocker=BlockerCode.INLINE_MISSING,
        retry=RetryInfo(no_progress=1),
        relevance_approved=True,
        media=MediaProgress(2),
    )
    outcome = classify(
        previous,
        {"decision": "process"},
        MediaProgress(2),
        {"passed": False, "failures": [{"gate": "imagens_no_corpo"}]},
        no_progress=2,
    )
    assert outcome.type.value == "human_required"
    assert outcome.blocker is BlockerCode.INLINE_MISSING


def test_runner_counts_two_real_media_no_progress_attempts():
    store = Store(WorkState(phase=Phase.MEDIA, blocker=BlockerCode.INLINE_MISSING, relevance_approved=True, media=MediaProgress(2)))
    stages = {
        "editorial": lambda *_: (_ for _ in ()).throw(AssertionError("editorial must not run")),
        "media": lambda context, state, editorial: MediaProgress(2),
        "compose": lambda context, editorial, media: {},
        "validate": lambda context, candidate: {"passed": False, "failures": [{"gate": "imagens_no_corpo"}]},
    }
    runner = PipelineRunner(store, stages)
    context = {"editorial": {"decision": "process"}}
    first = runner.run_one(42, context)
    assert first.type.value == "retry"
    assert store.state.retry.no_progress == 1
    second = runner.run_one(42, context)
    assert second.type.value == "human_required"
    assert store.state.retry.no_progress == 2


def test_missing_persisted_artifact_fails_closed():
    store = Store(WorkState(phase=Phase.MEDIA, blocker=BlockerCode.INLINE_MISSING, relevance_approved=True, media=MediaProgress(2)))
    stages = {
        "editorial": lambda *_: (_ for _ in ()).throw(AssertionError("editorial must not run")),
        "media": lambda *_: (_ for _ in ()).throw(AssertionError("media must not run")),
        "compose": lambda *_: {},
        "validate": lambda *_: {"passed": True, "failures": []},
    }
    outcome = PipelineRunner(store, stages).run_one(42, {})
    assert outcome.blocker is BlockerCode.MANIFEST_INVALID


def test_old_new_post_gets_turn_with_limit_one():
    now = datetime(2026, 10, 5, tzinfo=timezone.utc)
    new = (1, {"date": "2026-10-01T00:00:00+00:00"})
    retry = (2, {})
    states = {
        1: WorkState(phase=Phase.RELEVANCE),
        2: WorkState(phase=Phase.MEDIA, blocker=BlockerCode.INLINE_MISSING, relevance_approved=True, media=MediaProgress(2, (InlineMedia(9, "u", 0),))),
    }
    selected = select([new, retry], StoreMap(states), limit=1, now=now)
    assert selected == [new]


class StoreMap:
    def __init__(self, states):
        self.states = states

    def load(self, post_id):
        return self.states[post_id]
