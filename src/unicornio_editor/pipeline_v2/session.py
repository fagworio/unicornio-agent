"""V2 session coordinator: select, process, report."""

from collections import Counter
from typing import Any, Iterable

from .scheduler import rank


def run_session(candidates: Iterable[tuple[int, dict[str, Any]]], state_store: Any, runner: Any, *, limit: int = 5) -> dict[str, Any]:
    if limit < 1:
        raise ValueError("limit must be positive")
    ranked = sorted(candidates, key=lambda item: rank(state_store.load(item[0])), reverse=True)[:limit]
    outcomes = Counter()
    details = []
    for post_id, context in ranked:
        outcome = runner.run_one(post_id, context)
        kind = outcome.type.value
        outcomes[kind] += 1
        blocker = getattr(outcome, "blocker", None)
        details.append({"post_id": post_id, "outcome": kind, "blocker": blocker.value if blocker else None})
    return {"selected": len(ranked), "processed": len(details), "outcomes": dict(outcomes), "details": details}
