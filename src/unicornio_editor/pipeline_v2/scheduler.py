"""Pure distance-to-ready ranking for V2 work states."""

from datetime import datetime, timezone

from .model import BlockerCode, FeaturedStatus, LifecycleState, Phase, WorkState


def rank(state: WorkState) -> int:
    """Return a higher score for work closer to READY."""
    if state.state.value in {"ready", "published", "skipped", "human_required"}:
        return -1000
    if state.blocker in {BlockerCode.FEATURED_MISSING, BlockerCode.FEATURED_INVALID, BlockerCode.FEATURED_VISION}:
        return 1000 if state.media.missing == 0 else 500 - state.media.missing
    if state.blocker in {BlockerCode.INLINE_MISSING, BlockerCode.MEDIA_INVALID, BlockerCode.MEDIA_DUPLICATE, BlockerCode.MEDIA_ORIGIN}:
        return 400 - state.media.missing
    if state.phase is Phase.EDITORIAL:
        return 250
    if state.phase is Phase.RELEVANCE:
        return 100
    return 50


def _cooldown_expired(value: str | None, now: datetime) -> bool:
    if not value:
        return True
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed <= now


def select(candidates, state_store, *, limit: int = 5, now: datetime | None = None):
    """Filter eligible pending work, rank it, and reserve one NEW slot when possible."""
    if limit < 1:
        raise ValueError("limit must be positive")
    now = now or datetime.now(timezone.utc)
    eligible = []
    for item in candidates:
        state = state_store.load(item[0])
        if state.state is not LifecycleState.PENDING or not _cooldown_expired(state.retry.next_at, now):
            continue
        eligible.append((item, state))
    eligible.sort(key=lambda pair: rank(pair[1]), reverse=True)
    new_states = [(item, state) for item, state in eligible if state.phase is Phase.RELEVANCE and state.blocker is None]
    if limit == 1 and new_states:
        # A NEW post older than one day must get a turn even when a near-READY
        # retry exists; otherwise limit=1 can starve the intake indefinitely.
        aged_new = [item for item, _ in new_states if _candidate_age(item, now) >= 1]
        if aged_new:
            return [min(aged_new, key=lambda item: _candidate_date(item))]
    if limit == 1 or not new_states:
        return [item for item, _ in eligible[:limit]]
    new_item = new_states[0][0]
    selected = [item for item, _ in eligible if item != new_item][: max(0, limit - 1)]
    return selected + [new_item]


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
    if state.media.missing > 0:
        return "resolve_inline"
    if state.media.featured.status is not FeaturedStatus.VALID:
        return "resolve_featured"
    return "validate"
