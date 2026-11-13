"""Shared, fail-closed eligibility decisions for the V1 editorial surface.

The V1 commands and reports used to infer ``NEW`` from a missing
``_hermes_state``.  That is safe only for a genuinely legacy post.  A V2
checkpoint is authoritative even when its compatibility projection was not
written; malformed V2 state is uncertain, never new.

This module is deliberately read-only.  It does not migrate metadata,
attachments, or filesystem evidence.  Explicit migration remains the only
writer for a legacy state projection.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .pipeline_v2.model import LifecycleState, WorkState
from .state import (
    STATE_AWAITING_HUMAN,
    STATE_BLOCKED,
    STATE_NEW,
    STATE_PARTIAL,
    STATE_PUBLISHED,
    STATE_READY,
    STATE_SKIPPED,
    STATE_UNCERTAIN,
    cooldown_expired,
    read_state,
)


@dataclass(frozen=True)
class Eligibility:
    """Resolved state and the safe action allowed by the current metadata."""

    state: str
    source: str
    reason: str
    eligible_for_v1: bool
    cooldown_expired: bool
    attempts: int = 0
    next_retry_at: str = ""
    v2_state: WorkState | None = None

    @property
    def is_v2(self) -> bool:
        return self.source.startswith("v2")


def decode_v2_state(post: dict[str, Any]) -> tuple[bool, WorkState | None, str]:
    """Return ``(present, state, reason)`` without treating invalid V2 as V1."""

    meta = post.get("meta") if isinstance(post.get("meta"), dict) else {}
    if "_hermes_work_state" not in meta or meta.get("_hermes_work_state") in (None, ""):
        return False, None, "v2_state_absent"
    raw = meta.get("_hermes_work_state")
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(value, dict):
            raise ValueError("V2 state is not an object")
        state = WorkState.from_dict(value)
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return True, None, f"v2_state_invalid:{type(exc).__name__}"
    return True, state, "v2_state_valid"


def resolve_post_eligibility(post: dict[str, Any], root: Path | None = None) -> Eligibility:
    """Resolve one post once for queue, cards and mutating V1 commands.

    A valid V2 state remains V2; it is never projected to V1 ``NEW`` here.
    An invalid V2 state is quarantined as uncertain for this surface.  Only a
    post with no V2 marker can use the legacy filesystem fallback.
    """

    present, v2_state, v2_reason = decode_v2_state(post)
    if present:
        if v2_state is None:
            return Eligibility(
                state=STATE_UNCERTAIN,
                source="v2_invalid",
                reason=v2_reason,
                eligible_for_v1=False,
                cooldown_expired=False,
            )
        state = v2_state.state.value
        next_retry_at = v2_state.retry.next_at or ""
        expired = cooldown_expired(next_retry_at)
        return Eligibility(
            state=state,
            source="v2",
            reason="v2_pending_ready_for_v2_runtime" if state == LifecycleState.PENDING.value and expired else v2_reason,
            eligible_for_v1=False,
            cooldown_expired=expired,
            attempts=v2_state.retry.attempts,
            next_retry_at=next_retry_at,
            v2_state=v2_state,
        )

    info = read_state(post)
    state = info.get("state")
    source = "v1_meta" if state is not None else "legacy"
    if state is None:
        marker_dir = (root / "backups" / str(post.get("id"))) if root is not None else None
        if marker_dir is not None:
            if (marker_dir / "uncertain.json").is_file():
                state, source = STATE_UNCERTAIN, "legacy_filesystem"
            elif (marker_dir / "editorial.blocked.json").is_file():
                state, source = STATE_BLOCKED, "legacy_filesystem"
            elif (marker_dir / "editorial.latest.json").is_file():
                state, source = STATE_READY, "legacy_filesystem"
        if state is None:
            state, source = STATE_NEW, "legacy"

    next_retry_at = str(info.get("next_retry_at") or "")
    expired = cooldown_expired(next_retry_at)
    v1_work_states = {STATE_NEW, STATE_BLOCKED, STATE_PARTIAL, STATE_UNCERTAIN}
    eligible = state in v1_work_states and expired
    reason = "v1_eligible" if eligible else f"v1_{state}_not_eligible"
    return Eligibility(
        state=state,
        source=source,
        reason=reason,
        eligible_for_v1=eligible,
        cooldown_expired=expired,
        attempts=int(info.get("attempts") or 0),
        next_retry_at=next_retry_at,
    )


def assert_v1_mutation_allowed(post: dict[str, Any], root: Path, *, operation: str) -> Eligibility:
    """Fail closed before V1 prepare/apply/retry can touch a V2 post."""

    eligibility = resolve_post_eligibility(post, root)
    if eligibility.is_v2:
        raise RuntimeError(
            f"{operation} bloqueado para post V2 ({post.get('id')}): "
            f"{eligibility.reason}; use o executor V2 ou migração explícita"
        )
    retry_reopen = operation == "retry" and eligibility.state == STATE_AWAITING_HUMAN
    if not eligibility.eligible_for_v1 and not retry_reopen:
        raise RuntimeError(
            f"{operation} bloqueado para post {post.get('id')}: {eligibility.reason}"
        )
    return eligibility


__all__ = [
    "Eligibility",
    "assert_v1_mutation_allowed",
    "decode_v2_state",
    "resolve_post_eligibility",
]
