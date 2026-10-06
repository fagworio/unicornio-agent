"""Explicit, idempotent migrations for persisted V2 operational state."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from .classifier import EDITORIAL_BLOCKERS, MEDIA_BLOCKERS
from .model import BlockerCode, CURRENT_RETRY_POLICY_VERSION, LifecycleState, Phase, RetryInfo, WorkState


HISTORICAL_MEDIA_HUMAN_REQUIRED_IDS = frozenset({
    114835, 114847, 114851, 114855, 114885,
    114937, 115025, 115027, 115035,
})
HISTORICAL_MEDIA_LOSS_IDS = frozenset({114949, 114984, 114987})
HISTORICAL_MEDIA_DUPLICATE_ID = 114840
HISTORICAL_EDITORIAL_QUALITY_IDS = frozenset({115002, 115004})
HISTORICAL_VISION_PROVIDER_ID = 115025


def _as_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _is_future(value: str | None, now: datetime) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed > now


def _read_state(post: dict[str, Any]) -> WorkState | None:
    meta = post.get("meta") if isinstance(post, dict) else None
    raw = meta.get("_hermes_work_state") if isinstance(meta, dict) else None
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


def _migrate_state(state: WorkState, now: datetime) -> WorkState | None:
    if state.state is not LifecycleState.PENDING:
        return None
    if state.blocker not in MEDIA_BLOCKERS:
        return None
    if state.retry.policy_version >= CURRENT_RETRY_POLICY_VERSION:
        return None
    if not _is_future(state.retry.next_at, now):
        return None
    retry = RetryInfo(
        attempts=state.retry.attempts,
        no_progress=state.retry.no_progress,
        next_at=now.isoformat(timespec="seconds"),
        policy_version=CURRENT_RETRY_POLICY_VERSION,
    )
    return WorkState(
        state=state.state,
        phase=state.phase,
        blocker=state.blocker,
        retry=retry,
        relevance_approved=state.relevance_approved,
        media=state.media,
        version=state.version,
    )


def rebase_media_cooldowns(
    client: Any,
    *,
    apply: bool = False,
    limit: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Rebase only future media cooldowns created by an older policy.

    The migration is deliberately narrow: it scans pending posts, requires a
    V2 pending state with a media blocker and a future ``next_at``, and changes
    only the retry timestamp plus the persisted policy marker. Attempts,
    no-progress, phase, blocker, relevance and media progress remain intact.
    Without ``apply`` it is a read-only report.
    """
    current = _as_utc(now)
    scanned = 0
    candidates: list[dict[str, Any]] = []
    seen: set[int] = set()
    page = 1
    while True:
        posts = client.list_pending(page=page, per_page=100, status="pending")
        if not posts:
            break
        for post in posts:
            post_id = post.get("id") if isinstance(post, dict) else None
            if not isinstance(post_id, int) or post_id in seen:
                continue
            seen.add(post_id)
            scanned += 1
            state = _read_state(post)
            migrated = _migrate_state(state, current) if state is not None else None
            if migrated is None:
                continue
            candidates.append({
                "post_id": post_id,
                "state": state,
                "migrated": migrated,
            })
            if limit and len(candidates) >= limit:
                break
        if limit and len(candidates) >= limit:
            break
        if len(posts) < 100:
            break
        page += 1

    migrated_ids: list[int] = []
    if apply:
        for item in candidates:
            post_id = item["post_id"]
            post = client.get_post(post_id)
            current_state = _read_state(post)
            migrated = _migrate_state(current_state, current)
            if migrated is None:
                # The post changed after the scan; never overwrite a newer
                # pipeline decision with a stale migration candidate.
                continue
            meta = post.get("meta") if isinstance(post, dict) else None
            if not isinstance(meta, dict):
                raise RuntimeError(f"post {post_id} has no editable meta payload")
            updated_meta = dict(meta)
            updated_meta["_hermes_work_state"] = json.dumps(
                migrated.to_dict(), ensure_ascii=False, separators=(",", ":")
            )
            client.update_post(post_id, {"meta": updated_meta})
            verified = _read_state(client.get_post(post_id))
            if verified != migrated:
                raise RuntimeError(f"V2 retry migration read-back mismatch for post {post_id}")
            migrated_ids.append(post_id)

    return {
        "command": "v2-rebase-cooldowns",
        "apply": bool(apply),
        "policy_version": CURRENT_RETRY_POLICY_VERSION,
        "scanned": scanned,
        "candidates": len(candidates),
        "migrated": len(migrated_ids) if apply else 0,
        "post_ids": migrated_ids if apply else [item["post_id"] for item in candidates],
        "next_at": current.isoformat(timespec="seconds"),
    }


