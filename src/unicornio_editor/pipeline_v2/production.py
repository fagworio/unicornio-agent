"""Production readers for the V2 scheduler.

This module is read-only. It owns candidate pagination and context assembly;
state transitions and writes remain in the runner/writer layers.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .legacy import LegacyStateLoader
from .model import LifecycleState
from .scheduler import _cooldown_expired
from .state_store import StateStore


class _WordPressStateBackend:
    def __init__(self, client):
        self.client = client

    def get(self, post_id: int) -> dict[str, Any] | None:
        post = self.client.get_post(post_id)
        return post.get("meta", {}) if isinstance(post, dict) else {}

    def put(self, post_id: int, value: dict[str, Any]) -> None:
        raise RuntimeError("ProductionCandidateReader is read-only")


class ProductionContextLoader:
    def __init__(self, client, root: Path):
        self.client = client
        self.root = Path(root)

    def _read_json(self, path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def load(self, post: dict[str, Any]) -> dict[str, Any]:
        post_id = int(post["id"])
        directory = self.root / "backups" / str(post_id)
        manifest = self._read_json(directory / "editorial.partial.json")
        draft = self._read_json(directory / "editorial.draft.json")
        if not draft:
            draft = self._read_json(directory / "editorial.latest.json")
        return {
            "post": post,
            "post_id": post_id,
            "title": (post.get("title") or {}).get("raw") or (post.get("title") or {}).get("rendered") or "",
            "content": (post.get("content") or {}).get("raw") or "",
            "featured_media": post.get("featured_media"),
            "meta": post.get("meta") or {},
            "draft": draft,
            "editorial": draft or None,
            "manifest": manifest,
            "original_link": (post.get("meta") or {}).get("original_link"),
        }


class ProductionCandidateReader:
    """Paginate pending posts and expose only V2-eligible candidates."""

    def __init__(self, client, root: Path, *, now=None):
        self.client = client
        self.root = Path(root)
        self.context_loader = ProductionContextLoader(client, self.root)
        self.state_store = StateStore(
            _WordPressStateBackend(client),
            legacy_loader=LegacyStateLoader(
                lambda post_id: self.context_loader._read_json(
                    self.root / "backups" / str(post_id) / "editorial.partial.json"
                )
            ),
        )
        self.now = now

    def read(self, *, page_size: int = 100, max_pages: int = 100) -> list[tuple[int, dict[str, Any]]]:
        candidates: list[tuple[int, dict[str, Any]]] = []
        seen: set[int] = set()
        for page in range(1, max_pages + 1):
            posts = self.client.list_pending(page=page, per_page=page_size, status="pending")
            if not posts:
                break
            for post in posts:
                post_id = post.get("id")
                if not isinstance(post_id, int) or post_id in seen:
                    continue
                seen.add(post_id)
                state = self.state_store.load(post_id)
                if state.state is not LifecycleState.PENDING:
                    continue
                if not _cooldown_expired(state.retry.next_at, self.now or datetime.now(timezone.utc)):
                    continue
                context = self.context_loader.load(post)
                context["v2_state"] = state
                candidates.append((post_id, context))
            if len(posts) < page_size:
                break
        return candidates
