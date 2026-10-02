from unicornio_editor.pipeline_v2.legacy import from_legacy_state
from unicornio_editor.pipeline_v2.model import BlockerCode, LifecycleState, Phase


def test_legacy_states_map_to_v2_lifecycle_and_phase():
    assert from_legacy_state({"state": "ready"}).state is LifecycleState.READY
    assert from_legacy_state({"state": "skipped"}).state is LifecycleState.SKIPPED
    assert from_legacy_state({"state": "published"}).state is LifecycleState.PUBLISHED
    assert from_legacy_state({"state": "awaiting_human"}).state is LifecycleState.HUMAN_REQUIRED


def test_legacy_partial_media_maps_to_pending_media_with_progress():
    state = from_legacy_state({
        "state": "partial",
        "partial_kind": "featured_vision",
        "partial_required": 4,
        "partial_completed": 4,
        "partial_missing": 0,
    }, inline_assets=[{"media_id": i, "media_url": f"u{i}", "slot": i} for i in range(1, 5)])
    assert state.state is LifecycleState.PENDING
    assert state.phase is Phase.MEDIA
    assert state.blocker is BlockerCode.FEATURED_VISION
    assert state.media.accepted == 4
    assert state.media.missing == 0


def test_legacy_uncertain_maps_to_pending_relevance():
    state = from_legacy_state({"state": "uncertain"})
    assert state.state is LifecycleState.PENDING
    assert state.phase is Phase.RELEVANCE
    assert state.blocker is BlockerCode.RELEVANCE_UNCERTAIN
