from unicornio_editor.pipeline_v2.media_accounting import account_visual_media, reconcile_working_visual_media
from unicornio_editor.pipeline_v2.model import InlineMedia, MediaProgress


def _asset(media_id, decision, sha, phash, group, **extra):
    return {
        "media_id": media_id,
        "media_url": f"https://media.test/{media_id}.webp",
        "sha256": sha,
        "phash": phash,
        "visual_group_id": group,
        "visual_verification": {"decision": decision, **extra},
    }


def test_115152_reference_counts_only_the_distinct_inline_asset():
    media = {
        "required": 4,
        "inline": [
            _asset(115171, "SAME_ART_CROP", "b" * 64, "2" * 16, "visual:" + "b" * 24, duplicate_of="115170"),
            _asset(115172, "SAME_ART_CROP", "c" * 64, "3" * 16, "visual:" + "c" * 24, duplicate_of="115170"),
            _asset(115173, "DIFFERENT", "d" * 64, "4" * 16, "visual:" + "d" * 24),
        ],
        "featured": {
            "status": "valid", "media_id": 115170,
            **_asset(115170, "INITIAL", "a" * 64, "1" * 16, "visual:" + "a" * 24),
        },
    }

    result = account_visual_media(media)

    assert result.accepted_working_count == 3
    assert result.valid_distinct_inline_count == 1
    assert result.confirmed_duplicate_count == 2
    assert result.effective_missing == 3
    assert result.valid_inline_ids == (115173,)
    assert result.duplicate_inline_ids == (115171, 115172)
    assert result.baseline_complete


def test_only_complete_positive_identity_counts_toward_quota():
    media = {
        "required": 4,
        "inline": [
            _asset(1, "DIFFERENT", "a" * 64, "1" * 16, "visual:a"),
            _asset(2, "UNVERIFIED", "b" * 64, "2" * 16, "visual:b"),
            _asset(3, "DIFFERENT", "", "3" * 16, "visual:c"),
        ],
    }

    result = account_visual_media(media)

    assert result.valid_distinct_inline_count == 1
    assert result.unverified_count == 2
    assert result.effective_missing == 3
    assert not result.baseline_complete


def test_repeated_identity_group_cannot_inflate_visual_coverage():
    result = account_visual_media({
        "required": 4,
        "inline": [
            _asset(1, "INITIAL", "a" * 64, "1" * 16, "visual:a"),
            _asset(2, "DIFFERENT", "b" * 64, "2" * 16, "visual:a"),
            _asset(3, "DIFFERENT", "c" * 64, "3" * 16, "visual:c"),
            _asset(4, "DIFFERENT", "d" * 64, "4" * 16, "visual:d"),
        ],
    })

    assert result.valid_distinct_inline_count == 3
    assert result.duplicate_inline_ids == (2,)
    assert result.effective_missing == 1


def test_listicle_missing_items_are_derived_from_verified_media_only():
    result = account_visual_media(
        {
            "required": 2,
            "inline": [
                {**_asset(1, "DIFFERENT", "a" * 64, "1" * 16, "visual:a"), "item_number": 1},
                {**_asset(2, "UNVERIFIED", "b" * 64, "2" * 16, "visual:b"), "item_number": 2},
            ],
        },
        listicle_items=[1, 2, 3],
    )

    assert result.valid_distinct_inline_count == 1
    assert result.uncovered_listicle_items == ("2", "3")
    assert result.effective_missing == 2


def test_legacy_accepted_count_does_not_inflate_visual_coverage():
    media = MediaProgress(
        required=4,
        inline=(
            InlineMedia(
                1, "https://media.test/one.webp", 0,
                sha256="a" * 64, phash="1" * 16, visual_group_id="visual:a",
                visual_verification={"decision": "DIFFERENT"},
            ),
        ),
        accepted_count=4,
    )

    result = account_visual_media(media)

    assert media.accepted == 4
    assert result.accepted_working_count == 1
    assert result.valid_distinct_inline_count == 1
    assert result.effective_missing == 3


def test_working_reconciliation_is_read_only_and_idempotent():
    media = {
        "required": 2,
        "inline": [
            _asset(1, "DIFFERENT", "a" * 64, "1" * 16, "visual:a"),
            _asset(2, "SAME_IMAGE", "b" * 64, "2" * 16, "visual:b", duplicate_of="1"),
            _asset(3, "UNVERIFIED", "c" * 64, "3" * 16, "visual:c"),
        ],
    }
    before = repr(media)

    first = reconcile_working_visual_media(media)
    second = reconcile_working_visual_media(media)

    assert repr(media) == before
    assert [row["media_id"] for row in first.eligible_inline] == [1]
    assert [row["media_id"] for row in first.duplicate_inline] == [2]
    assert [row["media_id"] for row in first.unverified_inline] == [3]
    assert first == second
