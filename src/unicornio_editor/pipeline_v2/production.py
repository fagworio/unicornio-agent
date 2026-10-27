"""Production readers for the V2 scheduler.

This module is read-only. It owns candidate pagination and context assembly;
state transitions and writes remain in the runner/writer layers.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Keep the legacy symbol available to integrations that patch this module;
# production execution uses the canonical HTML-aware helper below.
from ..checklist import required_image_count, required_image_count_for_content
from ..batch import BatchError, wordpress_text
from ..builder import strip_canonical_footer
from .legacy import LegacyStateLoader
from .media_accounting import account_visual_media
from .media_layout import validate_normal_article_layout_policy
from .model import FeaturedStatus, LifecycleState, MediaProgress, WorkState
from .scheduler import _cooldown_expired
from .state_store import StateStore
from ..content_quality import word_count


_MIN_RECOVERABLE_EDITORIAL_WORDS = 20


def editorial_has_substantive_text(value: Any) -> bool:
    """Return true only for editorial HTML with visible substantive text."""
    if isinstance(value, dict):
        value = value.get("cleaned_html")
    html = str(value or "")
    return word_count(html) >= _MIN_RECOVERABLE_EDITORIAL_WORDS


def _payload_editorial(payload: dict[str, Any]) -> dict[str, Any] | None:
    editorial = payload.get("editorial") if isinstance(payload.get("editorial"), dict) else payload
    return editorial if editorial_has_substantive_text(editorial) else None


def _editorial_identity_hash(html: str) -> str:
    normalized = re.sub(r"<!--\s*wc:\s*\d+\s*-->", "", str(html or ""), flags=re.IGNORECASE)
    normalized = re.sub(r"\s+", " ", strip_canonical_footer(normalized)).strip()
    return hashlib.sha256(normalized.encode()).hexdigest()


def _recover_draft_metadata(draft: dict[str, Any], directory: Path, post_id: int) -> dict[str, Any]:
    """Restore verified editorial metadata without replacing the draft body."""
    merged = dict(draft)
    draft_body_hash = _editorial_identity_hash(str(draft.get("cleaned_html") or ""))
    prepared_path = directory / "prepared.json"
    prepared_body_hash = ""
    try:
        prepared = json.loads(prepared_path.read_text(encoding="utf-8")) if prepared_path.is_file() else {}
        prepared_html = str(prepared.get("cleaned_html") or "") if isinstance(prepared, dict) else ""
        if editorial_has_substantive_text(prepared_html):
            prepared_body_hash = _editorial_identity_hash(prepared_html)
    except (OSError, ValueError):
        prepared_body_hash = ""
    body_compatible = bool(draft_body_hash and prepared_body_hash and draft_body_hash == prepared_body_hash)
    if not body_compatible:
        return merged
    sources = [
        directory.parent.parent / "work" / "v2-editorial" / f"{int(post_id)}.json",
        directory / "editorial.candidate.json",
    ]
    draft_html = str(draft.get("cleaned_html") or "")
    for source in sources:
        try:
            payload = json.loads(source.read_text(encoding="utf-8")) if source.is_file() else {}
        except (OSError, ValueError):
            continue
        records = payload.get("results") if isinstance(payload, dict) else None
        if isinstance(records, list):
            record = next((item for item in records if isinstance(item, dict) and int(item.get("post_id") or 0) == int(post_id)), {})
            payload = record.get("editorial") if isinstance(record, dict) and isinstance(record.get("editorial"), dict) else {}
        editorial = payload.get("editorial") if isinstance(payload, dict) and isinstance(payload.get("editorial"), dict) else payload
        if not isinstance(editorial, dict):
            continue
        for key in ("title", "site_relevance", "media_plan", "needs_trailer", "trailer_url", "game_name", "seo", "original_link"):
            value = editorial.get(key)
            if value is None and isinstance(payload, dict):
                value = payload.get(key)
            if value is not None and (key not in merged or merged.get(key) is None):
                merged[key] = value
    return merged



def recover_editorial(post: dict[str, Any], directory: Path) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Choose a verifiable editorial source without reviving stale snapshots."""
    candidates: list[dict[str, Any]] = []
    content_present = "content" in post
    draft_path = directory / "editorial.draft.json"
    try:
        draft_payload = json.loads(draft_path.read_text(encoding="utf-8")) if draft_path.is_file() else {}
    except (OSError, ValueError):
        draft_payload = {}
    draft_editorial = _payload_editorial(draft_payload) if isinstance(draft_payload, dict) else None
    draft_fresh = True
    modified_gmt = str(post.get("modified_gmt") or "").strip()
    if draft_editorial and modified_gmt:
        try:
            modified_at = datetime.fromisoformat(modified_gmt.replace("Z", "+00:00"))
            if modified_at.tzinfo is None:
                modified_at = modified_at.replace(tzinfo=timezone.utc)
            draft_fresh = draft_path.stat().st_mtime >= modified_at.timestamp()
        except (OSError, ValueError):
            draft_fresh = False
    if draft_editorial and draft_fresh:
        draft_editorial = _recover_draft_metadata(draft_editorial, directory, int(post.get("id") or 0))
        draft_html = str(draft_editorial.get("cleaned_html") or "")
        return dict(draft_editorial), {
            "source": "editorial.draft.json",
            "sha256": hashlib.sha256(draft_html.encode()).hexdigest(),
            "word_count": word_count(draft_html),
            "snapshot_fresh": True,
            "candidates": ["editorial.draft.json"],
        }
    candidates.append({"source": "editorial.draft.json", "valid": bool(draft_editorial), "fresh": draft_fresh})
    wordpress_html = strip_canonical_footer(wordpress_text(post.get("content"), field="content"))
    prepared_for_wp: dict[str, Any] = {}
    prepared_for_wp_path = directory / "prepared.json"
    if prepared_for_wp_path.is_file():
        try:
            loaded_prepared = json.loads(prepared_for_wp_path.read_text(encoding="utf-8"))
            if isinstance(loaded_prepared, dict):
                prepared_for_wp = loaded_prepared
        except (OSError, ValueError):
            prepared_for_wp = {}
    if editorial_has_substantive_text(wordpress_html):
        digest = hashlib.sha256(wordpress_html.encode()).hexdigest()
        prepared_body = str(prepared_for_wp.get("cleaned_html") or "")
        base = dict(prepared_for_wp) if hashlib.sha256(prepared_body.encode()).hexdigest() == digest else {}
        candidate_path = directory / "editorial.candidate.json"
        try:
            candidate_payload = json.loads(candidate_path.read_text(encoding="utf-8")) if candidate_path.is_file() else {}
        except (OSError, ValueError):
            candidate_payload = {}
        candidate_editorial = _payload_editorial(candidate_payload) if isinstance(candidate_payload, dict) else None
        candidate_body = str(candidate_editorial.get("cleaned_html") or "") if candidate_editorial else ""
        if candidate_editorial and hashlib.sha256(candidate_body.encode()).hexdigest() == digest:
            base.update(candidate_editorial)
        base["cleaned_html"] = wordpress_html
        return base, {
            "source": "wordpress_get",
            "sha256": digest,
            "word_count": word_count(wordpress_html),
            "candidates": ["wordpress_get"],
        }
    candidates.append({"source": "wordpress_get", "valid": False, "word_count": word_count(wordpress_html)})

    prepared = {}
    prepared_path = directory / "prepared.json"
    if prepared_path.is_file():
        try:
            prepared = json.loads(prepared_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prepared = {}
    prepared_html = str(prepared.get("cleaned_html") or "") if isinstance(prepared, dict) else ""
    backup_path = directory / str(prepared.get("backup") or "")
    backup = {}
    if backup_path.is_file():
        try:
            backup = json.loads(backup_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            backup = {}
    snapshot_fresh = bool(
        isinstance(backup, dict)
        and post.get("modified_gmt")
        and backup.get("modified_gmt") == post.get("modified_gmt")
    )
    if editorial_has_substantive_text(prepared_html) and (not content_present or snapshot_fresh):
        digest = hashlib.sha256(prepared_html.encode()).hexdigest()
        candidates.append({"source": "prepared.json", "valid": True, "sha256": digest, "fresh": snapshot_fresh})
        return {"cleaned_html": prepared_html, **{k: v for k, v in prepared.items() if k != "cleaned_html"}}, {
            "source": "prepared.json",
            "sha256": digest,
            "word_count": word_count(prepared_html),
            "snapshot_fresh": snapshot_fresh,
            "candidates": candidates,
        }
    candidates.append({"source": "prepared.json", "valid": False, "fresh": snapshot_fresh, "word_count": word_count(prepared_html)})

    latest = {}
    latest_path = directory / "editorial.latest.json"
    if latest_path.is_file():
        try:
            latest = json.loads(latest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            latest = {}
    latest_editorial = _payload_editorial(latest) if isinstance(latest, dict) else None
    latest_html = str(latest_editorial.get("cleaned_html") or "") if latest_editorial else ""
    if latest_editorial and (not content_present or snapshot_fresh):
        latest_digest = hashlib.sha256(latest_html.encode()).hexdigest()
        return dict(latest_editorial), {
            "source": "editorial.latest.json",
            "sha256": latest_digest,
            "word_count": word_count(latest_html),
            "snapshot_fresh": snapshot_fresh,
            "candidates": candidates + [{"source": "editorial.latest.json", "valid": True}],
        }
    candidates.append({"source": "editorial.latest.json", "valid": bool(latest_editorial), "fresh": snapshot_fresh})

    candidate_path = directory / "editorial.candidate.json"
    try:
        candidate_payload = json.loads(candidate_path.read_text(encoding="utf-8")) if candidate_path.is_file() else {}
    except (OSError, ValueError):
        candidate_payload = {}
    candidate_editorial = _payload_editorial(candidate_payload) if isinstance(candidate_payload, dict) else None
    prepared_digest = hashlib.sha256(prepared_html.encode()).hexdigest() if editorial_has_substantive_text(prepared_html) else ""
    candidate_html = str(candidate_editorial.get("cleaned_html") or "") if candidate_editorial else ""
    candidate_digest = hashlib.sha256(candidate_html.encode()).hexdigest() if candidate_html else ""
    candidate_integrity = bool(candidate_editorial and candidate_digest == prepared_digest and prepared_digest)
    if candidate_integrity and (not content_present or snapshot_fresh):
        chosen_candidate = candidate_editorial or {}
        return dict(chosen_candidate), {
            "source": "editorial.candidate.json",
            "sha256": candidate_digest,
            "word_count": word_count(candidate_html),
            "snapshot_fresh": snapshot_fresh,
            "candidates": candidates + [{"source": "editorial.candidate.json", "valid": True, "integrity": True}],
        }
    candidates.append({"source": "editorial.candidate.json", "valid": False, "integrity": candidate_integrity})
    return None, {
        "source": None,
        "reason": "no_verifiable_editorial_source",
        "candidates": candidates,
    }


def validation_matches_candidate(validation: dict, candidate: dict, run_id: str) -> bool:
    """A historical checklist is not evidence of a new interrupted run."""
    candidate_run_id = str(candidate.get("_v2_run_id") or "").strip()
    return bool(
        run_id
        and candidate_run_id
        and validation.get("_v2_run_id") == run_id
        and validation.get("candidate_run_id") == candidate_run_id
        and validation.get("candidate_hash")
        == hashlib.sha256(str(candidate.get("content") or "").encode()).hexdigest()
    )


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
        except (OSError, ValueError) as exc:
            raise BatchError(f"JSON invalido em {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise BatchError(f"JSON em {path} precisa ser objeto")
        return value

    def load(self, post: dict[str, Any]) -> dict[str, Any]:
        post_id = int(post["id"])
        directory = self.root / "backups" / str(post_id)
        candidate = self._read_json(directory / "editorial.candidate.json")
        manifest = self._read_json(directory / "editorial.partial.json")
        draft = self._read_json(directory / "editorial.draft.json")
        recovered, recovery = recover_editorial(post, directory)
        if recovered is not None:
            draft = recovered
        elif not editorial_has_substantive_text(draft):
            draft = {}
        return {
            "post": post,
            "post_id": post_id,
            "title": wordpress_text(post.get("title"), field="title"),
            "content": wordpress_text(post.get("content"), field="content"),
            "featured_media": post.get("featured_media"),
            "meta": post.get("meta") if isinstance(post.get("meta"), dict) else {},
            "draft": draft,
            "editorial": draft or None,
            "editorial_recovery": recovery,
            "candidate": candidate or None,
            "manifest": manifest,
            "original_link": (post.get("meta") or {}).get("original_link") if isinstance(post.get("meta"), dict) else None,
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
        self.load_errors: list[dict[str, Any]] = []
        self.pagination: dict[str, Any] = {"truncated": False, "next_page": None}

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
        current_media = state.media
        ideal_required = required_image_count_for_content(html, title=title)
        structure = self.context_loader._read_json(directory / "editorial.structure.json")
        layout_policy = current_media.layout_policy or structure.get("media_layout_policy") or {}
        policy_validation = (
            validate_normal_article_layout_policy(
                html,
                layout_policy,
                target_ideal=ideal_required,
                media=current_media,
                listicle=structure.get("layout") == "listicle_preserved",
            )
            if layout_policy else {"valid": False}
        )
        if policy_validation.get("valid"):
            current_required = int(policy_validation["target_effective"])
        else:
            current_required = ideal_required
            layout_policy = {}
        if current_media.required != current_required or current_media.layout_policy != layout_policy:
            current_media = MediaProgress(
                required=current_required,
                inline=current_media.inline,
                featured=current_media.featured,
                search=current_media.search,
                enrichment_round=current_media.enrichment_round,
                waiver_applied=current_media.waiver_applied,
                waiver_reason=current_media.waiver_reason,
                layout_policy=layout_policy,
                closing_featured_repeat=(
                    current_media.closing_featured_repeat if layout_policy else {}
                ),
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
        candidate_payload = self.context_loader._read_json(directory / "editorial.candidate.json")
        if not validation_matches_candidate(validation, candidate_payload, str(journal.get("run_id") or "")):
            validation = {}
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
            confirmed_ids = {
                item.media_id for item in media.inline
                if item.media_id not in rejected_ids
                and item.media_url not in rejected_urls
                and verified(item.media_id, item.media_url)
            }
            accounting = account_visual_media(media, required_inline=current_required)
            return len(confirmed_ids.intersection(accounting.valid_inline_ids))

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
                    enrichment_round=candidate.enrichment_round,
                    waiver_applied=candidate.waiver_applied,
                    waiver_reason=candidate.waiver_reason,
                    layout_policy=layout_policy,
                    closing_featured_repeat=candidate.closing_featured_repeat,
                )
            except ValueError:
                continue
            score = (
                valid_count(recovered),
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
        self.load_errors = []
        self.pagination = {"truncated": False, "next_page": None}
        seen: set[int] = set()
        for page in range(1, max_pages + 2):
            posts = self.client.list_pending(page=page, per_page=page_size, status="pending")
            if not posts:
                break
            if page > max_pages:
                self.pagination = {"truncated": True, "next_page": page}
                raise BatchError(
                    f"pending queue exceeds snapshot limit {max_pages} pages; resume at page {page}"
                )
            for post in posts:
                post_id = post.get("id")
                if not isinstance(post_id, int) or post_id in seen:
                    continue
                seen.add(post_id)
                try:
                    context = self.context_loader.load(post)
                    state = self._recovered_state(
                        post_id,
                        self.state_store.load(post_id),
                        context,
                    )
                except (BatchError, TypeError, ValueError, KeyError) as exc:
                    # A malformed artifact/state for this candidate must not
                    # poison the whole queue, nor silently turn it into NEW.
                    # Transport and other operational errors still propagate.
                    self.load_errors.append({
                        "post_id": post_id,
                        "error_class": type(exc).__name__,
                        "detail": str(exc)[:240],
                    })
                    continue
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
