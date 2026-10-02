"""Pure adapter from V1 state markers to the V2 domain model."""

from typing import Any

from .model import (
    BlockerCode,
    FeaturedProgress,
    LifecycleState,
    MediaProgress,
    Phase,
    RetryInfo,
    WorkState,
)


def _int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def from_legacy_state(value: dict[str, Any] | None) -> WorkState:
    """Translate V1 markers without exposing V1 concepts to V2 callers."""
    value = value or {}
    old = value.get("state")
    attempts = _int(value.get("attempts"))
    retry = RetryInfo(attempts=attempts, no_progress=_int(value.get("no_progress_attempts")), next_at=value.get("next_retry_at") or None)

    if old == "ready":
        return WorkState(state=LifecycleState.READY, phase=Phase.VALIDATE, retry=retry, relevance_approved=True)
    if old == "published":
        return WorkState(state=LifecycleState.PUBLISHED, phase=Phase.PUBLISH, retry=retry, relevance_approved=True)
    if old == "skipped":
        return WorkState(state=LifecycleState.SKIPPED, phase=Phase.RELEVANCE, retry=retry)
    if old == "awaiting_human":
        return WorkState(state=LifecycleState.HUMAN_REQUIRED, phase=Phase.EDITORIAL, retry=retry)
    if old == "uncertain":
        return WorkState(state=LifecycleState.PENDING, phase=Phase.RELEVANCE, blocker=BlockerCode.RELEVANCE_UNCERTAIN, retry=retry)

    if old == "partial":
        required = _int(value.get("partial_required"))
        accepted = min(required, _int(value.get("partial_completed")))
        kind = value.get("partial_kind") or ""
        blocker = {
            "featured_vision": BlockerCode.FEATURED_VISION,
            "featured_missing": BlockerCode.FEATURED_MISSING,
            "inline_missing": BlockerCode.INLINE_MISSING,
        }.get(kind, BlockerCode.INLINE_MISSING)
        featured_status = "vision_rejected" if blocker is BlockerCode.FEATURED_VISION else "missing"
        return WorkState(
            state=LifecycleState.PENDING,
            phase=Phase.MEDIA,
            blocker=blocker,
            retry=retry,
            relevance_approved=True,
            media=MediaProgress(required, accepted, FeaturedProgress(featured_status)),
        )

    # V1 BLOCKED/NEW/PROCESSING and missing state all remain safely pending.
    phase = Phase.EDITORIAL if old in {"blocked", "processing"} else Phase.RELEVANCE
    blocker = BlockerCode.TEXT_QUALITY if old == "blocked" else None
    return WorkState(state=LifecycleState.PENDING, phase=phase, blocker=blocker, retry=retry)
