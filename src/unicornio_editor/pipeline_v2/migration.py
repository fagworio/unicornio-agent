"""Explicit, idempotent migrations for persisted V2 operational state."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .classifier import MEDIA_BLOCKERS
from .model import CURRENT_RETRY_POLICY_VERSION, LifecycleState, RetryInfo, WorkState


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
