"""Pure distance-to-ready ranking for V2 work states."""

from datetime import datetime, timezone
from collections.abc import Collection

from .model import BlockerCode, FeaturedStatus, LifecycleState, Phase, WorkState


def _manual_reopen_confirmed(item, state: WorkState) -> bool:
    if state.state is not LifecycleState.HUMAN_REQUIRED or not isinstance(item, tuple) or len(item) < 2:
        return False
    context = item[1] if isinstance(item[1], dict) else {}
    post = context.get("post") if isinstance(context.get("post"), dict) else {}
    meta = post.get("meta") if isinstance(post.get("meta"), dict) else {}
    return (
        post.get("status") == "pending"
        and bool(meta.get("_hermes_human_reopened_at"))
        and meta.get("_hermes_human_reopened_from") == "awaiting_human"
    )


def rank(state: WorkState, *, manually_reopened: bool = False) -> int:
    """Return a higher score for work closer to READY."""
    if state.state.value in {"ready", "published", "skipped"} or (
        state.state.value == "human_required" and not manually_reopened
    ):
        return -1000
    if state.blocker in {BlockerCode.FEATURED_MISSING, BlockerCode.FEATURED_INVALID, BlockerCode.FEATURED_VISION}:
        return 1000 if state.media.effective_missing == 0 else 500 - state.media.effective_missing
    if state.blocker in {BlockerCode.INLINE_MISSING, BlockerCode.MEDIA_INVALID, BlockerCode.MEDIA_DUPLICATE, BlockerCode.MEDIA_ORIGIN}:
        return 400 - state.media.effective_missing
    if state.phase is Phase.EDITORIAL:
        return 250
    if state.phase is Phase.RELEVANCE:
        return 100
    return 50


def _cooldown_expired(value: str | None, now: datetime) -> bool:
    return cooldown_status(value, now=now)["expired"]


