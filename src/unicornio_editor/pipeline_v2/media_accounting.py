"""Pure visual-media accounting shared by planning and validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


_POSITIVE_DECISIONS = frozenset({"INITIAL", "DIFFERENT"})
_DUPLICATE_DECISIONS = frozenset({"SAME_IMAGE", "SAME_ART_CROP"})


@dataclass(frozen=True)
class MediaAccounting:
    required_inline: int
    accepted_working_count: int
    valid_distinct_inline_count: int
    confirmed_duplicate_count: int
    unverified_count: int
    effective_missing: int
    valid_inline_ids: tuple[int, ...]
    duplicate_inline_ids: tuple[int, ...]
    unverified_inline_ids: tuple[int, ...]
    featured_identity_status: str
    baseline_complete: bool
    uncovered_listicle_items: tuple[str, ...] = ()


@dataclass(frozen=True)
class MediaReconciliation:
    """Read-only eligible, duplicate, and unresolved working-media sets."""

    accounting: MediaAccounting
    eligible_inline: tuple[Any, ...]
    duplicate_inline: tuple[Any, ...]
    unverified_inline: tuple[Any, ...]


def _row(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        return dict(result) if isinstance(result, Mapping) else {}
    return {}


def _identity_status(asset: Mapping[str, Any]) -> str:
    verification = asset.get("visual_verification")
    verification = verification if isinstance(verification, Mapping) else {}
    decision = str(verification.get("decision") or "").upper()
    if decision in _DUPLICATE_DECISIONS:
        return "duplicate"
    if (
        decision in _POSITIVE_DECISIONS
        and str(asset.get("sha256") or "").strip()
        and str(asset.get("phash") or "").strip()
        and str(asset.get("visual_group_id") or "").strip()
    ):
        return "valid"
    return "unverified"


def account_visual_media(
    media_or_assets: Any,
    *,
    required_inline: int | None = None,
    featured: Any = None,
    listicle_items: Iterable[Any] = (),
) -> MediaAccounting:
    """Project quota coverage from complete positive visual evidence.

    The function is deliberately read-only and accepts either a V2
    ``MediaProgress`` or its serialized media mapping. Editorial relevance
    remains a separate gate; this projection answers identity and coverage.
    """
    media_row = _row(media_or_assets)
    if required_inline is None:
        nested_inline = media_row.get("inline")
        required_inline = media_row.get("required")
        if required_inline is None and isinstance(nested_inline, Mapping):
            required_inline = nested_inline.get("required", 0)
        if required_inline is None:
            required_inline = 0
    required = max(0, int(required_inline or 0))
    if featured is None:
        featured = media_row.get("featured")
    featured_row = _row(featured)
    if str(featured_row.get("status") or "").casefold() not in {"valid", "featuredstatus.valid"}:
        featured_row = {}
    featured_status = "absent" if not featured_row else _identity_status(featured_row)

    inline_value: Any = media_or_assets if isinstance(media_or_assets, (list, tuple)) else getattr(media_or_assets, "inline", None)
    if inline_value is None:
        inline_value = media_row.get("inline")
    if inline_value is None and isinstance(media_row, Mapping):
        inline_value = media_row.get("accepted", ())
    if isinstance(inline_value, Mapping):
        inline_value = inline_value.get("accepted", ())
    if not isinstance(inline_value, (list, tuple)):
        inline_value = getattr(media_or_assets, "inline", ())
    rows = [_row(item) for item in (inline_value or ())]
    rows = [row for row in rows if row]

    featured_identity = _identity_status(featured_row) if featured_row else "absent"
    baseline_keys: set[str] = set()
    if featured_row and featured_identity == "valid":
        baseline_keys.update(
            str(featured_row.get(key) or "")
            for key in ("sha256", "visual_group_id")
            if str(featured_row.get(key) or "")
        )

    valid_ids: list[int] = []
    duplicate_ids: list[int] = []
    unverified_ids: list[int] = []
    duplicate_count = 0
    unverified_count = 0
    valid_keys = set(baseline_keys)
    valid_records: list[dict[str, Any]] = []
    for asset in rows:
        try:
            media_id = int(asset.get("media_id") or 0)
        except (TypeError, ValueError):
            media_id = 0
        status = _identity_status(asset)
        if status == "duplicate":
            duplicate_count += 1
            if media_id:
                duplicate_ids.append(media_id)
            continue
        if status != "valid":
            unverified_count += 1
            if media_id:
                unverified_ids.append(media_id)
            continue
        identity_keys = {
            str(asset.get(key) or "")
            for key in ("sha256", "visual_group_id")
            if str(asset.get(key) or "")
        }
        if identity_keys.intersection(valid_keys):
            duplicate_count += 1
            if media_id:
                duplicate_ids.append(media_id)
            continue
        valid_keys.update(identity_keys)
        valid_records.append(asset)
        if media_id:
            valid_ids.append(media_id)

    covered_items: set[str] = set()
    for asset in valid_records:
        for key in ("item_number", "subject"):
            value = str(asset.get(key) or "").strip().casefold()
            if value:
                covered_items.add(value)
    requested_items: list[tuple[str, tuple[str, ...]]] = []
    for item in listicle_items:
        if isinstance(item, Mapping):
            primary = str(item.get("item") or item.get("item_number") or item.get("subject") or "")
            aliases = (primary, str(item.get("subject") or ""))
        else:
            primary = str(item or "")
            aliases = (primary,)
        key = primary.strip().casefold()
        normalized_aliases = tuple(dict.fromkeys(
            value.strip().casefold() for value in aliases if value.strip()
        ))
        if key and all(existing_key != key for existing_key, _aliases in requested_items):
            requested_items.append((key, normalized_aliases))
    uncovered = tuple(
        key for key, aliases in requested_items
        if not set(aliases).intersection(covered_items)
    )
    missing = max(0, required - len(valid_ids))
    if requested_items:
        missing = max(missing, len(uncovered))
    return MediaAccounting(
        required_inline=required,
        accepted_working_count=len(rows),
        valid_distinct_inline_count=len(valid_ids),
        confirmed_duplicate_count=duplicate_count,
        unverified_count=unverified_count,
        effective_missing=missing,
        valid_inline_ids=tuple(valid_ids),
        duplicate_inline_ids=tuple(duplicate_ids),
        unverified_inline_ids=tuple(unverified_ids),
        featured_identity_status=featured_status,
        baseline_complete=unverified_count == 0 and featured_status != "unverified",
        uncovered_listicle_items=uncovered,
    )


def reconcile_working_visual_media(
    media: Any,
    *,
    required_inline: int | None = None,
    featured: Any = None,
    listicle_items: Iterable[Any] = (),
) -> MediaReconciliation:
    """Project the eligible working set without mutating checkpoints or WP."""
    accounting = account_visual_media(
        media,
        required_inline=required_inline,
        featured=featured,
        listicle_items=listicle_items,
    )
    media_row = _row(media)
    inline_value: Any = media if isinstance(media, (list, tuple)) else getattr(media, "inline", None)
    if inline_value is None:
        inline_value = media_row.get("inline")
    if isinstance(inline_value, Mapping):
        inline_value = inline_value.get("accepted", ())
    if not isinstance(inline_value, (list, tuple)):
        inline_value = getattr(media, "inline", ())
    assets = tuple(inline_value or ())
    valid_ids = set(accounting.valid_inline_ids)
    duplicate_ids = set(accounting.duplicate_inline_ids)

    def media_id(asset: Any) -> int:
        try:
            return int(_row(asset).get("media_id") or 0)
        except (TypeError, ValueError):
            return 0

    eligible = tuple(
        asset for asset in assets
        if media_id(asset) in valid_ids
    )
    duplicates = tuple(
        asset for asset in assets
        if media_id(asset) in duplicate_ids
    )
    unverified = tuple(
        asset for asset in assets
        if media_id(asset) in set(accounting.unverified_inline_ids)
    )
    return MediaReconciliation(accounting, eligible, duplicates, unverified)
