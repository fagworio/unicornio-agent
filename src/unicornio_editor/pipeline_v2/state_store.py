"""Single V2 operational-state boundary.

The backend is deliberately injected so this module remains testable without
WordPress. Only this boundary knows the V2 persistence key.
"""

from typing import Any, Protocol

from .legacy import from_legacy_state
from .model import LifecycleState, WorkState


class StateBackend(Protocol):
    def get(self, post_id: int) -> dict[str, Any] | None: ...
    def put(self, post_id: int, value: dict[str, Any]) -> None: ...


class StateStore:
    KEY = "_hermes_work_state"

    def __init__(self, backend: StateBackend):
        self._backend = backend

    def load(self, post_id: int) -> WorkState:
        raw = self._backend.get(post_id) or {}
        value = raw.get(self.KEY) if isinstance(raw, dict) else None
        if not isinstance(value, dict):
            if isinstance(raw, dict) and raw.get("_hermes_state"):
                return from_legacy_state({
                    "state": raw.get("_hermes_state"),
                    "attempts": raw.get("_hermes_attempts"),
                    "next_retry_at": raw.get("_hermes_next_retry_at"),
                    "last_error": raw.get("_hermes_last_error"),
                    "partial_kind": raw.get("_hermes_partial_kind"),
                    "partial_required": raw.get("_hermes_media_required"),
                    "partial_completed": raw.get("_hermes_media_completed"),
                    "partial_missing": raw.get("_hermes_media_missing"),
                    "no_progress_attempts": raw.get("_hermes_no_progress_attempts"),
                })
            return WorkState()
        return WorkState.from_dict(value)

    def commit(self, post_id: int, state: WorkState) -> None:
        self._backend.put(post_id, {self.KEY: state.to_dict()})

    def mark_ready(self, post_id: int, state: WorkState) -> None:
        if state.state is not LifecycleState.READY:
            raise ValueError("mark_ready requires READY state")
        self.commit(post_id, state)

    def mark_published(self, post_id: int, state: WorkState) -> None:
        if state.state is not LifecycleState.PUBLISHED:
            raise ValueError("mark_published requires PUBLISHED state")
        self.commit(post_id, state)
