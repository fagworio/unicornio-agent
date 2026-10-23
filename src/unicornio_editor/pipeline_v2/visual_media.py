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
from ..media.visual_identity import VisualIdentityDecision, fingerprint_path, verify_candidate_identity
from ..observability import append_telemetry
from .model import FeaturedProgress, FeaturedStatus, InlineMedia, LifecycleState, MediaProgress
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


def _identity_complete(asset: Any) -> bool:
    verification = getattr(asset, "visual_verification", {})
    decision = str(verification.get("decision") or "") if isinstance(verification, dict) else ""
    return bool(
        str(getattr(asset, "sha256", "") or "")
        and str(getattr(asset, "phash", "") or "")
        and str(getattr(asset, "visual_group_id", "") or "")
        and decision in {"INITIAL", "DIFFERENT"}
    )


def _visual_record(decision: Any, *, baseline_media_ids: list[int] | None = None) -> dict[str, Any]:
    return {
        "method": str(getattr(decision, "reason", "") or ""),
        "decision": str(getattr(decision, "decision", "") or "UNVERIFIED"),
        "duplicate_of": str(getattr(decision, "duplicate_of", "") or ""),
        "confidence": _decision_confidence(decision),
        "comparator_version": 1,
        "baseline_media_ids": list(baseline_media_ids or []),
    }


