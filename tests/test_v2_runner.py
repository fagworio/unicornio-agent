from unicornio_editor.pipeline_v2.model import BlockerCode, FeaturedProgress, FeaturedStatus, InlineMedia, MediaProgress, Outcome, OutcomeType, Phase, WorkState
from unicornio_editor.pipeline_v2.runner import PipelineRunner


class MemoryStore:
    def __init__(self, state=None):
        self.state = state or WorkState()
        self.commits = []
    def load(self, post_id):
        return self.state
    def commit(self, post_id, state):
        self.state = state
        self.commits.append((post_id, state))


def test_runner_processes_one_post_and_persists_ready():
    store = MemoryStore()
    calls = []
    stages = {
        "editorial": lambda context, state: {"decision": "process"},
        "media": lambda context, state, editorial: (calls.append("media") or MediaProgress()),
        "compose": lambda context, editorial, media: (calls.append("compose") or {}),
        "validate": lambda context, candidate: {"passed": True, "failures": []},
    }
    result = PipelineRunner(store, stages).run_one(114859, {"title": "test"})
    assert result.type.value == "ready"
    assert calls == ["media", "compose"]
    assert store.state.state.value == "ready"
    assert store.commits[-1][0] == 114859


def test_runner_persists_retry_without_running_later_stages_for_skip():
    store = MemoryStore()
    stages = {
        "editorial": lambda context, state: {"decision": "skip"},
        "media": lambda *args: (_ for _ in ()).throw(AssertionError("media must not run")),
        "compose": lambda *args: (_ for _ in ()).throw(AssertionError("compose must not run")),
        "validate": lambda *args: (_ for _ in ()).throw(AssertionError("validate must not run")),
    }
    result = PipelineRunner(store, stages).run_one(1, {})
    assert result.type.value == "skipped"
    assert store.state.state.value == "skipped"


def test_runner_exposes_media_blocker_as_pending_retry():
    store = MemoryStore()
    stages = {
        "editorial": lambda context, state: {"decision": "process"},
        "media": lambda context, state, editorial: MediaProgress(),
        "compose": lambda context, editorial, media: {},
        "validate": lambda context, candidate: {"passed": False, "failures": [{"gate": "imagens_visao"}]},
    }
    result = PipelineRunner(store, stages).run_one(114849, {})
    assert result.blocker is BlockerCode.FEATURED_VISION
    assert store.state.phase is Phase.MEDIA
    assert store.state.state is WorkState().state


def test_runner_clears_inline_progress_after_media_rejection():
    inline = InlineMedia(10, "https://example.test/a.webp", 0)
    previous = WorkState(media=MediaProgress(required=2, inline=(inline,)))
    outcome = Outcome(OutcomeType.RETRY, Phase.MEDIA, BlockerCode.MEDIA_INVALID)
    state = PipelineRunner._next_state(previous, {}, previous.media, outcome)
    assert state.media.inline == ()
    assert state.media.required == 2


def test_runner_invalidates_only_featured_after_featured_vision_rejection():
    inline = InlineMedia(10, "https://example.test/a.webp", 0)
    featured = FeaturedProgress(FeaturedStatus.VALID, 20, "https://example.test/f.webp")
    previous = WorkState(media=MediaProgress(required=2, inline=(inline,), featured=featured))
    outcome = Outcome(OutcomeType.RETRY, Phase.MEDIA, BlockerCode.FEATURED_VISION)
    state = PipelineRunner._next_state(previous, {}, previous.media, outcome)
    assert state.media.inline == (inline,)
    assert state.media.featured.status is FeaturedStatus.VISION_REJECTED
    assert state.media.featured.media_id is None


def test_runner_preserves_inline_progress_for_inline_missing():
    inline = InlineMedia(10, "https://example.test/a.webp", 0)
    previous = WorkState(media=MediaProgress(required=2, inline=(inline,)))
    outcome = Outcome(OutcomeType.RETRY, Phase.MEDIA, BlockerCode.INLINE_MISSING)
    state = PipelineRunner._next_state(previous, {}, previous.media, outcome)
    assert state.media.inline == (inline,)


def test_runner_removes_only_structured_invalid_inline_media():
    inline = tuple(
        InlineMedia(i, f"https://example.test/{i}.webp", i * 3)
        for i in (1, 2, 3, 4)
    )
    previous = WorkState(media=MediaProgress(required=4, inline=inline))
    outcome = Outcome(OutcomeType.RETRY, Phase.MEDIA, BlockerCode.MEDIA_INVALID)
    validation = {
        "failures": [{
            "gate": "relevancia_imagens",
            "invalid_media": [{"media_id": 3, "url": "https://example.test/3.webp", "slot": 9}],
        }],
    }
    state = PipelineRunner._next_state(previous, {}, previous.media, outcome, validation=validation)
    assert [item.media_id for item in state.media.inline] == [1, 2, 4]
    assert state.media.missing == 1
