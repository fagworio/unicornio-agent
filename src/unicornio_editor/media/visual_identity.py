"""Persistent, fail-closed visual identity for V2 media assets.

The subject/relevance vision gate answers *what* an image depicts.  This
module answers whether two final image files are the same artwork.  It is kept
separate so a relevance cache can never accidentally authorise a duplicate.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .library_index import hamming
from .vision_cache import get_cached_visual_comparison, set_cached_visual_comparison
from .vision_gate import compare_visual_assets, prepare_vision_image_input_from_path
from .visual_hash import phash_from_path


@dataclass(frozen=True)
class VisualIdentity:
    sha256: str
    phash: str
    visual_group_id: str

    def to_dict(self) -> dict[str, str]:
        return {"sha256": self.sha256, "phash": self.phash, "visual_group_id": self.visual_group_id}


@dataclass(frozen=True)
class VisualIdentityDecision:
    decision: str
    verified: bool
    duplicate_of: str = ""
    reason: str = ""
    comparisons: tuple[dict[str, Any], ...] = ()


def fingerprint_path(path: str | Path) -> VisualIdentity:
    data = Path(path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    return VisualIdentity(digest, phash_from_path(str(path)), f"visual:{digest[:24]}")


def verify_candidate_identity(
    candidate_path: str | Path,
    *, candidate_id: str, baseline: Iterable[dict[str, Any]], config: Any, root: str | Path,
    comparison_budget: list[int] | None = None,
) -> tuple[VisualIdentity, VisualIdentityDecision]:
    """Compare final candidate bytes with every materialised baseline asset.

    Missing baseline bytes or disabled/unavailable vision are deliberately not
    interpreted as distinct.  The caller must defer the candidate instead of
    silently growing a duplicate library.
    """
    identity = fingerprint_path(candidate_path)
    comparisons: list[dict[str, Any]] = []
    rows = list(baseline)
    if not rows:
        return identity, VisualIdentityDecision("INITIAL", True, reason="no_visual_baseline")
    for asset in rows:
        ref_id = str(asset.get("media_id") or asset.get("media_url") or "baseline")
        ref_sha = str(asset.get("sha256") or "")
        ref_phash = str(asset.get("phash") or "")
        if ref_sha and ref_sha == identity.sha256:
            return identity, VisualIdentityDecision("SAME_IMAGE", True, ref_id, "sha256_exact")
        # pHash is a cheap definitive catch for an exact/recompressed frame.
        if ref_phash and identity.phash and hamming(ref_phash, identity.phash) <= 2:
            return identity, VisualIdentityDecision("SAME_IMAGE", True, ref_id, "phash_near_exact")
        if not bool(getattr(config, "vision_enabled", False)) or not str(getattr(config, "vision_api_key", "")):
            return identity, VisualIdentityDecision("UNVERIFIED", False, ref_id, "vision_identity_unavailable")
        reference_path = asset.get("local_path")
        if not reference_path or not Path(str(reference_path)).is_file() or not ref_sha:
            return identity, VisualIdentityDecision("UNVERIFIED", False, ref_id, "baseline_identity_unavailable")
        cached = get_cached_visual_comparison(root, ref_sha, identity.sha256)
        if cached is None:
            if comparison_budget is not None and comparison_budget[0] >= max(0, int(getattr(config, "visual_comparison_max_calls", 0))):
                return identity, VisualIdentityDecision("UNVERIFIED", False, ref_id, "visual_comparison_budget_exhausted", tuple(comparisons))
            candidate_input = prepare_vision_image_input_from_path(candidate_path)
            comparison = compare_visual_assets(
                prepare_vision_image_input_from_path(reference_path), candidate_input,
                reference_id=ref_id, candidate_id=candidate_id,
                api_key=str(config.vision_api_key), base_url=str(config.vision_base_url),
                model=str(config.vision_model), root=root,
                call_budget=comparison_budget,
                max_calls=max(0, int(getattr(config, "visual_comparison_max_calls", 0))),
            )
            cached = comparison.to_dict()
            # Transient provider/budget states are deliberately not durable:
            # a later retry may have a healthy provider or fresh budget.
            if (
                comparison.decision in {"SAME_IMAGE", "SAME_ART_CROP", "DIFFERENT"}
                and comparison.confidence >= float(getattr(config, "visual_comparison_min_confidence", 0.85))
            ):
                set_cached_visual_comparison(root, ref_sha, identity.sha256, cached)
        comparisons.append(dict(cached))
        decision = str(cached.get("decision") or "ERROR")
        try:
            confidence = float(cached.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.0
        minimum = float(getattr(config, "visual_comparison_min_confidence", 0.85))
        if decision in {"SAME_IMAGE", "SAME_ART_CROP", "DIFFERENT"} and confidence < minimum:
            return identity, VisualIdentityDecision("UNVERIFIED", False, ref_id, "visual_comparison_low_confidence", tuple(comparisons))
        if decision in {"SAME_IMAGE", "SAME_ART_CROP"}:
            return identity, VisualIdentityDecision(decision, True, ref_id, "gpt_visual_comparison", tuple(comparisons))
        if decision != "DIFFERENT":
            return identity, VisualIdentityDecision("UNVERIFIED", False, ref_id, "visual_comparison_inconclusive", tuple(comparisons))
    return identity, VisualIdentityDecision("DIFFERENT", True, reason="all_baselines_different", comparisons=tuple(comparisons))
