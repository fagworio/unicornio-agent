"""Auditable, guarded visual-media reconciliation for one V2 post."""

from __future__ import annotations

import json
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..backup import atomic_write_text
from ..locking import LockManager
from ..media.downloader import download_image
from ..media.visual_identity import fingerprint_path, verify_candidate_identity
from ..observability import append_telemetry
from .model import LifecycleState
from .operational import WordPressStateBackend
from .state_store import StateStore


def _asset_row(role: str, asset: Any) -> dict[str, Any]:
    return {
        "role": role, "media_id": asset.media_id, "media_url": asset.media_url,
        "sha256": asset.sha256, "phash": asset.phash,
        "visual_group_id": asset.visual_group_id,
        "visual_verification": asset.visual_verification,
    }


def _decision_confidence(decision: Any) -> float | None:
    for comparison in reversed(getattr(decision, "comparisons", ())):
        try:
            return float(comparison.get("confidence"))
        except (TypeError, ValueError, AttributeError):
            pass
    return None


def _inspect(client: Any, config: Any, root: Path, post_id: int) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    store = StateStore(WordPressStateBackend(client))
    state = store.load(post_id)
    post = client.get_post(post_id)
    rows: list[tuple[str, Any]] = [("inline", item) for item in state.media.inline]
    if state.media.featured.media_id and state.media.featured.media_url:
        rows.insert(0, ("featured", state.media.featured))
    errors: list[dict[str, str]] = []
    materialized: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    retained: list[dict[str, Any]] = []
    comparison_budget = [0]
    with tempfile.TemporaryDirectory(prefix="unicornio-visual-reconcile-") as directory:
        for role, asset in rows:
            row = _asset_row(role, asset)
            try:
                local = download_image(str(asset.media_url), Path(directory) / f"{role}-{asset.media_id}.img")
                identity = fingerprint_path(local)
                row.update(identity.to_dict())
                row["local_path"] = str(local)
                materialized.append(row)
            except Exception as exc:  # no destructive action without pixels
                errors.append({"media_id": str(asset.media_id), "role": role, "error": type(exc).__name__})
                continue
            if role == "featured":
                row["visual_verification"] = {
                    "method": "fingerprint", "decision": "INITIAL",
                    "duplicate_of": "", "confidence": None, "comparator_version": 1,
                }
                retained.append(row)
                continue
            identity, decision = verify_candidate_identity(
                local, candidate_id=str(asset.media_id), baseline=retained,
                config=config, root=root, comparison_budget=comparison_budget,
            )
            row.update(identity.to_dict())
            row["visual_verification"] = {
                "method": decision.reason, "decision": decision.decision,
                "duplicate_of": decision.duplicate_of, "confidence": _decision_confidence(decision),
                "comparator_version": 1,
            }
            if decision.decision in {"SAME_IMAGE", "SAME_ART_CROP"}:
                duplicates.append({
                    "media_id": int(asset.media_id), "media_url": str(asset.media_url),
                    "duplicate_of": decision.duplicate_of, "method": decision.reason,
                    "confidence": _decision_confidence(decision), "reason": decision.decision,
                })
            elif decision.verified:
                retained.append(row)
            else:
                errors.append({"media_id": str(asset.media_id), "role": role, "error": decision.reason})
    inline_unique = sum(row["role"] == "inline" for row in retained)
    report = {
        "post_id": post_id, "wordpress_status": post.get("status"),
        "v2_state": state.state.value, "v2_phase": state.phase.value,
        "required_inline": state.media.required,
        "attachments_accepted": state.media.accepted,
        "inline_visual_unique": inline_unique,
        "effective_missing": max(0, state.media.required - inline_unique),
        "assets": [{key: value for key, value in row.items() if key != "local_path"} for row in materialized],
        "duplicates": duplicates, "errors": errors,
        "comparison_calls": comparison_budget[0],
        "identity_complete": bool(rows) and not errors,
        "writes": 0,
    }
    return state, post, report


def audit_visual_media(client: Any, config: Any, root: Path, post_id: int) -> dict[str, Any]:
    """Read-only visual accounting. It never updates V2 or WordPress."""
    _state, _post, report = _inspect(client, config, Path(root), post_id)
    report["command"] = "v2-audit-visual-media"
    return report


def reconcile_visual_media(client: Any, config: Any, root: Path, post_id: int, *, apply: bool = False) -> dict[str, Any]:
    """Remove only proven duplicate inline assets from V2 progress.

    The command never deletes global attachments or resets retries.  The next
    normal COMPOSE rebuilds pending HTML from the retained V2 media state.
    """
    root = Path(root)
    previous, post, report = _inspect(client, config, root, post_id)
    report.update({"command": "v2-reconcile-visual-media", "apply": apply})
    safe = (
        str(post.get("status") or "") == "pending"
        and previous.state not in {LifecycleState.READY, LifecycleState.PUBLISHED}
        and not report["errors"]
    )
    report["eligible"] = safe
    if not apply or not safe:
        return report
    duplicate_ids = {int(item["media_id"]) for item in report["duplicates"]}
    identity_by_id = {int(row["media_id"]): row for row in report["assets"] if row.get("media_id")}
    with LockManager(root / "work" / "locks", ttl=int(getattr(config, "lock_ttl", 900))).acquire(post_id):
        store = StateStore(WordPressStateBackend(client))
        current = store.load(post_id)
        current_post = client.get_post(post_id)
        if current.to_dict() != previous.to_dict() or str(current_post.get("status") or "") != "pending":
            raise RuntimeError("visual reconciliation state changed concurrently")
        remaining = tuple(
            replace(
                item,
                sha256=str(identity_by_id.get(item.media_id, {}).get("sha256") or item.sha256),
                phash=str(identity_by_id.get(item.media_id, {}).get("phash") or item.phash),
                visual_group_id=str(identity_by_id.get(item.media_id, {}).get("visual_group_id") or item.visual_group_id),
                visual_verification=dict(identity_by_id.get(item.media_id, {}).get("visual_verification") or item.visual_verification),
            )
            for item in current.media.inline if item.media_id not in duplicate_ids
        )
        featured = current.media.featured
        if featured.media_id in identity_by_id:
            row = identity_by_id[featured.media_id]
            featured = replace(
                featured, sha256=str(row.get("sha256") or featured.sha256),
                phash=str(row.get("phash") or featured.phash),
                visual_group_id=str(row.get("visual_group_id") or featured.visual_group_id),
                visual_verification=dict(row.get("visual_verification") or featured.visual_verification),
            )
        proposed = replace(current, media=replace(current.media, inline=remaining, featured=featured, accepted_count=None))
        journal_dir = root / "work" / "v2-visual-reconcile"
        journal_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            journal_dir / f"{post_id}.json",
            json.dumps({"previous": previous.to_dict(), "proposed": proposed.to_dict(), "duplicates": report["duplicates"]}, ensure_ascii=False, indent=2),
        )
        store.commit(post_id, proposed)
        if store.load(post_id).to_dict() != proposed.to_dict():
            raise RuntimeError("visual reconciliation state readback mismatch")
    append_telemetry(root, "v2_visual_media_reconciled", post_id=post_id, duplicates=len(duplicate_ids), effective_missing=proposed.media.missing)
    report.update({"writes": 1, "readback": True, "invalid_media": report["duplicates"], "effective_missing": proposed.media.missing})
    return report
