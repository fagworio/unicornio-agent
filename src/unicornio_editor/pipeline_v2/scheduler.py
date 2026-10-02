"""Pure distance-to-ready ranking for V2 work states."""

from .model import BlockerCode, Phase, WorkState


def rank(state: WorkState) -> int:
    """Return a higher score for work closer to READY."""
    if state.state.value in {"ready", "published", "skipped", "human_required"}:
        return -1000
    if state.blocker in {BlockerCode.FEATURED_MISSING, BlockerCode.FEATURED_INVALID, BlockerCode.FEATURED_VISION}:
        return 1000 if state.media.missing == 0 else 500 - state.media.missing
    if state.blocker in {BlockerCode.INLINE_MISSING, BlockerCode.MEDIA_INVALID, BlockerCode.MEDIA_DUPLICATE, BlockerCode.MEDIA_ORIGIN}:
        return 400 - state.media.missing
    if state.phase is Phase.EDITORIAL:
        return 250
    if state.phase is Phase.RELEVANCE:
        return 100
    return 50
