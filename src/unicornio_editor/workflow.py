"""Application workflows shared by the CLI and integration tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .backup import SnapshotStore
from .builder import append_canonical_footer
from .config import Config
from .editorial_schema import validate_editorial
from .html_cleaner import clean_html
from .list_quality import validate_list_content
from .observability import build_processing_markers
from .seo.rank_math import build_meta
from .trailer import TrailerError, build_trailer_html, find_game_trailer
from .wordpress import WordPressClient


class WorkflowError(RuntimeError):
    """Raised when a post cannot safely enter a workflow step."""


def prepare_post(client: WordPressClient, root: Path, post_id: int) -> dict[str, Any]:
    post = client.get_post(post_id)
    _require_pending(post)
    backup = SnapshotStore(root).save(post_id, post)
    raw = _raw_content(post)
    return {
        "post_id": post_id,
        "status": post["status"],
        "backup": str(backup),
        "cleaned_html": clean_html(raw),
        "original_link": _original_link(post),
        "wordpress_changed": False,
    }


def apply_editorial(
    client: WordPressClient,
    config: Config,
    root: Path,
    post_id: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    post = client.get_post(post_id)
    _require_pending(post)
    backup = SnapshotStore(root).save(post_id, post)
    editorial = validate_editorial(payload, min_confidence=config.min_relevance_confidence)
    if editorial["site_relevance"]["decision"] == "skip":
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "dry_run": config.dry_run,
            "skip_reason": editorial["site_relevance"]["reason"],
            "backup": str(backup),
        }

    html = editorial["cleaned_html"]
    trailer = _discover_trailer(editorial, config)
    if trailer is not None:
        html = html.rstrip() + "\n\n" + build_trailer_html(trailer)
    content = append_canonical_footer(html, _original_link(post))
    validate_list_content(_post_title(post) or editorial["seo"]["title"], content)
    if config.dry_run:
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "dry_run": True,
            "backup": str(backup),
            "content_preview": content,
            "trailer": trailer,
        }

    latest = client.get_post(post_id)
    _require_pending(latest)
    result = client.update_post(
        post_id,
        {
            "content": {"raw": content},
            "meta": {
                **build_meta(editorial["seo"], latest.get("meta", {})),
                **build_processing_markers(
                    editorial["site_relevance"]["decision"],
                    editorial["site_relevance"]["confidence"],
                ),
            },
        },
    )
    return {
        "post_id": post_id,
        "wordpress_changed": True,
        "dry_run": False,
        "backup": str(backup),
        "status_after": result.get("status"),
        "trailer": trailer,
    }


def _discover_trailer(editorial: dict[str, Any], config: Config) -> dict[str, str] | None:
    """Discover a YouTube trailer for game content; fail-closed to None."""
    game_name = editorial.get("game_name")
    if not isinstance(game_name, str) or not game_name.strip():
        return None
    try:
        return find_game_trailer(game_name, timeout=config.http_timeout)
    except TrailerError:
        return None


def _require_pending(post: dict[str, Any]) -> None:
    if post.get("status") != "pending":
        raise WorkflowError("post is no longer pending; refusing to process")


def _raw_content(post: dict[str, Any]) -> str:
    content = post.get("content")
    if not isinstance(content, dict) or not isinstance(content.get("raw"), str):
        raise WorkflowError("post content.raw is missing")
    return content["raw"]


def _post_title(post: dict[str, Any]) -> str | None:
    title = post.get("title")
    if isinstance(title, dict) and isinstance(title.get("raw"), str):
        return title["raw"].strip() or None
    if isinstance(title, str):
        return title.strip() or None
    return None


def _original_link(post: dict[str, Any]) -> str | None:
    meta = post.get("meta")
    if not isinstance(meta, dict):
        return None
    value = meta.get("original_link")
    return value.strip() if isinstance(value, str) and value.strip() else None
