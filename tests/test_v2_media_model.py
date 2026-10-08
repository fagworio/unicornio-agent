from unicornio_editor.pipeline_v2.model import FeaturedProgress, InlineMedia, MediaProgress, MediaSearchProgress


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


def test_media_search_progress_round_trips_auditable_exhaustion():
    progress = MediaProgress(
        required=4,
        search=MediaSearchProgress(
            completed=True,
            exhausted=True,
            queries_attempted=2,
            engines_attempted=("bing", "yandex"),
            engines_disabled=("google_browser",),
            candidates_seen=8,
            candidates_rejected=7,
            distinct_valid_frames=1,
            queries_planned=3,
            queries_completed=3,
        ),
    )

    restored = MediaProgress.from_dict(progress.to_dict())

    assert restored.search == progress.search


def test_media_progress_round_trips_enrichment_round_and_waiver():
    progress = MediaProgress(
        required=4,
        enrichment_round=2,
        waiver_applied=True,
        waiver_reason="enrichment_retries_exhausted",
    )
    assert MediaProgress.from_dict(progress.to_dict()) == progress
