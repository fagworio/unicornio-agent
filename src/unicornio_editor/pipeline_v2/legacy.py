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


def _blocked_blocker(last_error: str) -> BlockerCode:
    markers = (
        ("qualidade_texto", BlockerCode.TEXT_QUALITY),
        ("seo", BlockerCode.SEO),
        ("estrutura", BlockerCode.STRUCTURE),
        ("schema", BlockerCode.SCHEMA),
        ("destaque", BlockerCode.FEATURED_INVALID),
        ("imagens_visao", BlockerCode.FEATURED_VISION),
        ("imagens_no_corpo", BlockerCode.INLINE_MISSING),
        ("imagens_webp", BlockerCode.MEDIA_INVALID),
        ("fonte", BlockerCode.SOURCE),
        ("trailer", BlockerCode.TRAILER),
    )
    for marker, blocker in markers:
        if marker in last_error:
            return blocker
    return BlockerCode.TEXT_QUALITY


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
        if (
            kind == "media"
            and _int(value.get("partial_missing")) == 0
            and _int(value.get("partial_completed")) == _int(value.get("partial_required"))
            and "imagens_visao" in str(value.get("last_error") or "")
        ):
            kind = "featured_vision"
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
            media=MediaProgress(required, assets, featured_value, accepted_count=accepted),
        )

    # Preserve V1 media progress even when apply recorded multiple blockers.
    kind = str(value.get("partial_kind") or "").casefold()
    media_kind_blockers = {
        "inline_missing": BlockerCode.INLINE_MISSING,
        "featured_missing": BlockerCode.FEATURED_MISSING,
        "featured_vision": BlockerCode.FEATURED_VISION,
    }
    if old == "blocked" and kind in media_kind_blockers:
        blocker = media_kind_blockers[kind]
    else:
        blocker = _blocked_blocker(str(value.get("last_error") or "")) if old == "blocked" else None
    media_blockers = {BlockerCode.FEATURED_INVALID, BlockerCode.FEATURED_VISION, BlockerCode.FEATURED_MISSING, BlockerCode.INLINE_MISSING, BlockerCode.MEDIA_INVALID}
    phase = Phase.MEDIA if blocker in media_blockers else (Phase.EDITORIAL if old in {"blocked", "processing"} else Phase.RELEVANCE)
    required = _int(value.get("partial_required"))
    assets = tuple(InlineMedia.from_dict(item) for item in (inline_assets or []))
    featured_value = FeaturedProgress(str((featured or {}).get("status", "missing")), (featured or {}).get("media_id"), (featured or {}).get("media_url"))
    media = MediaProgress(required, assets, featured_value, accepted_count=_int(value.get("partial_completed")) if required else None) if required or assets else MediaProgress()
    return WorkState(state=LifecycleState.PENDING, phase=phase, blocker=blocker, retry=retry, relevance_approved=bool(kind in media_kind_blockers), media=media)


class LegacyStateLoader:
    """Transition adapter combining V1 WordPress markers with its snapshot."""

    def __init__(self, manifest_loader):
        self._manifest_loader = manifest_loader

    @staticmethod
    def _normalize_featured(value: dict[str, Any] | None) -> dict[str, Any] | None:
        if not value:
            return value
        normalized = {**value}
        status = str(normalized.get("status", "missing")).lower()
        normalized["status"] = {
            "rejected": "vision_rejected",
            "vision": "vision_rejected",
            "vision_rejected": "vision_rejected",
            "failed": "invalid",
        }.get(status, status)
        return normalized

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
        featured = self._normalize_featured(manifest.get("featured") if isinstance(manifest.get("featured"), dict) else None)
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
