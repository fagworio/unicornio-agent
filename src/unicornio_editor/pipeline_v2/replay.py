"""Offline PipelineRunner replay over immutable snapshots."""

import json
from pathlib import Path
from typing import Any

from .legacy import LegacyStateLoader
from .recording_store import RecordingStateStore
from .runner import PipelineRunner
from .scheduler import next_action


def _summary(state) -> dict[str, Any]:
    return {"state": state.state.value, "phase": state.phase.value, "blocker": state.blocker.value if state.blocker else None, "required": state.media.required, "accepted": state.media.accepted, "missing": state.media.missing, "next_action": next_action(state)}


def replay_snapshot(path: str | Path, stages: dict[str, Any]) -> dict[str, Any]:
    snapshot = json.loads(Path(path).read_text(encoding="utf-8"))
    post_id = int(snapshot["post_id"])
    wp = snapshot.get("wp", {})
    manifest = snapshot.get("manifest", {})
    state = LegacyStateLoader(lambda _: manifest).load(post_id, wp.get("meta", {}))
    initial = _summary(state)
    base = {"post_id": post_id, "executed": False, "initial": initial, "context": wp.get("context", {}), "recording_writes": 0, "wordpress_writes": 0, "production_writes": 0}
    if state.state.value != "pending":
        base["reason"] = "terminal_state"
        return base
    store = RecordingStateStore({post_id: state})
    result = PipelineRunner(store, stages).run_one(post_id, wp.get("context", {}))
    proposed = _summary(store.states[post_id])
    return {**base, "executed": True, "outcome": {"type": result.type.value, "blocker": result.blocker.value if result.blocker else None}, "proposed": proposed, "recording_writes": store.writes}
