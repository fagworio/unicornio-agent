"""Evidence-bearing layout policy for normal-article inline media."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from ..media.inserter import normal_media_slot_capacity, plan_normal_media_insertions
from ..media.relevance import iter_content_images


LAYOUT_POLICY_VERSION = 2
LAYOUT_POLICY_NAME = "normal-article-layout-v2"


def _canonical_url(value: str) -> str:
    from urllib.parse import urlsplit
    import re

    parsed = urlsplit(str(value or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return str(value or "").strip().split("?", 1)[0]
    path = re.sub(r"-\d+x\d+(?=\.[^./]+$)", "", parsed.path)
    return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}{path.rstrip('/') or '/'}"


def editorial_body_sha256(body: str) -> str:
    return hashlib.sha256(str(body or "").encode("utf-8")).hexdigest()


def _asset_value(asset: Any, name: str) -> Any:
    if isinstance(asset, Mapping):
        return asset.get(name)
    return getattr(asset, name, None)


def _closing_featured_repeat_record(
    featured: Any,
    *,
    body_hash: str,
    placement_plan: list[dict[str, Any]],
    placement_plan_hash: str,
) -> dict[str, Any] | None:
    if not featured or not placement_plan or placement_plan[-1].get("anchor") != "after_editorial_body":
        return None
    try:
        media_id = int(_asset_value(featured, "media_id") or 0)
        width = int(_asset_value(featured, "width") or 0)
        height = int(_asset_value(featured, "height") or 0)
    except (TypeError, ValueError):
        return None
    verification = _asset_value(featured, "visual_verification")
    verification = verification if isinstance(verification, Mapping) else {}
    decision = str(verification.get("decision") or "").upper()
    url = str(_asset_value(featured, "media_url") or "").strip()
    alt = str(_asset_value(featured, "alt_text") or "").strip()
    sha256 = str(_asset_value(featured, "sha256") or "").strip()
    phash = str(_asset_value(featured, "phash") or "").strip()
    visual_group_id = str(_asset_value(featured, "visual_group_id") or "").strip()
    status = str(_asset_value(featured, "status") or "").casefold()
    from urllib.parse import urlsplit

    parsed_url = urlsplit(url)
    if (
        status not in {"valid", "featuredstatus.valid"}
        or media_id < 1 or parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc or not alt
        or width != 1280 or height != 720
        or not sha256 or not phash or not visual_group_id
        or decision not in {"INITIAL", "DIFFERENT"}
    ):
        return None
    return {
        "role": "closing_featured_repeat",
        "placement": "after_editorial_body",
        "media_id": media_id,
        "media_url": _canonical_url(url),
        "asset_sha256": sha256,
        "asset_phash": phash,
        "visual_group_id": visual_group_id,
        "body_sha256": body_hash,
        "placement_plan_sha256": placement_plan_hash,
    }


def _verified_present_count(
    body: str,
    assets: Iterable[Any],
    *,
    featured_media_id: int | None = None,
) -> int:
    embedded = {
        _canonical_url(str(item.get("src") or ""))
        for item in iter_content_images(body)
        if str(item.get("src") or "").strip()
    }
    urls: set[str] = set()
    for asset in assets:
        url = str(_asset_value(asset, "media_url") or "").strip()
        media_id = _asset_value(asset, "media_id")
        if (
            url
            and isinstance(media_id, int)
            and not isinstance(media_id, bool)
            and media_id > 0
            and media_id != featured_media_id
            and _canonical_url(url) in embedded
        ):
            urls.add(_canonical_url(url))
    return len(urls)


def build_normal_article_layout_policy(
    body: str,
    *,
    target_ideal: int,
    verified_inline: Iterable[Any],
    source_body_sha256: str | None = None,
    featured_media_id: int | None = None,
    featured_repeat_evidence: Any = None,
) -> dict[str, Any] | None:
    """Build a reduction only from a parseable, positive structural capacity."""
    if isinstance(target_ideal, bool) or target_ideal < 1:
        return None
    layout = normal_media_slot_capacity(body)
    if not layout.get("verified") or layout.get("first_slot") is None:
        return None
    present = _verified_present_count(
        body, verified_inline, featured_media_id=featured_media_id,
    )
    capacity = present + int(layout.get("capacity") or 0)
    if capacity < 1:
        return None
    effective = min(target_ideal, capacity)
    # Persist the full set of structurally safe positions, independently of
    # the minimum quota. Composition can then preserve already-accepted
    # verified media without inventing new slots or treating them as quota.
    insertion_count = int(layout.get("capacity") or 0)
    planned = plan_normal_media_insertions(
        body,
        [
            {"paragraph_index": 0, "media_url": f"__layout_slot_{index}__"}
            for index in range(insertion_count)
        ],
    )
    body_hash = editorial_body_sha256(body)
    placement_plan = [
        {
            "anchor": str(item.get("placement") or "after_paragraph"),
            "paragraph_index": int(item["paragraph_index"]),
            "body_sha256": body_hash,
        }
        for item in planned
    ]
    placement_plan_hash = _placement_plan_sha256(body_hash, placement_plan)
    policy: dict[str, Any] = {
        "schema_version": LAYOUT_POLICY_VERSION,
        "policy": LAYOUT_POLICY_NAME,
        "layout": "normal_article",
        "target_ideal": int(target_ideal),
        "capacity_verified": int(capacity),
        "target_effective": int(effective),
        "reduction_reason": "layout_limited" if effective < target_ideal else "",
        "body_sha256": body_hash,
        "source_body_sha256": source_body_sha256 or body_hash,
        "verified_inline_in_body": present,
        "additional_slots": int(layout.get("capacity") or 0),
        "occupied_slots": list(layout.get("existing_media_slots") or []),
        "minimum_spacing": int(layout.get("minimum_spacing") or 3),
        "placement_plan": placement_plan,
        "placement_plan_sha256": placement_plan_hash,
    }
    closing_repeat = _closing_featured_repeat_record(
        featured_repeat_evidence,
        body_hash=body_hash,
        placement_plan=placement_plan,
        placement_plan_hash=placement_plan_hash,
    )
    if closing_repeat:
        policy["closing_featured_repeat"] = closing_repeat
    policy["structural_sha256"] = _structural_sha256(policy)
    policy["layout_sha256"] = _layout_sha256(policy)
    return policy


def _structural_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    """Return the immutable body/layout contract, excluding dynamic evidence."""
    return {
        key: value
        for key, value in policy.items()
        if key not in {
            "closing_featured_repeat",
            "structural_sha256",
            "layout_sha256",
        }
    }


def _structural_sha256(policy: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _structural_policy(policy),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _layout_sha256(policy: Mapping[str, Any]) -> str:
    stable = {
        key: policy.get(key)
        for key in (
            "schema_version", "policy", "layout", "target_ideal",
            "capacity_verified", "target_effective", "reduction_reason",
            "body_sha256", "source_body_sha256", "verified_inline_in_body", "additional_slots",
            "occupied_slots", "minimum_spacing", "placement_plan", "placement_plan_sha256",
            "closing_featured_repeat",
        )
    }
    # The optional field is absent in historical policies. Adding it only for
    # new records preserves their legacy hash while protecting the new
    # structural signature from tampering.
    if "structural_sha256" in policy:
        stable["structural_sha256"] = policy.get("structural_sha256")
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _placement_plan_sha256(body_sha256: str, plan: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        {"body_sha256": body_sha256, "placement_plan": plan},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_normal_article_layout_policy(
    body: str,
    policy: Mapping[str, Any] | None,
    *,
    target_ideal: int,
    media: Any,
    listicle: bool = False,
) -> dict[str, Any]:
    """Recompute persisted layout evidence; never trust a success flag alone."""
    if policy is None:
        return {"valid": True, "present": False, "reason": "not_provided"}
    if not isinstance(policy, Mapping):
        return {"valid": False, "present": True, "reason": "invalid_policy_type"}
    if listicle or policy.get("layout") != "normal_article":
        return {"valid": False, "present": True, "reason": "layout_scope_mismatch"}
    if policy.get("schema_version") != LAYOUT_POLICY_VERSION or policy.get("policy") != LAYOUT_POLICY_NAME:
        return {"valid": False, "present": True, "reason": "unsupported_layout_policy"}
    if policy.get("body_sha256") != editorial_body_sha256(body):
        return {"valid": False, "present": True, "reason": "prepared_body_hash_mismatch"}
    if policy.get("target_ideal") != target_ideal:
        return {"valid": False, "present": True, "reason": "target_ideal_mismatch"}
    try:
        from .media_accounting import account_visual_media

        accounting = account_visual_media(media, required_inline=target_ideal)
        accepted = getattr(media, "inline", None)
        if accepted is None and isinstance(media, Mapping):
            inline = media.get("inline")
            accepted = inline.get("accepted", []) if isinstance(inline, Mapping) else []
        valid_ids = set(accounting.valid_inline_ids)
        valid_assets = [
            item for item in (accepted or [])
            if _asset_value(item, "media_id") in valid_ids
        ]
        expected_policy = build_normal_article_layout_policy(
            body,
            target_ideal=target_ideal,
            verified_inline=valid_assets,
            source_body_sha256=str(policy.get("source_body_sha256") or "") or None,
            featured_media_id=(
                int(_asset_value(featured_row, "media_id") or 0)
                if (featured_row := _asset_value(media, "featured")) is not None
                else None
            ),
            featured_repeat_evidence=featured_row,
        )
    except (TypeError, ValueError, KeyError):
        return {"valid": False, "present": True, "reason": "layout_capacity_unverifiable"}
    if expected_policy is None:
        return {"valid": False, "present": True, "reason": "layout_capacity_unverifiable"}
    if policy.get("placement_plan_sha256") != _placement_plan_sha256(
        str(policy.get("body_sha256") or ""),
        list(policy.get("placement_plan") or []),
    ):
        return {"valid": False, "present": True, "reason": "placement_plan_hash_mismatch"}
    if policy.get("layout_sha256") != _layout_sha256(policy):
        return {"valid": False, "present": True, "reason": "layout_hash_mismatch"}
    persisted_structural_hash = policy.get("structural_sha256")
    if (
        persisted_structural_hash is not None
        and persisted_structural_hash != _structural_sha256(policy)
    ):
        return {"valid": False, "present": True, "reason": "structural_hash_mismatch"}
    if _structural_policy(policy) != _structural_policy(expected_policy):
        return {"valid": False, "present": True, "reason": "layout_capacity_or_plan_mismatch"}
    if "closing_featured_repeat" in policy:
        if policy.get("closing_featured_repeat") != expected_policy.get(
            "closing_featured_repeat"
        ):
            return {
                "valid": False,
                "present": True,
                "reason": "closing_featured_repeat_mismatch",
            }
    capacity = int(expected_policy["capacity_verified"])
    effective = int(expected_policy["target_effective"])
    reduction_reason = str(expected_policy["reduction_reason"])
    return {
        "valid": effective >= 1,
        "present": True,
        "reason": "verified" if effective >= 1 else "zero_layout_capacity",
        "target_ideal": target_ideal,
        "capacity_verified": capacity,
        "target_effective": effective,
        "allowed_surplus": max(0, target_ideal - effective),
        "reduction_reason": reduction_reason,
    }
