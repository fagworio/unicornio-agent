"""V2 session coordinator: select, process, report."""

from collections import Counter
from typing import Any, Iterable

from .lock import RunSessionLock
from .scheduler import select


def run_session(candidates: Iterable[tuple[int, dict[str, Any]]], state_store: Any, runner: Any, *, limit: int = 5, lock_path=None) -> dict[str, Any]:
    if limit < 1:
        raise ValueError("limit must be positive")
    lock = RunSessionLock(lock_path) if lock_path else None
    if lock is not None and not lock.acquire():
        return {"selected": 0, "processed": 0, "errors": 0, "locked": True, "outcomes": {}, "details": []}
    try:
        selected = select(candidates, state_store, limit=limit)
        outcomes = Counter()
        details = []
        errors = 0
        for post_id, context in selected:
            try:
                outcome = runner.run_one(post_id, context)
            except Exception as exc:  # isolate one post; next posts remain processable
                errors += 1
                details.append({"post_id": post_id, "outcome": "error", "error": str(exc)})
                continue
            kind = outcome.type.value
            outcomes[kind] += 1
            blocker = outcome.blocker
            details.append({"post_id": post_id, "outcome": kind, "blocker": blocker.value if blocker else None})
        return {"selected": len(selected), "processed": len(details), "errors": errors, "locked": False, "outcomes": dict(outcomes), "details": details}
    finally:
        if lock is not None:
            lock.release()