def materialize_media_identity(
    client: Any,
    config: Any,
    root: Path,
    media: MediaProgress,
    *,
    post_id: int | None = None,
    run_id: str = "",
) -> MediaProgress:
    """Recover missing final-image identity without changing external state."""
    get_media = getattr(client, "get_media", None)
    if not callable(get_media):
        return media
    needs_recovery = any(not _identity_complete(item) for item in media.inline) or (
        media.featured.status is FeaturedStatus.VALID
        and media.featured.media_id
        and (not str(media.featured.media_url or "").strip() or not _identity_complete(media.featured))
    )
    if not needs_recovery:
        return media

    updated_featured = media.featured
    updated_inline: list[InlineMedia] = []
    retained: list[dict[str, Any]] = []
    comparison_budget = [0]
    baseline_unavailable = False

    def recovery_error(
        exc: BaseException | None,
        *,
        media_id: int | None,
        role: str,
        stage: str,
        cause: str = "",
    ) -> None:
        fields: dict[str, Any] = {
            "media_id": media_id,
            "role": role,
            "stage": stage,
            "error": type(exc).__name__ if exc is not None else "comparison_inconclusive",
        }
        if post_id is not None:
            fields["post_id"] = int(post_id)
        if run_id:
            fields["run_id"] = str(run_id)
        if cause:
            fields["cause"] = str(cause)[:120]
        try:
            append_telemetry(root, "visual_identity_recovery_failed", **fields)
        except Exception:  # noqa: BLE001 - diagnostics cannot change lifecycle
            pass

    def attachment_url(media_id: int) -> str:
        attachment = get_media(media_id)
        source_url = str((attachment or {}).get("source_url") or "").strip()
        if not source_url:
            raise ValueError("attachment has no source_url")
        return source_url

    with tempfile.TemporaryDirectory(prefix="unicornio-identity-recovery-") as directory:
        temp_root = Path(directory)
        if (
            media.featured.status is FeaturedStatus.VALID
            and media.featured.media_id
        ):
            featured = media.featured
            row = featured.to_dict()
            try:
                resolved_url = attachment_url(int(featured.media_id))
                local = download_image(
                    resolved_url,
                    temp_root / f"featured-{featured.media_id}.img",
                    url_policy=str(getattr(config, "remote_url_policy", "audit")),
                )
                identity = fingerprint_path(local)
                row.update(identity.to_dict())
                row["local_path"] = str(local)
                verification = (
                    dict(featured.visual_verification)
                    if _identity_complete(featured)
                    else {"decision": "INITIAL", "method": "fingerprint", "duplicate_of": "", "confidence": None, "comparator_version": 1}
                )
                updated_featured = FeaturedProgress(
                    featured.status, featured.media_id, resolved_url,
                    identity.sha256, identity.phash, identity.visual_group_id,
                    verification,
                    featured.alt_text, featured.credit_text,
                    featured.width, featured.height,
                )
                retained.append({**row, "visual_verification": verification})
            except Exception as exc:  # noqa: BLE001 - keep the post retryable
                # A known featured asset whose bytes cannot be materialized is
                # not the same as an absent baseline.  Do not let the first
                # inline asset become INITIAL while this comparison anchor is
                # unavailable.
                baseline_unavailable = True
                recovery_error(exc, media_id=int(featured.media_id), role="featured", stage="materialize")

        for item in media.inline:
            row = item.to_dict()
            try:
                local = download_image(
                    attachment_url(int(item.media_id)),
                    temp_root / f"inline-{item.media_id}.img",
                    url_policy=str(getattr(config, "remote_url_policy", "audit")),
                )
                identity = fingerprint_path(local)
                row.update(identity.to_dict())
                row["local_path"] = str(local)
                existing_verification = item.visual_verification if isinstance(item.visual_verification, dict) else {}
                existing_baselines = {
                    str(value) for value in existing_verification.get("baseline_media_ids", [])
                } if isinstance(existing_verification.get("baseline_media_ids", []), (list, tuple)) else set()
                featured_id = str(media.featured.media_id or "") if media.featured.status is FeaturedStatus.VALID else ""
                bytes_changed = bool(item.sha256 and item.sha256 != identity.sha256)
                needs_featured_recheck = bool(
                    _identity_complete(item)
                    and featured_id
                    and (
                        str(existing_verification.get("decision") or "") == "INITIAL"
                        or featured_id not in existing_baselines
                    )
                )
                needs_identity_recheck = bytes_changed or needs_featured_recheck
                if _identity_complete(item) and not needs_identity_recheck:
                    updated_inline.append(item)
                    retained.append(row)
                    continue
                comparison_error_logged = False
                if baseline_unavailable:
                    identity_value = identity
                    decision = VisualIdentityDecision(
                        "UNVERIFIED", False,
                        reason="baseline_identity_unavailable",
                    )
                else:
                    try:
                        baseline_media_ids = [
                            int(value["media_id"])
                            for value in retained
                            if str(value.get("media_id") or "").isdigit()
                        ]
                        identity_value, decision = verify_candidate_identity(
                            local,
                            candidate_id=str(item.media_id),
                            baseline=retained,
                            config=config,
                            root=root,
                            comparison_budget=comparison_budget,
                        )
                    except Exception as exc:  # noqa: BLE001 - defer provider/runtime faults
                        baseline_unavailable = True
                        recovery_error(exc, media_id=int(item.media_id), role="inline", stage="compare")
                        comparison_error_logged = True
                        identity_value = identity
                        decision = VisualIdentityDecision(
                            "UNVERIFIED", False,
                            reason="baseline_identity_unavailable",
                        )
                baseline_media_ids = [
                    int(value["media_id"])
                    for value in retained
                    if str(value.get("media_id") or "").isdigit()
                ]
                verification = _visual_record(decision, baseline_media_ids=baseline_media_ids)
                updated_inline.append(replace(
                    item,
                    sha256=identity_value.sha256,
                    phash=identity_value.phash,
                    visual_group_id=identity_value.visual_group_id,
                    visual_verification=verification,
                ))
                row.update(identity_value.to_dict())
                row["visual_verification"] = verification
                if decision.decision == "UNVERIFIED":
                    # The bytes were recovered but the comparison anchor was
                    # not. Later assets must not be promoted to INITIAL as if
                    # this unresolved asset were absent.
                    baseline_unavailable = True
                    if not comparison_error_logged:
                        recovery_error(
                            None,
                            media_id=int(item.media_id),
                            role="inline",
                            stage="compare",
                            cause=str(decision.reason or "identity_unverified"),
                        )
                if decision.verified and decision.decision not in {"SAME_IMAGE", "SAME_ART_CROP"}:
                    retained.append(row)
            except Exception as exc:  # noqa: BLE001 - preserve evidence for retry
                updated_inline.append(item)
                baseline_unavailable = True
                recovery_error(exc, media_id=int(item.media_id), role="inline", stage="materialize")

    return replace(media, inline=tuple(updated_inline), featured=updated_featured)


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
    if apply:
        from .quarantine import assert_post_not_quarantined

        assert_post_not_quarantined(root, post_id, operation="visual_media_reconciliation")
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
            json.dumps({"status": "prepared", "previous": previous.to_dict(), "proposed": proposed.to_dict(), "duplicates": report["duplicates"]}, ensure_ascii=False, indent=2),
        )
        store.commit(post_id, proposed)
        if store.load(post_id).to_dict() != proposed.to_dict():
            raise RuntimeError("visual reconciliation state readback mismatch")
        atomic_write_text(
            journal_dir / f"{post_id}.json",
            json.dumps({"status": "confirmed", "previous": previous.to_dict(), "proposed": proposed.to_dict(), "duplicates": report["duplicates"]}, ensure_ascii=False, indent=2),
        )
    append_telemetry(root, "v2_visual_media_reconciled", post_id=post_id, duplicates=len(duplicate_ids), effective_missing=proposed.media.missing)
    report.update({"writes": 1, "readback": True, "invalid_media": report["duplicates"], "effective_missing": proposed.media.missing})
    return report