def _migrate_editorial_state(state: WorkState, now: datetime) -> WorkState | None:
    if state.state is not LifecycleState.PENDING:
        return None
    if state.phase is not Phase.EDITORIAL:
        return None
    if state.blocker not in EDITORIAL_BLOCKERS:
        return None
    if state.retry.policy_version >= CURRENT_RETRY_POLICY_VERSION:
        return None
    if not _is_future(state.retry.next_at, now):
        return None
    retry = RetryInfo(
        attempts=state.retry.attempts,
        no_progress=state.retry.no_progress,
        next_at=now.isoformat(timespec="seconds"),
        policy_version=CURRENT_RETRY_POLICY_VERSION,
        phase_attempts=0,
    )
    return WorkState(
        state=state.state,
        phase=state.phase,
        blocker=state.blocker,
        retry=retry,
        relevance_approved=state.relevance_approved,
        media=state.media,
        version=state.version,
    )


def rebase_editorial_retries(
    client: Any,
    *,
    apply: bool = False,
    limit: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Release future editorial cooldowns created by the global retry budget."""
    current = _as_utc(now)
    scanned = 0
    candidates: list[dict[str, Any]] = []
    seen: set[int] = set()
    page = 1
    while True:
        posts = client.list_pending(page=page, per_page=100, status="pending")
        if not posts:
            break
        for post in posts:
            post_id = post.get("id") if isinstance(post, dict) else None
            if not isinstance(post_id, int) or post_id in seen:
                continue
            seen.add(post_id)
            scanned += 1
            state = _read_state(post)
            migrated = _migrate_editorial_state(state, current) if state is not None else None
            if migrated is None:
                continue
            candidates.append({"post_id": post_id})
            if limit and len(candidates) >= limit:
                break
        if limit and len(candidates) >= limit:
            break
        if len(posts) < 100:
            break
        page += 1

    migrated_ids: list[int] = []
    if apply:
        for item in candidates:
            post_id = item["post_id"]
            post = client.get_post(post_id)
            current_state = _read_state(post)
            migrated = _migrate_editorial_state(current_state, current)
            if migrated is None:
                continue
            meta = post.get("meta") if isinstance(post, dict) else None
            if not isinstance(meta, dict):
                raise RuntimeError(f"post {post_id} has no editable meta payload")
            updated_meta = dict(meta)
            updated_meta["_hermes_work_state"] = json.dumps(
                migrated.to_dict(), ensure_ascii=False, separators=(",", ":")
            )
            client.update_post(post_id, {"meta": updated_meta})
            verified = _read_state(client.get_post(post_id))
            if verified != migrated:
                raise RuntimeError(f"V2 editorial retry migration read-back mismatch for post {post_id}")
            migrated_ids.append(post_id)

    return {
        "command": "v2-rebase-editorial-retries",
        "apply": bool(apply),
        "policy_version": CURRENT_RETRY_POLICY_VERSION,
        "scanned": scanned,
        "candidates": len(candidates),
        "migrated": len(migrated_ids) if apply else 0,
        "post_ids": migrated_ids if apply else [item["post_id"] for item in candidates],
        "next_at": current.isoformat(timespec="seconds"),
    }


def _repair_historical_media_state(state: WorkState, now: datetime) -> WorkState | None:
    """Reopen only the known false terminal states from the v3 drain."""
    if state.state is not LifecycleState.HUMAN_REQUIRED:
        return None
    if state.phase is not Phase.MEDIA or state.blocker not in MEDIA_BLOCKERS:
        return None
    if state.retry.policy_version != CURRENT_RETRY_POLICY_VERSION:
        return None
    if state.retry.phase_attempts != 1 or state.retry.no_progress < 2:
        return None
    retry = RetryInfo(
        attempts=state.retry.attempts,
        no_progress=1,
        next_at=now.isoformat(timespec="seconds"),
        policy_version=CURRENT_RETRY_POLICY_VERSION,
        phase_attempts=state.retry.phase_attempts,
    )
    return WorkState(
        state=LifecycleState.PENDING,
        phase=Phase.MEDIA,
        blocker=state.blocker,
        retry=retry,
        relevance_approved=state.relevance_approved,
        media=state.media,
        version=state.version,
    )


def repair_historical_media_human_required(
    client: Any,
    *,
    apply: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Safely reopen the nine audited false HUMAN_REQUIRED media states.

    This is intentionally allowlisted and idempotent. It refuses any post
    outside the audited drain or whose current state no longer matches the
    exact bug signature, so it cannot become a generic HUMAN_REQUIRED reset.
    The three known media-loss posts are reported separately and are never
    restored without an independently verified media identity.
    """
    current = _as_utc(now)
    candidates: list[int] = []
    skipped: list[dict[str, Any]] = []
    for post_id in sorted(HISTORICAL_MEDIA_HUMAN_REQUIRED_IDS):
        try:
            post = client.get_post(post_id)
        except Exception as exc:  # noqa: BLE001 - report one unavailable post
            skipped.append({"post_id": post_id, "reason": f"read_error: {exc}"})
            continue
        state = _read_state(post)
        migrated = _repair_historical_media_state(state, current) if state is not None else None
        if migrated is None:
            skipped.append({"post_id": post_id, "reason": "state_signature_mismatch"})
            continue
        candidates.append(post_id)

    migrated_ids: list[int] = []
    if apply:
        for post_id in candidates:
            post = client.get_post(post_id)
            state = _read_state(post)
            migrated = _repair_historical_media_state(state, current) if state is not None else None
            if migrated is None:
                skipped.append({"post_id": post_id, "reason": "changed_after_scan"})
                continue
            meta = post.get("meta") if isinstance(post, dict) else None
            if not isinstance(meta, dict):
                raise RuntimeError(f"post {post_id} has no editable meta payload")
            updated_meta = dict(meta)
            updated_meta["_hermes_work_state"] = json.dumps(
                migrated.to_dict(), ensure_ascii=False, separators=(",", ":")
            )
            client.update_post(post_id, {"meta": updated_meta})
            verified = _read_state(client.get_post(post_id))
            if verified != migrated:
                raise RuntimeError(f"historical media repair read-back mismatch for post {post_id}")
            migrated_ids.append(post_id)

    return {
        "command": "v2-repair-historical-media",
        "apply": bool(apply),
        "policy_version": CURRENT_RETRY_POLICY_VERSION,
        "audited_human_required_ids": sorted(HISTORICAL_MEDIA_HUMAN_REQUIRED_IDS),
        "candidates": len(candidates),
        "migrated": len(migrated_ids) if apply else 0,
        "post_ids": migrated_ids if apply else candidates,
        "skipped": skipped,
        "media_loss_requires_evidence": sorted(HISTORICAL_MEDIA_LOSS_IDS),
        "next_at": current.isoformat(timespec="seconds"),
    }


def _repair_known_terminal_state(
    post_id: int,
    state: WorkState,
    now: datetime,
    *,
    max_rework_attempts: int = 3,
) -> WorkState | None:
    """Reopen only the three terminal states caused by the identified bugs."""
    if state.state is not LifecycleState.HUMAN_REQUIRED:
        return None
    if state.retry.policy_version != CURRENT_RETRY_POLICY_VERSION:
        return None
    if post_id == HISTORICAL_MEDIA_DUPLICATE_ID:
        if (
            state.phase is not Phase.MEDIA
            or state.blocker is not BlockerCode.MEDIA_DUPLICATE
            or state.media.required != 4
            or state.media.accepted != 3
            or state.retry.no_progress != 2
        ):
            return None
        retry = RetryInfo(
            attempts=state.retry.attempts,
            no_progress=1,
            next_at=now.isoformat(timespec="seconds"),
            policy_version=CURRENT_RETRY_POLICY_VERSION,
            phase_attempts=state.retry.phase_attempts,
        )
        return WorkState(
            state=LifecycleState.PENDING,
            phase=Phase.MEDIA,
            blocker=state.blocker,
            retry=retry,
            relevance_approved=state.relevance_approved,
            media=state.media,
            version=state.version,
        )
    if post_id in HISTORICAL_EDITORIAL_QUALITY_IDS:
        if (
            state.phase is not Phase.EDITORIAL
            or state.blocker is not BlockerCode.TEXT_QUALITY
            or state.retry.phase_attempts != max_rework_attempts
        ):
            return None
        retry = RetryInfo(
            attempts=state.retry.attempts,
            no_progress=state.retry.no_progress,
            next_at=now.isoformat(timespec="seconds"),
            policy_version=CURRENT_RETRY_POLICY_VERSION,
            phase_attempts=max(0, max_rework_attempts - 1),
        )
        return WorkState(
            state=LifecycleState.PENDING,
            phase=Phase.EDITORIAL,
            blocker=state.blocker,
            retry=retry,
            relevance_approved=state.relevance_approved,
            media=state.media,
            version=state.version,
        )
    return None


def repair_known_terminal_states(
    client: Any,
    *,
    apply: bool = False,
    now: datetime | None = None,
    max_rework_attempts: int = 3,
) -> dict[str, Any]:
    """Reopen the exact MEDIA_DUPLICATE/TEXT_QUALITY regressions once."""
    current = _as_utc(now)
    post_ids = (HISTORICAL_MEDIA_DUPLICATE_ID, *sorted(HISTORICAL_EDITORIAL_QUALITY_IDS))
    candidates: list[int] = []
    skipped: list[dict[str, Any]] = []
    for post_id in post_ids:
        try:
            post = client.get_post(post_id)
        except Exception as exc:  # noqa: BLE001 - preserve per-post audit
            skipped.append({"post_id": post_id, "reason": f"read_error: {exc}"})
            continue
        state = _read_state(post)
        migrated = _repair_known_terminal_state(
            post_id, state, current, max_rework_attempts=max_rework_attempts
        ) if state is not None else None
        if migrated is None:
            skipped.append({"post_id": post_id, "reason": "state_signature_mismatch"})
            continue
        candidates.append(post_id)

    migrated_ids: list[int] = []
    if apply:
        for post_id in candidates:
            post = client.get_post(post_id)
            state = _read_state(post)
            migrated = _repair_known_terminal_state(
                post_id, state, current, max_rework_attempts=max_rework_attempts
            ) if state is not None else None
            if migrated is None:
                skipped.append({"post_id": post_id, "reason": "changed_after_scan"})
                continue
            meta = post.get("meta") if isinstance(post, dict) else None
            if not isinstance(meta, dict):
                raise RuntimeError(f"post {post_id} has no editable meta payload")
            updated_meta = dict(meta)
            updated_meta["_hermes_work_state"] = json.dumps(
                migrated.to_dict(), ensure_ascii=False, separators=(",", ":")
            )
            client.update_post(post_id, {"meta": updated_meta})
            if _read_state(client.get_post(post_id)) != migrated:
                raise RuntimeError(f"known terminal repair read-back mismatch for post {post_id}")
            migrated_ids.append(post_id)

    return {
        "command": "v2-repair-known-terminal-states",
        "apply": bool(apply),
        "policy_version": CURRENT_RETRY_POLICY_VERSION,
        "allowlisted_post_ids": list(post_ids),
        "candidates": len(candidates),
        "migrated": len(migrated_ids) if apply else 0,
        "post_ids": migrated_ids if apply else candidates,
        "skipped": skipped,
        "next_at": current.isoformat(timespec="seconds"),
    }


def repair_media_provider_terminal_states(
    client: Any,
    *,
    apply: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reopen only the audited duplicate and Vision-provider terminal states."""
    current = _as_utc(now)
    allowlist = (HISTORICAL_MEDIA_DUPLICATE_ID, HISTORICAL_VISION_PROVIDER_ID)
    candidates: list[int] = []
    skipped: list[dict[str, Any]] = []

    prepared: dict[int, WorkState] = {}
    original: dict[int, WorkState] = {}
    for post_id in allowlist:
        try:
            post = client.get_post(post_id)
        except Exception as exc:  # noqa: BLE001 - preserve per-post audit
            skipped.append({"post_id": post_id, "reason": f"read_error: {exc}"})
            continue
        state = _read_state(post)
        if state is None or state.phase is not Phase.MEDIA or state.retry.policy_version != CURRENT_RETRY_POLICY_VERSION:
            skipped.append({"post_id": post_id, "reason": "state_signature_mismatch"})
            continue
        original[post_id] = state
        if post_id == HISTORICAL_MEDIA_DUPLICATE_ID:
            if (
                state.state is not LifecycleState.HUMAN_REQUIRED
                or state.blocker is not BlockerCode.MEDIA_DUPLICATE
                or state.retry.no_progress != 2
                or state.media.required != 4
                or state.media.accepted != 3
            ):
                skipped.append({"post_id": post_id, "reason": "state_signature_mismatch"})
                continue
            from ..media.visual_hash import image_hashes

            urls = [item.media_url for item in state.media.inline]
            hashes = image_hashes(urls)
            if any(not hashes.get(url) for url in urls):
                skipped.append({"post_id": post_id, "reason": "baseline_phash_unavailable"})
                continue
            inline = tuple(
                replace(item, phash=str(hashes[item.media_url]))
                for item in state.media.inline
            )
            media = replace(state.media, inline=inline)
            retry = replace(
                state.retry,
                no_progress=1,
                next_at=current.isoformat(timespec="seconds"),
            )
            prepared[post_id] = replace(
                state,
                state=LifecycleState.PENDING,
                retry=retry,
                media=media,
            )
            candidates.append(post_id)
            continue
        if (
            state.state is not LifecycleState.PENDING
            or state.blocker is not BlockerCode.MEDIA_INVALID
            or state.retry.attempts != 6
            or state.retry.phase_attempts != 1
            or state.retry.no_progress != 1
            or state.media.required != 4
            or state.media.accepted != 0
        ):
            skipped.append({"post_id": post_id, "reason": "state_signature_mismatch"})
            continue
        prepared[post_id] = replace(
            state,
            blocker=BlockerCode.PROVIDER_ERROR,
            retry=replace(
                state.retry,
                no_progress=0,
                next_at=current.isoformat(timespec="seconds"),
            ),
        )
        candidates.append(post_id)

    migrated_ids: list[int] = []
    if apply:
        for post_id in candidates:
            post = client.get_post(post_id)
            migrated = prepared.get(post_id)
            state = _read_state(post)
            if migrated is None or state is None or state != original.get(post_id):
                skipped.append({"post_id": post_id, "reason": "changed_after_scan"})
                continue
            meta = post.get("meta") if isinstance(post, dict) else None
            if not isinstance(meta, dict):
                raise RuntimeError(f"post {post_id} has no editable meta payload")
            updated_meta = dict(meta)
            updated_meta["_hermes_work_state"] = json.dumps(
                migrated.to_dict(), ensure_ascii=False, separators=(",", ":")
            )
            client.update_post(post_id, {"meta": updated_meta})
            if _read_state(client.get_post(post_id)) != migrated:
                raise RuntimeError(f"media provider repair read-back mismatch for post {post_id}")
            migrated_ids.append(post_id)

    return {
        "command": "v2-repair-media-provider-states",
        "apply": bool(apply),
        "policy_version": CURRENT_RETRY_POLICY_VERSION,
        "allowlisted_post_ids": list(allowlist),
        "candidates": len(candidates),
        "migrated": len(migrated_ids) if apply else 0,
        "post_ids": migrated_ids if apply else candidates,
        "skipped": skipped,
        "next_at": current.isoformat(timespec="seconds"),
    }
