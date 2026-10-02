"""Offline PipelineRunner replay over immutable snapshots."""

import json
from pathlib import Path
from typing import Any

from .legacy import LegacyStateLoader
from .recording_store import RecordingStateStore
from .runner import PipelineRunner


def replay_snapshot(path: str | Path, stages: dict[str, Any]) -> dict[str, Any]:
    snapshot = json.loads(Path(path).read_text(encoding="utf-8"))
    post_id = int(snapshot["post_id"])
    wp = snapshot.get("wp", {})
    manifest = snapshot.get("manifest", {})
    state = LegacyStateLoader(lambda _: manifest).load(post_id, wp.get("meta", {}))
    store = RecordingStateStore({post_id: state})
    result = PipelineRunner(store, stages).run_one(post_id, wp.get("context", {}))
    return {"post_id": post_id, "outcome": result.type.value, "blocker": result.blocker.value if result.blocker else None, "recording_writes": store.writes, "context": wp.get("context", {})}
