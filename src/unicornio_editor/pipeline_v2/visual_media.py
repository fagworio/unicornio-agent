"""Read-only audit and guarded fingerprint reconciliation for V2 media."""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..media.downloader import download_image
from ..media.visual_identity import fingerprint_path
from .model import FeaturedProgress, InlineMedia, MediaProgress
from .operational import WordPressStateBackend
from .state_store import StateStore


def _asset_audit(asset: Any) -> dict[str, Any]:
    return {
        "media_id": asset.media_id,
        "media_url": asset.media_url,
        "sha256": asset.sha256,
        "phash": asset.phash,
        "visual_group_id": asset.visual_group_id,
        "verified": bool(asset.sha256 and asset.phash and asset.visual_group_id),
        "visual_verification": asset.visual_verification,
    }


def audit_visual_media(client: Any, root: Path, post_id: int) -> dict[str, Any]:
    """No-write projection suitable for production incident investigation."""
    store = StateStore(WordPressStateBackend(client))
    state = store.load(post_id)
    post = client.get_post(post_id)
    assets = [_asset_audit(item) for item in state.media.inline]
    if state.media.featured.media_id:
        assets.append({"role": "featured", **_asset_audit(state.media.featured)})
    return {
        "post_id": post_id,
        "wordpress_status": post.get("status"),
        "v2_state": state.state.value,
        "v2_phase": state.phase.value,
        "assets": assets,
        "identity_complete": bool(assets) and all(row["verified"] for row in assets),
        "writes": 0,
    }


def reconcile_visual_media(client: Any, root: Path, post_id: int, *, apply: bool = False) -> dict[str, Any]:
    """Materialise legacy attachment fingerprints, with no content/media writes.

    A failed materialisation never overwrites state.  ``--apply`` updates only
    ``_hermes_work_state`` after all accepted assets have been fingerprinted.
    """
    store = StateStore(WordPressStateBackend(client))
    previous = store.load(post_id)
    post = client.get_post(post_id)
    rows: list[tuple[str, Any]] = [("inline", item) for item in previous.media.inline]
    if previous.media.featured.media_id:
        rows.append(("featured", previous.media.featured))
    updates: dict[int, dict[str, str]] = {}
    errors: list[dict[str, str]] = []
    with tempfile.TemporaryDirectory(prefix="unicornio-visual-reconcile-") as directory:
        for role, asset in rows:
            if asset.sha256 and asset.phash and asset.visual_group_id:
                continue
            try:
                path = download_image(str(asset.media_url), Path(directory) / f"{role}-{asset.media_id}.img")
                updates[int(asset.media_id)] = fingerprint_path(path).to_dict()
            except Exception as exc:  # fail closed: retain state untouched
                errors.append({"media_id": str(asset.media_id), "role": role, "error": type(exc).__name__})
    eligible = bool(rows) and not errors
    result: dict[str, Any] = {"post_id": post_id, "wordpress_status": post.get("status"), "eligible": eligible, "apply": apply, "updates": updates, "errors": errors, "writes": 0}
    if not apply or not eligible:
        return result
    inline = tuple(
        replace(item, **updates[item.media_id]) if item.media_id in updates else item
        for item in previous.media.inline
    )
    featured = previous.media.featured
    if featured.media_id in updates:
        featured = replace(featured, **updates[featured.media_id])
    proposed = replace(previous, media=replace(previous.media, inline=inline, featured=featured))
    store.commit(post_id, proposed)
    readback = store.load(post_id)
    if readback.media.to_dict() != proposed.media.to_dict():
        raise RuntimeError("visual identity state readback mismatch")
    result.update({"writes": 1, "readback": True})
    return result
