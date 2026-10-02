"""Read-only production snapshot capture and offline V2 conversion."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .legacy import LegacyStateLoader
from .scheduler import next_action
from .shadow import compare_work_state


def capture_snapshot(post_id: int, post_reader: Callable[[int], dict[str, Any]], manifest_reader: Callable[[int], dict[str, Any]], output_dir: str | Path) -> Path:
    """Perform only injected reads and write a local immutable snapshot."""
    post = post_reader(post_id)
    manifest = manifest_reader(post_id)
    payload = {"post_id": post_id, "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "wp": {"status": post.get("status"), "meta": post.get("meta", {})}, "manifest": manifest}
    target = Path(output_dir) / f"{post_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def compare_snapshot(path: str | Path) -> dict[str, Any]:
    """Convert one snapshot offline; does not call WordPress or persist state."""
    snapshot = json.loads(Path(path).read_text(encoding="utf-8"))
    post_id = int(snapshot["post_id"])
    wp = snapshot.get("wp", {})
    manifest = snapshot.get("manifest", {})
    loader = LegacyStateLoader(lambda _: manifest)
    state = loader.load(post_id, wp.get("meta", {}))
    action = next_action(state)
    assets = manifest.get("accepted_media", []) or []
    v1_state = (wp.get("meta", {}) or {}).get("_hermes_state") or "new"
    expected = {"required": state.media.required, "accepted": state.media.accepted, "missing": state.media.missing, "blocker": state.blocker.value if state.blocker else None, "phase": state.phase.value, "next_action": action}
    report = compare_work_state(post_id, v1_state, state, expected=expected, expected_ids=[int(item["media_id"]) for item in assets], expected_slots=[int(item.get("slot", item.get("paragraph_index", i + 1))) for i, item in enumerate(assets)], expected_action=action, actual_action=action, expected_featured=state.media.featured.status.value)
    report["snapshot"] = str(path)
    report["writes"] = 0
    return report
