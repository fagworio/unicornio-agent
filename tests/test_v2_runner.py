from unicornio_editor.pipeline_v2.model import BlockerCode, FeaturedProgress, FeaturedStatus, InlineMedia, LifecycleState, MediaProgress, MediaSearchProgress, Outcome, OutcomeType, Phase, RetryInfo, WorkState
from unicornio_editor.pipeline_v2.runner import PipelineRunner
from datetime import datetime, timedelta
from unicornio_editor.pipeline_v2.scheduler import cooldown_status


class MemoryStore:
    def __init__(self, state=None):
        self.state = state or WorkState()
        self.commits = []
    def load(self, post_id):
        return self.state
    def commit(self, post_id, state):
        self.state = state
        self.commits.append((post_id, state))


def test_runner_rejects_invalid_keyword_before_media():
    store = MemoryStore()
    calls = []
    previous = WorkState(
        phase=Phase.EDITORIAL,
        media=MediaProgress(required=2, inline=(InlineMedia(10, "https://example.test/a.webp", 0),)),
    )
    store.state = previous
    stages = {
        "editorial": lambda context, state: {
            "decision": "process",
            "seo": {"title": "Título sobre Android", "focus_keyword": "termo ausente"},
            "cleaned_html": "<p>Texto sobre Android.</p>",
        },
        "media": lambda *args: calls.append("media"),
        "compose": lambda *args: calls.append("compose"),
        "validate": lambda *args: calls.append("validate"),
    }
    result = PipelineRunner(store, stages).run_one(115524, {})
    assert result.type is OutcomeType.RETRY
    assert result.phase is Phase.EDITORIAL
    assert result.blocker is BlockerCode.TEXT_QUALITY
    assert "focus keyword" in (result.detail or "")
    assert "title_match=false" in (result.detail or "")
    assert "content_match=false" in (result.detail or "")
    assert calls == []
    assert store.state.media == previous.media


def test_runner_rejects_missing_keyword_before_media():
    store = MemoryStore()
    calls = []
    stages = {
        "editorial": lambda context, state: {
            "decision": "process",
            "seo": {"title": "Título incompleto", "meta_description": "Descrição"},
            "cleaned_html": "<p>Texto editorial.</p>",
        },
        "media": lambda *args: calls.append("media"),
        "compose": lambda *args: calls.append("compose"),
        "validate": lambda *args: calls.append("validate"),
    }
    result = PipelineRunner(store, stages).run_one(115525, {})
    assert result.type is OutcomeType.RETRY
    assert result.phase is Phase.EDITORIAL
    assert result.blocker is BlockerCode.TEXT_QUALITY
    assert result.detail == "focus keyword is required (title_match=false, content_match=false)"
    assert calls == []




def test_runner_ignores_keyword_found_only_in_html_attributes():
    store = MemoryStore()
    calls = []
    stages = {
        "editorial": lambda context, state: {
            "decision": "process",
            "seo": {"title": "Guia Android", "focus_keyword": "Android"},
            "cleaned_html": '<p>Texto editorial.</p><img alt="Android">',
        },
        "media": lambda *args: calls.append("media"),
        "compose": lambda *args: calls.append("compose"),
        "validate": lambda *args: calls.append("validate"),
    }
    result = PipelineRunner(store, stages).run_one(115526, {})
    assert result.type is OutcomeType.RETRY
    assert result.blocker is BlockerCode.TEXT_QUALITY
    assert "content_match=false" in (result.detail or "")
    assert calls == []


def test_runner_processes_one_post_and_persists_ready():
    store = MemoryStore()
    calls = []
    context = {"title": "test"}
    stages = {
        "editorial": lambda context, state: {"decision": "process"},
        "media": lambda context, state, editorial: (calls.append("media") or MediaProgress()),
        "compose": lambda context, editorial, media: (calls.append("compose") or {}),
        "validate": lambda context, candidate: {"passed": True, "failures": []},
    }
    result = PipelineRunner(store, stages).run_one(114859, context)
    assert result.type.value == "ready"
    assert calls == ["media", "compose"]
    assert store.state.state.value == "ready"
    assert store.commits[-1][0] == 114859
    assert set(context["_v2_stage_durations_ms"]) == {"editorial", "media", "compose", "validate"}


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


