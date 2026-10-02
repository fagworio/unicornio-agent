from unicornio_editor.pipeline_v2.model import FeaturedProgress, InlineMedia, MediaProgress


def test_media_progress_round_trips_inline_identity_and_counts_from_assets():
    progress = MediaProgress(
        required=4,
        inline=(
            InlineMedia(101, "https://wp.test/101.webp", 1),
            InlineMedia(102, "https://wp.test/102.webp", 2),
            InlineMedia(103, "https://wp.test/103.webp", 3),
        ),
        featured=FeaturedProgress("valid", 201, "https://wp.test/201.webp"),
    )
    restored = MediaProgress.from_dict(progress.to_dict())
    assert restored == progress
    assert restored.accepted == 3
    assert restored.missing == 1
    assert [asset.media_id for asset in restored.inline] == [101, 102, 103]


def test_media_progress_rejects_duplicate_media_or_slots():
    try:
        MediaProgress(2, (InlineMedia(101, "u", 1), InlineMedia(101, "u2", 2)))
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate media must be rejected")
