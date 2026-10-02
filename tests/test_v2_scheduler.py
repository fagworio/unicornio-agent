from unicornio_editor.pipeline_v2.model import BlockerCode, MediaProgress, Phase, WorkState
from unicornio_editor.pipeline_v2.scheduler import rank


def test_scheduler_prioritizes_featured_only_over_empty_media():
    near = WorkState(phase=Phase.MEDIA, blocker=BlockerCode.FEATURED_VISION, relevance_approved=True, media=MediaProgress(4, 4))
    far = WorkState(phase=Phase.MEDIA, blocker=BlockerCode.INLINE_MISSING, relevance_approved=True, media=MediaProgress(4, 0))
    assert rank(near) > rank(far)


def test_scheduler_prioritizes_one_missing_over_two_missing():
    one = WorkState(phase=Phase.MEDIA, blocker=BlockerCode.INLINE_MISSING, relevance_approved=True, media=MediaProgress(4, 3))
    two = WorkState(phase=Phase.MEDIA, blocker=BlockerCode.INLINE_MISSING, relevance_approved=True, media=MediaProgress(4, 2))
    assert rank(one) > rank(two)
