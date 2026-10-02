from unicornio_editor.pipeline_v2.model import BlockerCode, InlineMedia, MediaProgress, Phase, WorkState


def mp(required, count):
    return MediaProgress(required, tuple(InlineMedia(i, f"u{i}", i) for i in range(1, count + 1)))
from unicornio_editor.pipeline_v2.scheduler import rank


def test_scheduler_prioritizes_featured_only_over_empty_media():
    near = WorkState(phase=Phase.MEDIA, blocker=BlockerCode.FEATURED_VISION, relevance_approved=True, media=mp(4, 4))
    far = WorkState(phase=Phase.MEDIA, blocker=BlockerCode.INLINE_MISSING, relevance_approved=True, media=mp(4, 0))
    assert rank(near) > rank(far)


def test_scheduler_prioritizes_one_missing_over_two_missing():
    one = WorkState(phase=Phase.MEDIA, blocker=BlockerCode.INLINE_MISSING, relevance_approved=True, media=mp(4, 3))
    two = WorkState(phase=Phase.MEDIA, blocker=BlockerCode.INLINE_MISSING, relevance_approved=True, media=mp(4, 2))
    assert rank(one) > rank(two)
