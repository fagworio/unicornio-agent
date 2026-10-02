"""Pure adapter from V1 state markers to the V2 domain model."""

from typing import Any

from .model import (
    BlockerCode,
    FeaturedProgress,
    InlineMedia,
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


def from_legacy_state(value: dict[str, Any] | None, *, inline_assets: list[dict[str, Any]] | None = None, featured: dict[str, Any] | None = None) -> WorkState:
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
        assets = tuple(InlineMedia.from_dict(item) for item in (inline_assets or []))
        featured_value = FeaturedProgress(
            str((featured or {}).get("status", featured_status)),
            (featured or {}).get("media_id"),
            (featured or {}).get("media_url"),
        )
        return WorkState(
            state=LifecycleState.PENDING,
            phase=Phase.MEDIA,
            blocker=blocker,
            retry=retry,
            relevance_approved=True,
            media=MediaProgress(required, assets, featured_value),
        )

    # V1 BLOCKED/NEW/PROCESSING and missing state all remain safely pending.
    phase = Phase.EDITORIAL if old in {"blocked", "processing"} else Phase.RELEVANCE
    blocker = BlockerCode.TEXT_QUALITY if old == "blocked" else None
    return WorkState(state=LifecycleState.PENDING, phase=phase, blocker=blocker, retry=retry)


class LegacyStateLoader:
    """Transition adapter combining V1 WordPress markers with its snapshot."""

    def __init__(self, manifest_loader):
        self._manifest_loader = manifest_loader

    def load(self, post_id: int, wp_meta: dict[str, Any]) -> WorkState:
        manifest = self._manifest_loader(post_id) or {}
        accepted = manifest.get("accepted_media", []) or []
        assets = []
        for index, item in enumerate(accepted, 1):
            assets.append({
                "media_id": int(item["media_id"]),
                "media_url": str(item.get("media_url", "")),
                "slot": int(item.get("slot", item.get("paragraph_index", index))),
                "alt_text": str(item.get("alt_text", "")),
                "credit_text": str(item.get("credit_text", "")),
            })
        featured = manifest.get("featured") if isinstance(manifest.get("featured"), dict) else None
        value = {
            "state": wp_meta.get("_hermes_state"),
            "attempts": wp_meta.get("_hermes_attempts"),
            "next_retry_at": wp_meta.get("_hermes_next_retry_at"),
            "last_error": wp_meta.get("_hermes_last_error"),
            "partial_kind": wp_meta.get("_hermes_partial_kind"),
            "partial_required": wp_meta.get("_hermes_media_required"),
            "partial_completed": wp_meta.get("_hermes_media_completed"),
            "partial_missing": wp_meta.get("_hermes_media_missing"),
            "no_progress_attempts": wp_meta.get("_hermes_no_progress_attempts"),
        }
        return from_legacy_state(value, inline_assets=assets, featured=featured)
