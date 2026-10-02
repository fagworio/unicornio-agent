from typing import Any

from .model import WorkState


class RecordingStateStore:
    """In-memory StateStore double for offline replay; never writes WordPress."""

    def __init__(self, states: dict[int, WorkState] | None = None):
        self.states = dict(states or {})
        self.loads: list[int] = []
        self.commits: list[tuple[int, WorkState]] = []

    def load(self, post_id: int) -> WorkState:
        self.loads.append(post_id)
        return self.states.get(post_id, WorkState())

    def commit(self, post_id: int, state: WorkState) -> None:
        self.commits.append((post_id, state))
        self.states[post_id] = state

    @property
    def writes(self) -> int:
        return len(self.commits)
