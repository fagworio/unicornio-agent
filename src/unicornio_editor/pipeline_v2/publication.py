"""Read-only diagnosis and opt-in repair of WP/V2 publication divergence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..manifest import META_READY_MANIFEST, manifest_hash, manifest_matches, parse_manifest
from ..observability import append_telemetry
from ..state import read_state
from .model import LifecycleState, Phase, WorkState


def _meta(post: dict[str, Any]) -> dict[str, Any]:
    value = post.get("meta")
    return value if isinstance(value, dict) else {}


def _v2_state(post: dict[str, Any]) -> WorkState | None:
    raw = _meta(post).get("_hermes_work_state")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    if not isinstance(raw, dict):
        return None
    try:
        return WorkState.from_dict(raw)
    except (TypeError, ValueError, KeyError):
        return None


def _content_hash(post: dict[str, Any]) -> str:
    content = post.get("content")
    raw = content.get("raw") if isinstance(content, dict) else ""
    return hashlib.sha256(str(raw or "").encode("utf-8")).hexdigest()


def _precondition_signature(
    post: dict[str, Any],
    *,
    legacy_state: str | None,
    v2_state: WorkState | None,
    manifest_raw: str | None,
    ready_hash: str,
) -> str:
    value = {
        "status": post.get("status"),
        "legacy_state": legacy_state,
        "v2_state": v2_state.to_dict() if v2_state else None,
        "manifest": manifest_raw,
        "ready_hash": ready_hash,
        "content_hash": _content_hash(post),
        "featured_media": post.get("featured_media"),
        "date": post.get("date"),
        "date_gmt": post.get("date_gmt"),
    }
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def audit_publication_post(
    client: Any,
    post_id: int,
    *,
    policy_version: int = 2,
) -> dict[str, Any]:
    """Inspect one explicit post without writing to WordPress."""
    try:
        post = client.get_post(int(post_id))
    except Exception as exc:  # noqa: BLE001 - report per explicit ID
        return {
            "post_id": int(post_id),
            "read_error": f"{type(exc).__name__}: {exc}",
            "eligible": False,
            "recommended_action": "retry_read_only_get",
        }
    meta = _meta(post)
    legacy_state = str(read_state(post).get("state") or "") or None
    v2_state = _v2_state(post)
    manifest_raw = meta.get(META_READY_MANIFEST)
    manifest = parse_manifest(manifest_raw)
    ready_hash = str(meta.get("_hermes_ready_hash") or "")
    hash_matches = bool(manifest and ready_hash and manifest_hash(manifest) == ready_hash)
    manifest_matches_post = bool(
        manifest
        and ready_hash
        and manifest_matches(post, manifest, ready_hash, policy_version=policy_version)
    )
    wp_status = str(post.get("status") or "")
    v2_value = v2_state.state.value if v2_state else None
    divergence: list[str] = []
    if wp_status == "publish" and v2_value == LifecycleState.READY.value:
        divergence.append("wordpress_publish_v2_ready")
    if wp_status == "publish" and legacy_state != "published":
        divergence.append("legacy_not_published")
    if v2_value == LifecycleState.READY.value and not hash_matches:
        divergence.append("ready_manifest_hash_invalid")
    if v2_value == LifecycleState.READY.value and not manifest_matches_post:
        divergence.append("ready_manifest_does_not_match_post")
    eligible = bool(
        wp_status == "publish"
        and legacy_state == "published"
        and v2_state is not None
        and v2_state.state is LifecycleState.READY
        and hash_matches
        and manifest_matches_post
    )
    signature = _precondition_signature(
        post,
        legacy_state=legacy_state,
        v2_state=v2_state,
        manifest_raw=manifest_raw if isinstance(manifest_raw, str) else None,
        ready_hash=ready_hash,
    )
    if eligible:
        recommended_action = "reconcile_v2_published"
    elif divergence:
        recommended_action = "do_not_modify_without_new_evidence"
    else:
        recommended_action = "no_reconciliation_needed"
    return {
        "post_id": int(post_id),
        "wordpress_status": wp_status,
        "legacy_state": legacy_state,
        "v2_state": v2_value,
        "v2_phase": v2_state.phase.value if v2_state else None,
        "divergence": divergence,
        "eligible": eligible,
        "recommended_action": recommended_action,
        "manifest": {
            "present": manifest is not None,
            "hash_present": bool(ready_hash),
            "hash_matches": hash_matches,
            "matches_post": manifest_matches_post,
            "post_id": manifest.get("post_id") if manifest else None,
        },
        "previous_v2_state": v2_state.to_dict() if v2_state else None,
        "media": v2_state.media.to_dict() if v2_state else None,
        "precondition_signature": signature,
    }


def audit_publication_posts(
    client: Any,
    post_ids: list[int] | tuple[int, ...],
    *,
    policy_version: int = 2,
) -> dict[str, Any]:
    """Audit only the explicitly supplied IDs; never scans or writes."""
    reports = [audit_publication_post(client, post_id, policy_version=policy_version) for post_id in post_ids]
    return {
        "command": "v2-audit-publication",
        "read_only": True,
        "post_ids": [int(post_id) for post_id in post_ids],
        "divergences": sum(bool(report.get("divergence")) for report in reports),
        "eligible_for_reconciliation": sum(bool(report.get("eligible")) for report in reports),
        "posts": reports,
    }


def reconcile_published_v2(
    client: Any,
    config: Any,
    root: Path | str,
    post_ids: list[int] | tuple[int, ...],
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Repair eligible WP-published/V2-ready posts, only with ``apply``.

    The repair writes only ``_hermes_work_state``. It never calls ``publish``
    and rechecks the complete precondition after taking the per-post lock.
    """
    root = Path(root)
    before = audit_publication_posts(client, post_ids, policy_version=config.policy_version)
    reports = list(before["posts"])
    reconciled: list[int] = []
    skipped: list[dict[str, Any]] = []
    if not apply:
        return {
            **before,
            "command": "v2-reconcile-publication",
            "apply": False,
            "reconciled": [],
            "skipped": [],
        }
    if getattr(config, "dry_run", True):
        return {
            **before,
            "command": "v2-reconcile-publication",
            "apply": True,
            "reconciled": [],
            "skipped": [{"post_id": report["post_id"], "reason": "dry_run_enabled"} for report in reports if report.get("eligible")],
        }
    from ..workflow import _acquire_post_lock

    for report in reports:
        post_id = int(report["post_id"])
        if not report.get("eligible"):
            if report.get("divergence"):
                skipped.append({"post_id": post_id, "reason": "precondition_not_eligible"})
            continue
        try:
            with _acquire_post_lock(root, config, post_id):
                current = audit_publication_post(client, post_id, policy_version=config.policy_version)
                if current.get("precondition_signature") != report.get("precondition_signature"):
                    skipped.append({"post_id": post_id, "reason": "changed_after_scan"})
                    continue
                post = client.get_post(post_id)
                if _precondition_signature(
                    post,
                    legacy_state=str(read_state(post).get("state") or "") or None,
                    v2_state=_v2_state(post),
                    manifest_raw=_meta(post).get(META_READY_MANIFEST)
                    if isinstance(_meta(post).get(META_READY_MANIFEST), str)
                    else None,
                    ready_hash=str(_meta(post).get("_hermes_ready_hash") or ""),
                ) != report.get("precondition_signature"):
                    skipped.append({"post_id": post_id, "reason": "changed_before_write"})
                    continue
                state = _v2_state(post)
                if state is None or state.state is not LifecycleState.READY:
                    skipped.append({"post_id": post_id, "reason": "v2_state_changed"})
                    continue
                updated = replace(state, state=LifecycleState.PUBLISHED, phase=Phase.PUBLISH, blocker=None)
                meta = _meta(post)
                payload_meta = {"_hermes_work_state": json.dumps(updated.to_dict(), ensure_ascii=False, separators=(",", ":"))}
                client.update_post(post_id, {"meta": payload_meta})
                verified_post = client.get_post(post_id)
                verified = audit_publication_post(client, post_id, policy_version=config.policy_version)
                verified_state = _v2_state(verified_post)
                immutable_ok = (
                    verified_post.get("status") == "publish"
                    and str(read_state(verified_post).get("state") or "") == "published"
                    and verified_state == updated
                    and _content_hash(verified_post) == _content_hash(post)
                    and verified_post.get("featured_media") == post.get("featured_media")
                    and verified_post.get("date") == post.get("date")
                    and verified_post.get("date_gmt") == post.get("date_gmt")
                    and _meta(verified_post).get(META_READY_MANIFEST) == meta.get(META_READY_MANIFEST)
                    and _meta(verified_post).get("_hermes_ready_hash") == meta.get("_hermes_ready_hash")
                )
                if not immutable_ok or verified.get("v2_state") != "published":
                    raise RuntimeError("publication reconciliation read-back mismatch")
                append_telemetry(root, "v2_publication_reconciled", post_id=post_id, previous_state="ready")
                reconciled.append(post_id)
        except Exception as exc:  # noqa: BLE001 - report each explicit ID
            append_telemetry(root, "v2_publication_reconcile_failed", post_id=post_id, error=str(exc)[:200])
            skipped.append({"post_id": post_id, "reason": f"reconcile_error: {exc}"})
    return {
        **before,
        "command": "v2-reconcile-publication",
        "apply": True,
        "reconciled": reconciled,
        "skipped": skipped,
    }