def test_identity_recovery_counts_as_progress_once():
    legacy = InlineMedia(10, "https://example.test/a.webp", 0)
    recovered = InlineMedia(
        10,
        "https://example.test/a.webp",
        0,
        sha256="a" * 64,
        phash="1" * 16,
        visual_group_id="visual:" + "a" * 24,
        visual_verification={"decision": "INITIAL"},
    )
    previous = MediaProgress(required=1, inline=(legacy,))
    assert PipelineRunner._media_progressed(previous, MediaProgress(required=1, inline=(recovered,)))
    assert not PipelineRunner._media_progressed(
        MediaProgress(required=1, inline=(recovered,)),
        MediaProgress(required=1, inline=(recovered,)),
    )


def test_reconciliation_keeps_enriched_identity_for_existing_media():
    legacy = InlineMedia(10, "https://example.test/a.webp", 0)
    recovered = InlineMedia(
        10,
        "https://example.test/a.webp",
        0,
        sha256="a" * 64,
        phash="1" * 16,
        visual_group_id="visual:" + "a" * 24,
        visual_verification={"decision": "DIFFERENT"},
    )
    outcome = Outcome(OutcomeType.RETRY, Phase.MEDIA, BlockerCode.MEDIA_INVALID)
    validation = {"failures": [{"gate": "imagens_no_corpo"}]}
    result = PipelineRunner._reconcile_media_for_outcome(
        MediaProgress(required=1, inline=(recovered,)),
        outcome,
        validation,
        previous_media=MediaProgress(required=1, inline=(legacy,)),
    )
    assert result.inline == (recovered,)


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


def test_incomplete_media_search_does_not_exhaust_no_progress_budget():
    previous = WorkState(
        phase=Phase.MEDIA,
        retry=RetryInfo(no_progress=2),
        relevance_approved=True,
        media=MediaProgress(
            required=2,
            search=MediaSearchProgress(
                completed=False,
                queries_attempted=1,
                queries_planned=3,
                queries_completed=1,
                completion_reason="SOURCE_VERIFICATION_INCOMPLETE",
            ),
        ),
    )
    store = MemoryStore(previous)
    stages = {
        "editorial": lambda *_: (_ for _ in ()).throw(AssertionError("editorial must not run")),
        "media": lambda *_: previous.media,
        "compose": lambda *_: {},
        "validate": lambda *_: {
            "passed": False,
            "failures": [{"gate": "imagens_no_corpo"}],
        },
    }

    outcome = PipelineRunner(store, stages).run_one(115633, {"editorial": {"decision": "process"}})

    assert outcome.type is OutcomeType.RETRY
    assert outcome.phase is Phase.MEDIA
    assert outcome.blocker is BlockerCode.PROVIDER_ERROR
    assert store.state.state is not LifecycleState.HUMAN_REQUIRED
    assert store.state.retry.no_progress == 2
    assert store.state.media.search.completion_reason == "SOURCE_VERIFICATION_INCOMPLETE"


def test_retryable_article_source_failure_does_not_consume_media_no_progress():
    previous = WorkState(
        phase=Phase.MEDIA,
        retry=RetryInfo(no_progress=2),
        relevance_approved=True,
        media=MediaProgress(required=2),
    )
    store = MemoryStore(previous)
    media = MediaProgress(
        required=2,
        search=MediaSearchProgress(
            completed=True,
            queries_attempted=1,
            queries_planned=1,
            queries_completed=1,
            completion_reason="EXHAUSTED_VERIFIED",
            coverage={
                "source_discovery": {
                    "status": "unavailable",
                    "reason_code": "source_page_unavailable",
                    "retryable": True,
                },
            },
        ),
    )
    stages = {
        "editorial": lambda *_: (_ for _ in ()).throw(AssertionError("editorial must not run")),
        "media": lambda *_: media,
        "compose": lambda *_: {},
        "validate": lambda *_: {
            "passed": False,
            "failures": [{"gate": "imagens_no_corpo"}],
        },
    }

    outcome = PipelineRunner(store, stages).run_one(
        115633, {"editorial": {"decision": "process"}}
    )

    assert outcome.type is OutcomeType.RETRY
    assert outcome.phase is Phase.MEDIA
    assert outcome.blocker is BlockerCode.PROVIDER_ERROR
    assert "source_page_unavailable" in (outcome.detail or "")
    assert store.state.retry.no_progress == 2


