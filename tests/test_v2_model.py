import pytest

from unicornio_editor.pipeline_v2.model import (
    BlockerCode,
    FeaturedProgress,
    LifecycleState,
    MediaProgress,
    Outcome,
    OutcomeType,
    Phase,
    RetryInfo,
    WorkState,
)


def test_work_state_round_trips_v2_wire_format():
    state = WorkState(
        state=LifecycleState.PENDING,
        phase=Phase.MEDIA,
        blocker=BlockerCode.FEATURED_VISION,
        retry=RetryInfo(attempts=1, no_progress=0),
        relevance_approved=True,
        media=MediaProgress(
            required=4,
            accepted=4,
            featured=FeaturedProgress(status="vision_rejected", media_id=123),
        ),
    )

    restored = WorkState.from_dict(state.to_dict())

    assert restored == state
    assert state.to_dict()["version"] == 2
    assert state.to_dict()["media"]["inline"] == {
        "required": 4,
        "accepted": 4,
        "missing": 0,
    }


def test_work_state_rejects_invalid_lifecycle_and_progress():
    with pytest.raises(ValueError, match="accepted cannot exceed required"):
        MediaProgress(required=2, accepted=3)

    with pytest.raises(ValueError, match="READY requires"):
        WorkState(state=LifecycleState.READY, phase=Phase.MEDIA)


def test_outcome_has_only_classifier_results():
    assert {item.value for item in OutcomeType} == {
        "ready", "retry", "human_required", "skipped"
    }
    outcome = Outcome.retry(Phase.MEDIA, BlockerCode.INLINE_MISSING, next_at="2030-01-01T00:00:00Z")
    assert outcome.to_dict() == {
        "type": "retry",
        "phase": "media",
        "blocker": "inline_missing",
        "next_at": "2030-01-01T00:00:00Z",
    }
