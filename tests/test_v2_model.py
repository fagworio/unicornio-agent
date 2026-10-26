import pytest

from unicornio_editor.pipeline_v2.model import (
    BlockerCode, FeaturedProgress, InlineMedia, LifecycleState, MediaProgress,
    Outcome, OutcomeType, Phase, RetryInfo, WorkState,
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
            inline=tuple(InlineMedia(i, f"https://wp.test/{i}.webp", i) for i in range(1, 5)),
            featured=FeaturedProgress(status="vision_rejected", media_id=123),
        ),
    )

    restored = WorkState.from_dict(state.to_dict())

    assert restored == state
    assert state.to_dict()["version"] == 2
    assert [item["media_id"] for item in state.to_dict()["media"]["inline"]["accepted"]] == [1, 2, 3, 4]


def test_inline_media_round_trips_persisted_phash():
    item = InlineMedia(101, "https://wp.test/101.webp", 1, phash="0123abcd")
    restored = InlineMedia.from_dict(item.to_dict())
    assert restored.phash == "0123abcd"


def test_featured_repeat_evidence_round_trips_without_changing_legacy_defaults():
    featured = FeaturedProgress(
        status="valid", media_id=7, media_url="https://wp.test/featured.webp",
        sha256="f" * 64, phash="f" * 16, visual_group_id="visual:featured",
        visual_verification={"decision": "INITIAL"}, alt_text="Key art",
        credit_text="Crédito da imagem: Fonte oficial", width=1280, height=720,
    )
    marker = {
        "role": "closing_featured_repeat", "placement": "after_editorial_body",
        "media_id": 7, "body_sha256": "body", "placement_plan_sha256": "plan",
    }
    state = WorkState(media=MediaProgress(featured=featured, closing_featured_repeat=marker))
    restored = WorkState.from_dict(state.to_dict())
    assert restored == state
    legacy = FeaturedProgress.from_dict({"status": "valid", "media_id": 7})
    assert legacy.alt_text == legacy.credit_text == ""
    assert legacy.width == legacy.height == 0


def test_work_state_allows_valid_assets_above_current_requirement():
    progress = MediaProgress(required=2, inline=tuple(InlineMedia(i, f"https://wp.test/{i}.webp", i) for i in (1, 2, 3)))
    assert progress.accepted == 3
    assert progress.missing == 0

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
