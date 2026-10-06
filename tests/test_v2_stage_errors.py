import pytest

from unicornio_editor.pipeline_v2.errors import StageError
from unicornio_editor.pipeline_v2.model import BlockerCode, InlineMedia, MediaProgress, OutcomeType, Phase, WorkState
from unicornio_editor.pipeline_v2.runner import PipelineRunner


class Store:
    def __init__(self): self.state = WorkState(relevance_approved=True); self.committed = None
    def load(self, post_id): return self.state
    def commit(self, post_id, state): self.state = state; self.committed = state


def test_expected_stage_error_becomes_retry_and_persists_cooldown_path():
    store = Store()
    stages = {
        "editorial": lambda context, state: {"decision": "process"},
        "media": lambda context, state, editorial: (_ for _ in ()).throw(StageError(BlockerCode.PROVIDER_ERROR, Phase.MEDIA, "provider timeout")),
        "compose": lambda *args: {},
        "validate": lambda *args: {"passed": True, "failures": []},
    }
    result = PipelineRunner(store, stages).run_one(1, {})
    assert result.type is OutcomeType.RETRY
    assert result.blocker is BlockerCode.PROVIDER_ERROR
    assert store.committed.blocker is BlockerCode.PROVIDER_ERROR


def test_technical_media_error_preserves_previous_accepted_media():
    previous = WorkState(
        phase=Phase.MEDIA,
        blocker=BlockerCode.MEDIA_INVALID,
        relevance_approved=True,
        media=MediaProgress(
            required=2,
            inline=(InlineMedia(17, "https://cdn.test/old.webp", 0),),
        ),
    )
    store = Store()
    store.state = previous
    stages = {
        "editorial": lambda *_: (_ for _ in ()).throw(AssertionError("editorial must not run")),
        "media": lambda *_: (_ for _ in ()).throw(
            StageError(BlockerCode.MEDIA_INVALID, Phase.MEDIA, "temporary media service error")
        ),
        "compose": lambda *args: {},
        "validate": lambda *args: {"passed": True, "failures": []},
    }

    PipelineRunner(store, stages).run_one(1, {"editorial": {"decision": "process"}})

    assert [item.media_id for item in store.state.media.inline] == [17]


def test_unexpected_stage_error_still_escapes_runner_for_session_isolation():
    store = Store()
    stages = {
        "editorial": lambda context, state: {"decision": "process"},
        "media": lambda context, state, editorial: (_ for _ in ()).throw(AssertionError("bug")),
        "compose": lambda *args: {},
        "validate": lambda *args: {},
    }
    with pytest.raises(AssertionError):
        PipelineRunner(store, stages).run_one(1, {})
