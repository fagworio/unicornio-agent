"""Production readers for the V2 scheduler.

This module is read-only. It owns candidate pagination and context assembly;
state transitions and writes remain in the runner/writer layers.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..checklist import required_image_count
from ..content_quality import word_count
from .legacy import LegacyStateLoader
from .model import FeaturedStatus, LifecycleState, MediaProgress, WorkState
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
        candidate = self._read_json(directory / "editorial.candidate.json")
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
            "candidate": candidate or None,
            "manifest": manifest,
            "original_link": (post.get("meta") or {}).get("original_link"),
            # The scheduler uses the persisted publication date to reserve
            # occasional turns for old NEW posts.
            "date": post.get("date_gmt") or post.get("date"),
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

    def _recovered_state(
        self,
        post_id: int,
        state: WorkState,
        context: dict[str, Any] | None = None,
    ) -> WorkState:
        """Recover only from an interrupted, verifiable V2 transaction."""
        context = context or {}
        directory = self.root / "backups" / str(post_id)
        journal_path = self.root / "work" / "v2-journal" / f"{post_id}.json"
        journal = self.context_loader._read_json(journal_path)
        journal_status = str(journal.get("status") or "")
        active_journal = bool(journal) and journal_status != "committed"
        title = str(context.get("title") or "")
        editorial = context.get("editorial") or context.get("draft") or {}
        html = str(editorial.get("cleaned_html") or context.get("content") or "")
        current_required = required_image_count(
            word_count(html),
            title=title,
            content=html,
        )
        current_media = state.media
        if current_media.required != current_required:
            current_media = MediaProgress(
                required=current_required,
                inline=current_media.inline,
                featured=current_media.featured,
                search=current_media.search,
            )
        if not active_journal:
            return WorkState(
                state=state.state,
                phase=state.phase,
                blocker=state.blocker,
                retry=state.retry,
                relevance_approved=state.relevance_approved,
                media=current_media,
            )

        media_candidates: list[MediaProgress] = []
        partial = self.context_loader._read_json(directory / "editorial.partial.json")
        try:
            if partial:
                media_candidates.append(MediaProgress.from_dict(partial))
        except (TypeError, ValueError, KeyError):
            pass
        try:
            journal_state = (
                WorkState.from_dict(journal.get("state"))
                if isinstance(journal.get("state"), dict) else None
            )
            if journal_state is not None:
                media_candidates.append(journal_state.media)
        except (TypeError, ValueError, KeyError):
            pass
        if not media_candidates:
            return WorkState(
                state=state.state,
                phase=state.phase,
                blocker=state.blocker,
                retry=state.retry,
                relevance_approved=state.relevance_approved,
                media=current_media,
            )

        rejected_ids: set[int] = set()
        rejected_urls: set[str] = set()
        validation = self.context_loader._read_json(directory / "editorial.validation.json")
        for failure in validation.get("failures") or []:
            if not isinstance(failure, dict):
                continue
            for invalid in failure.get("invalid_media") or []:
                if not isinstance(invalid, dict):
                    continue
                try:
                    if invalid.get("media_id") is not None:
                        rejected_ids.add(int(invalid["media_id"]))
                except (TypeError, ValueError):
                    pass
                value = str(invalid.get("url") or invalid.get("media_url") or "").strip()
                if value:
                    rejected_urls.add(value)

        def verified(media_id: int | None, media_url: str | None) -> bool:
            if not media_id:
                return False
            try:
                attachment = self.client.get_media(int(media_id))
            except Exception:  # noqa: BLE001 - recovery is fail-closed
                return False
            stored = str(media_url or "").strip()
            actual = str((attachment or {}).get("source_url") or "").strip()
            return bool(actual) and (not stored or stored == actual)

        def valid_count(media: MediaProgress) -> int:
            return sum(
                1 for item in media.inline
                if item.media_id not in rejected_ids
                and item.media_url not in rejected_urls
                and verified(item.media_id, item.media_url)
            )

        best = current_media
        best_score = (
            valid_count(best),
            int(
                best.featured.status is FeaturedStatus.VALID
                and verified(best.featured.media_id, best.featured.media_url)
            ),
        )
        for candidate in media_candidates:
            inline = tuple(
                item for item in candidate.inline
                if item.media_id not in rejected_ids
                and item.media_url not in rejected_urls
                if verified(item.media_id, item.media_url)
            )
            featured = (
                candidate.featured
                if candidate.featured.status is FeaturedStatus.VALID
                and verified(candidate.featured.media_id, candidate.featured.media_url)
                else state.media.featured
            )
            try:
                recovered = MediaProgress(
                    required=current_required,
                    inline=inline,
                    featured=featured,
                    search=candidate.search,
                )
            except ValueError:
                continue
            score = (
                len(inline),
                int(featured.status is FeaturedStatus.VALID),
            )
            if score > best_score:
                best, best_score = recovered, score
        if best == current_media and current_media == state.media:
            return state
        return WorkState(
            state=state.state,
            phase=state.phase,
            blocker=state.blocker,
            retry=state.retry,
            relevance_approved=state.relevance_approved,
            media=best,
        )

    def snapshot(self, *, page_size: int = 100, max_pages: int = 100) -> list[tuple[int, dict[str, Any]]]:
        """Return all pending posts, including cooldown/terminal V2 states."""
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
                context = self.context_loader.load(post)
                state = self._recovered_state(
                    post_id,
                    self.state_store.load(post_id),
                    context,
                )
                context["v2_state"] = state
                candidates.append((post_id, context))
            if len(posts) < page_size:
                break
        return candidates

    def read(self, *, page_size: int = 100, max_pages: int = 100) -> list[tuple[int, dict[str, Any]]]:
        now = self.now or datetime.now(timezone.utc)
        return [
            (post_id, context)
            for post_id, context in self.snapshot(page_size=page_size, max_pages=max_pages)
            if context["v2_state"].state is LifecycleState.PENDING
            and _cooldown_expired(context["v2_state"].retry.next_at, now)
        ]