def cooldown_status(value: str | None, *, now: datetime | None = None) -> dict[str, object]:
    """Return a timezone-safe, read-only interpretation of a retry cooldown."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    if not value:
        return {"value": value, "valid": True, "active": False, "expired": True}
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return {"value": value, "valid": False, "active": False, "expired": True}
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    active = parsed > current
    return {
        "value": value,
        "valid": True,
        "active": active,
        "expired": not active,
        "next_at_utc": parsed.isoformat(),
        "now_utc": current.isoformat(),
    }


def _eligible_queues(
    candidates,
    state_store,
    now: datetime,
    *,
    retry_post_ids: Collection[int] = (),
):
    """Return eligible work split into retry and new queues.

    Keeping the queues separate makes the fairness contract explicit: retries
    retain priority by distance to READY, while NEW work can fill capacity that
    retries do not use. The returned entries keep the original item shape so
    callers can preserve the preview authorization contract.
    """
    queues = {"retry": [], "new": []}
    retry_ids = {int(post_id) for post_id in retry_post_ids}
    for item in candidates:
        state = state_store.load(item[0])
        reopened = _manual_reopen_confirmed(item, state)
        if not reopened and (
            state.state is not LifecycleState.PENDING
            or not _cooldown_expired(state.retry.next_at, now)
        ):
            continue
        queue = (
            "new"
            if (
                int(item[0]) not in retry_ids
                and not reopened
                and state.state is LifecycleState.PENDING
                and state.phase is Phase.RELEVANCE
                and state.blocker is None
            )
            else "retry"
        )
        queues[queue].append((item, state, reopened))
    for queue in queues.values():
        queue.sort(key=lambda pair: rank(pair[1], manually_reopened=pair[2]), reverse=True)
    return queues


def select(
    candidates,
    state_store,
    *,
    limit: int = 5,
    now: datetime | None = None,
    retry_post_ids: Collection[int] = (),
    excluded_post_ids: Collection[int] = (),
    max_new_posts: int | None = None,
):
    """Select retry work first while reserving capacity for NEW work."""
    if limit < 1:
        raise ValueError("limit must be positive")
    now = now or datetime.now(timezone.utc)
    excluded_ids = {int(post_id) for post_id in excluded_post_ids}
    candidates = [item for item in candidates if int(item[0]) not in excluded_ids]
    if max_new_posts is not None and max_new_posts < 0:
        raise ValueError("max_new_posts must not be negative")
    queues = _eligible_queues(
        candidates,
        state_store,
        now,
        retry_post_ids=retry_post_ids,
    )
    retry_items = [item for item, _state, _reopened in queues["retry"]]
    new_items = [item for item, _state, _reopened in queues["new"]]
    cycle_retry_ids = {int(post_id) for post_id in retry_post_ids}
    cycle_retry_items = [item for item in retry_items if int(item[0]) in cycle_retry_ids]
    external_retry_items = [item for item in retry_items if int(item[0]) not in cycle_retry_ids]
    if max_new_posts is not None:
        # Every post outside this cycle's admitted set consumes a new-post
        # admission, even when its editorial state is already in MEDIA or it
        # was manually reopened.  Otherwise DRAINING could admit an external
        # retry while max_new_posts=0 and authorize a post unseen by the cycle.
        admission_items = external_retry_items + new_items
        if max_new_posts == 0:
            return cycle_retry_items[:limit]
        retry_slots = min(
            len(cycle_retry_items),
            limit if not admission_items else max(0, limit - 1),
        )
        admission_slots = min(limit - retry_slots, max_new_posts)
        return cycle_retry_items[:retry_slots] + admission_items[:admission_slots]
    if limit == 1 and new_items and (max_new_posts is None or max_new_posts > 0):
        # Reserve one deterministic two-hour slot out of every three for an
        # aged NEW post. The other two slots remain retry-first, bounding
        # starvation in both directions.
        aged_new = [item for item in new_items if _candidate_age(item, now) >= 1]
        if aged_new and (now.hour // 2) % 3 == 0:
            return [min(aged_new, key=lambda item: _candidate_date(item))]
    if not retry_items:
        return new_items[:limit if max_new_posts is None else min(limit, max_new_posts)]
    if not new_items:
        return retry_items[:limit]

    retry_slots = min(
        len(retry_items),
        limit if max_new_posts == 0 else max(0, limit - 1),
    )
    new_slots = limit - retry_slots
    if max_new_posts is not None:
        new_slots = min(new_slots, max_new_posts)
    return retry_items[:retry_slots] + new_items[:new_slots]


def _candidate_date(item) -> datetime:
    value = item[1].get("date") if isinstance(item, tuple) and len(item) > 1 else None
    if isinstance(value, dict):
        value = value.get("raw") or value.get("rendered")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return datetime.max.replace(tzinfo=timezone.utc)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _candidate_age(item, now: datetime) -> int:
    date = _candidate_date(item)
    return max(0, (now - date).days)


def next_action(state: WorkState) -> str:
    if state.state in {LifecycleState.READY, LifecycleState.PUBLISHED, LifecycleState.SKIPPED, LifecycleState.HUMAN_REQUIRED}:
        return "none"
    if state.phase is Phase.RELEVANCE:
        return "evaluate_relevance"
    if state.phase is Phase.EDITORIAL:
        return "regenerate_editorial"
    if state.blocker in {BlockerCode.INLINE_MISSING, BlockerCode.MEDIA_INVALID, BlockerCode.MEDIA_DUPLICATE, BlockerCode.MEDIA_ORIGIN}:
        return "resolve_inline"
    if state.blocker in {BlockerCode.FEATURED_MISSING, BlockerCode.FEATURED_INVALID, BlockerCode.FEATURED_VISION}:
        return "resolve_featured"
    if state.media.effective_missing > 0:
        return "resolve_inline"
    if state.media.featured.status is not FeaturedStatus.VALID:
        return "resolve_featured"
    return "validate"