def test_provider_retry_preserves_three_images_and_next_run_requests_only_one():
    existing = tuple(
        InlineMedia(
            media_id=index,
            media_url=f"https://cdn.test/{index}.webp",
            slot=(index - 1) * 3,
            subject="Metroid Prime 4",
            sha256=f"{index:x}" * 64,
            phash=f"{index:016x}",
            visual_group_id=f"visual:{index:024x}",
            visual_verification={"decision": "INITIAL"},
        )
        for index in range(1, 4)
    )
    initial = WorkState(
        state=LifecycleState.PENDING,
        phase=Phase.MEDIA,
        blocker=BlockerCode.PROVIDER_ERROR,
        retry=RetryInfo(attempts=3, no_progress=1),
        relevance_approved=True,
        media=MediaProgress(required=4, inline=existing),
    )
    store = MemoryStore(initial)
    requested_deficits = []
    healthy = False

    def media_stage(_context, state, _editorial):
        nonlocal healthy
        requested_deficits.append(state.media.effective_missing)
        assert state.media.inline == existing
        if not healthy:
            return MediaProgress(
                required=4,
                inline=state.media.inline,
                search=MediaSearchProgress(
                    completed=False,
                    queries_attempted=1,
                    queries_planned=1,
                    queries_completed=0,
                    completion_reason="PROVIDER_ERROR",
                ),
            )
        new_image = InlineMedia(
            media_id=4,
            media_url="https://cdn.test/4.webp",
            slot=9,
            subject="Metroid Prime 4",
            sha256="4" * 64,
            phash="0000000000000004",
            visual_group_id="visual:000000000000000000000004",
            visual_verification={"decision": "DIFFERENT"},
        )
        return MediaProgress(
            required=4,
            inline=(*state.media.inline, new_image),
            search=MediaSearchProgress(
                completed=True,
                queries_attempted=1,
                queries_planned=1,
                queries_completed=1,
                completion_reason="TARGET_REACHED",
            ),
        )

    stages = {
        "editorial": lambda *_: (_ for _ in ()).throw(AssertionError("editorial must not rerun")),
        "media": media_stage,
        "compose": lambda *_: {"content": "article"},
        "validate": lambda *_: (
            {"passed": not healthy, "failures": [{"gate": "imagens_no_corpo"}]}
            if not healthy else {"passed": True, "failures": []}
        ),
    }
    runner = PipelineRunner(store, stages)
    first = runner.run_one(42, {"editorial": {"decision": "process"}})

    assert first.type is OutcomeType.RETRY
    assert store.state.state is LifecycleState.PENDING
    assert [item.media_id for item in store.state.media.inline] == [1, 2, 3]
    assert store.state.retry.no_progress == 1
    assert store.state.retry.next_at
    active_now = datetime.fromisoformat(store.state.retry.next_at) - timedelta(seconds=1)
    assert cooldown_status(store.state.retry.next_at, now=active_now)["active"]

    healthy = True
    second = runner.run_one(42, {"editorial": {"decision": "process"}})

    assert second.type is OutcomeType.READY
    assert requested_deficits == [1, 1]
    assert [item.media_id for item in store.state.media.inline] == [1, 2, 3, 4]


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


def test_reconciliation_preserves_new_verified_media_without_descriptor():
    first = InlineMedia(10, "https://example.test/first.webp", 0)
    second = InlineMedia(
        11, "https://example.test/second.webp", 3,
        phash="phash-11", sha256="sha-11", visual_group_id="visual:11",
        visual_verification={"decision": "DIFFERENT", "method": "test"},
    )
    previous = MediaProgress(required=2, inline=(first,))
    current = MediaProgress(required=2, inline=(first, second))
    outcome = Outcome(OutcomeType.RETRY, Phase.MEDIA, BlockerCode.MEDIA_INVALID)

    result = PipelineRunner._reconcile_media_for_outcome(
        current,
        outcome,
        {"failures": [{"gate": "visual_identity_verified"}]},
        previous_media=previous,
    )

    assert [item.media_id for item in result.inline] == [10, 11]


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
