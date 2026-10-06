from unicornio_editor.pipeline_v2.model import BlockerCode, FeaturedProgress, FeaturedStatus, InlineMedia, MediaProgress, Outcome, OutcomeType, Phase, RetryInfo, WorkState
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


def test_runner_preserves_inline_progress_after_unidentified_media_rejection():
    inline = InlineMedia(10, "https://example.test/a.webp", 0)
    previous = WorkState(media=MediaProgress(required=2, inline=(inline,)))
    outcome = Outcome(OutcomeType.RETRY, Phase.MEDIA, BlockerCode.MEDIA_INVALID)
    state = PipelineRunner._next_state(previous, {}, previous.media, outcome)
    assert state.media.inline == (inline,)
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


def test_featured_invalidations_are_not_counted_as_media_progress():
    previous = MediaProgress(
        required=1,
        featured=FeaturedProgress(FeaturedStatus.VALID, 20, "https://example.test/f.webp"),
    )
    invalid = MediaProgress(
        required=1,
        featured=FeaturedProgress(FeaturedStatus.INVALID),
    )
    assert not PipelineRunner._media_progressed(previous, invalid)


def test_phase_attempts_reset_when_retry_changes_phase():
    previous = WorkState(
        phase=Phase.MEDIA,
        retry=RetryInfo(attempts=7, phase_attempts=4),
        relevance_approved=True,
    )
    outcome = Outcome(OutcomeType.RETRY, Phase.EDITORIAL, BlockerCode.TEXT_QUALITY)
    state = PipelineRunner._next_state(
        previous,
        {"decision": "process"},
        previous.media,
        outcome,
        media_reconciled=True,
    )
    assert state.retry.attempts == 8
    assert state.retry.phase_attempts == 1
    assert state.retry.policy_version == 3


def test_media_no_progress_from_editorial_starts_a_fresh_media_budget():
    inline = InlineMedia(10, "https://example.test/a.webp", 0)
    previous = WorkState(
        phase=Phase.EDITORIAL,
        retry=RetryInfo(no_progress=1),
        relevance_approved=True,
        media=MediaProgress(required=2, inline=(inline,)),
    )
    store = MemoryStore(previous)
    stages = {
        "editorial": lambda *_: {"decision": "process"},
        "media": lambda *_: previous.media,
        "compose": lambda *_: {},
        "validate": lambda *_: {"passed": False, "failures": [{"gate": "imagens_no_corpo"}]},
    }

    outcome = PipelineRunner(store, stages).run_one(1, {})

    assert outcome.type is OutcomeType.RETRY
    assert store.state.phase is Phase.MEDIA
    assert store.state.retry.no_progress == 1


def test_media_no_progress_continues_only_within_media_phase():
    previous = WorkState(
        phase=Phase.MEDIA,
        retry=RetryInfo(no_progress=1),
        relevance_approved=True,
        media=MediaProgress(required=2),
    )
    store = MemoryStore(previous)
    stages = {
        "editorial": lambda *_: (_ for _ in ()).throw(AssertionError("editorial must not run")),
        "media": lambda *_: previous.media,
        "compose": lambda *_: {},
        "validate": lambda *_: {"passed": False, "failures": [{"gate": "imagens_no_corpo"}]},
    }

    outcome = PipelineRunner(store, stages).run_one(1, {"editorial": {"decision": "process"}})

    assert outcome.type is OutcomeType.HUMAN_REQUIRED
    assert store.state.retry.no_progress == 2


def test_media_no_progress_resets_when_final_outcome_is_editorial():
    previous = WorkState(
        phase=Phase.MEDIA,
        retry=RetryInfo(no_progress=1),
        relevance_approved=True,
        media=MediaProgress(required=2),
    )
    store = MemoryStore(previous)
    stages = {
        "editorial": lambda *_: (_ for _ in ()).throw(AssertionError("editorial must not run")),
        "media": lambda *_: previous.media,
        "compose": lambda *_: {},
        "validate": lambda *_: {"passed": False, "failures": [{"gate": "qualidade_texto"}]},
    }

    outcome = PipelineRunner(store, stages).run_one(1, {"editorial": {"decision": "process"}})

    assert outcome.type is OutcomeType.RETRY
    assert outcome.phase is Phase.EDITORIAL
    assert store.state.retry.no_progress == 0


def test_reconciliation_preserves_previous_when_invalid_media_has_no_identity():
    first = InlineMedia(10, "https://example.test/first.webp", 0)
    second = InlineMedia(11, "https://example.test/second.webp", 3)
    previous = MediaProgress(required=2, inline=(first,))
    current = MediaProgress(required=2, inline=(first, second))
    outcome = Outcome(OutcomeType.RETRY, Phase.MEDIA, BlockerCode.MEDIA_INVALID)

    result = PipelineRunner._reconcile_media_for_outcome(
        current,
        outcome,
        {"failures": [{"gate": "imagens_webp", "invalid_media": []}]},
        previous_media=previous,
    )

    assert result.inline == (first,)


def test_reconciliation_removes_only_explicitly_invalidated_asset():
    first = InlineMedia(10, "https://example.test/first.webp", 0)
    second = InlineMedia(11, "https://example.test/second.webp", 3)
    previous = MediaProgress(required=2, inline=(first,))
    current = MediaProgress(required=2, inline=(first, second))
    outcome = Outcome(OutcomeType.RETRY, Phase.MEDIA, BlockerCode.MEDIA_INVALID)

    removed_old = PipelineRunner._reconcile_media_for_outcome(
        current,
        outcome,
        {"failures": [{"gate": "imagens_webp", "invalid_media": [{"media_id": 10}]}]},
        previous_media=previous,
    )
    removed_new = PipelineRunner._reconcile_media_for_outcome(
        current,
        outcome,
        {"failures": [{"gate": "imagens_webp", "invalid_media": [{"media_id": 11}]}]},
        previous_media=previous,
    )

    assert [item.media_id for item in removed_old.inline] == [11]
    assert [item.media_id for item in removed_new.inline] == [10]


def test_reconciliation_removes_new_asset_by_url_and_preserves_previous():
    first = InlineMedia(10, "https://example.test/first.webp", 0)
    second = InlineMedia(11, "https://example.test/second.webp", 3)
    previous = MediaProgress(required=2, inline=(first,))
    current = MediaProgress(required=2, inline=(first, second))
    outcome = Outcome(OutcomeType.RETRY, Phase.MEDIA, BlockerCode.MEDIA_INVALID)

    result = PipelineRunner._reconcile_media_for_outcome(
        current,
        outcome,
        {"failures": [{"gate": "imagens_webp", "invalid_media": [{"url": "HTTPS://EXAMPLE.TEST/second.webp/"}]}]},
        previous_media=previous,
    )

    assert [item.media_id for item in result.inline] == [10]


def test_reconciliation_never_uses_slot_when_url_is_unknown():
    first = InlineMedia(10, "https://example.test/first.webp", 0)
    second = InlineMedia(11, "https://example.test/second.webp", 3)
    previous = MediaProgress(required=2, inline=(first,))
    current = MediaProgress(required=2, inline=(first, second))
    outcome = Outcome(OutcomeType.RETRY, Phase.MEDIA, BlockerCode.MEDIA_INVALID)

    result = PipelineRunner._reconcile_media_for_outcome(
        current,
        outcome,
        {"failures": [{"gate": "imagens_webp", "invalid_media": [{"url": "https://unknown.test/nope.webp", "slot": 0}]}]},
        previous_media=previous,
    )

    assert [item.media_id for item in result.inline] == [10]


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
