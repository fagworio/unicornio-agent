"""Operational boundary for V2 shadow and explicit write commands.

Shadow is GET/filesystem-only. The write command is deliberately separate and
reuses the existing fully gated apply path; it never publishes.
"""

from dataclasses import replace
from pathlib import Path
from typing import Any

from .legacy import LegacyStateLoader
from .model import InlineMedia, LifecycleState, MediaProgress
from .production_reader import ProductionShadowReader
from .snapshot import capture_snapshot, compare_snapshot
from .state_store import StateStore
from ..workflow import apply_editorial


class WriteCountingClient:
    """Delegating client that counts only mutating WordPress operations."""

    _MUTATIONS = frozenset({"update_post", "upload_media", "publish", "move_to_status"})

    def __init__(self, client):
        self._client = client
        self.write_calls = 0

    def __getattr__(self, name):
        target = getattr(self._client, name)
        if name not in self._MUTATIONS:
            return target

        def counted(*args, **kwargs):
            self.write_calls += 1
            return target(*args, **kwargs)

        return counted


class WordPressStateBackend:
    """V2 state backend using only the post meta boundary."""

    def __init__(self, client):
        self.client = client

    def get(self, post_id: int) -> dict[str, Any] | None:
        post = self.client.get_post(post_id)
        return post.get("meta", {}) if isinstance(post, dict) else {}

    def put(self, post_id: int, value: dict[str, Any]) -> None:
        self.client.update_post(post_id, {"meta": value})


def run_shadow(client, post_ids: list[int], root: Path, output_dir: Path) -> dict[str, Any]:
    reader = ProductionShadowReader(client, root / "backups")
    captured = []
    reports = []
    for post_id in post_ids:
        path = capture_snapshot(post_id, reader.read_post, reader.read_manifest, output_dir)
        captured.append(str(path))
        reports.append(compare_snapshot(path))
    return {
        "mode": "shadow",
        "snapshots": len(reports),
        "equivalent": sum(bool(item.get("equivalent")) for item in reports),
        "wordpress_writes": 0,
        "production_writes": 0,
        "paths": captured,
        "reports": reports,
    }


def _merge_ready_media(initial_state, result: dict[str, Any], final_state):
    """Carry V2 media identity across apply's READY manifest cleanup."""
    if result.get("status") != "ready":
        return final_state
    existing = {item.media_id for item in initial_state.media.inline}
    added = []
    for item in result.get("media_plan_results", []) or []:
        if not isinstance(item, dict) or item.get("featured") or int(item.get("media_id") or 0) in existing:
            continue
        added.append(InlineMedia.from_dict({
            "media_id": int(item["media_id"]),
            "media_url": str(item.get("media_url", "")),
            "slot": int(item.get("paragraph_index", len(initial_state.media.inline) * 3)),
            "alt_text": str(item.get("alt_text", "")),
            "credit_text": str(item.get("credit_text", "")),
        }))
    media = MediaProgress(initial_state.media.required, tuple(initial_state.media.inline) + tuple(added), initial_state.media.featured)
    return replace(final_state, media=media)


def run_write_one(client, config, root: Path, post_id: int, editorial: dict[str, Any], *, allow_write: bool) -> dict[str, Any]:
    if not allow_write:
        raise ValueError("write mode requires --write explicitly")
    if config.dry_run:
        raise ValueError("write mode requires EDITOR_DRY_RUN=false")
    counted_client = WriteCountingClient(client)
    reader = ProductionShadowReader(counted_client, root / "backups")
    legacy_loader = LegacyStateLoader(reader.read_manifest)
    initial_state = StateStore(WordPressStateBackend(counted_client), legacy_loader=legacy_loader).load(post_id)
    if initial_state.state is not LifecycleState.PENDING:
        return {"mode": "write", "post_id": post_id, "executed": False, "reason": "terminal_state", "wordpress_writes": 0, "production_writes": 0}
    result = apply_editorial(counted_client, config, root, post_id, editorial)
    final_post = counted_client.get_post(post_id)
    final_meta = final_post.get("meta", {}) if isinstance(final_post, dict) else {}
    final_state = _merge_ready_media(initial_state, result, legacy_loader.load(post_id, final_meta))
    StateStore(WordPressStateBackend(counted_client)).commit(post_id, final_state)
    verified_post = counted_client.get_post(post_id)
    verified_state = StateStore(WordPressStateBackend(counted_client)).load(post_id)
    if verified_state.to_dict() != final_state.to_dict():
        raise RuntimeError("V2 state read-back mismatch after WordPress commit")
    return {
        "mode": "write",
        "post_id": post_id,
        "executed": True,
        "result": result,
        "initial_state": initial_state.to_dict(),
        "proposed_state": final_state.to_dict(),
        "wordpress_writes": counted_client.write_calls,
        "production_writes": counted_client.write_calls,
    }
