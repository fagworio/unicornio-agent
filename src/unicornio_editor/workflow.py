"""Application workflows shared by the CLI and integration tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .backup import SnapshotStore
from .builder import append_canonical_footer
from .config import Config
from .editorial_schema import validate_editorial
from .html_cleaner import clean_html
from .seo.rank_math import build_meta
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

    content = append_canonical_footer(editorial["cleaned_html"], _original_link(post))
    if config.dry_run:
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "dry_run": True,
            "backup": str(backup),
            "content_preview": content,
        }

    latest = client.get_post(post_id)
    _require_pending(latest)
    result = client.update_post(
        post_id,
        {
            "content": {"raw": content},
            "meta": build_meta(editorial["seo"], latest.get("meta", {})),
        },
    )
    return {
        "post_id": post_id,
        "wordpress_changed": True,
        "dry_run": False,
        "backup": str(backup),
        "status_after": result.get("status"),
    }


def _require_pending(post: dict[str, Any]) -> None:
    if post.get("status") != "pending":
        raise WorkflowError("post is no longer pending; refusing to process")


def _raw_content(post: dict[str, Any]) -> str:
    content = post.get("content")
    if not isinstance(content, dict) or not isinstance(content.get("raw"), str):
        raise WorkflowError("post content.raw is missing")
    return content["raw"]


def _original_link(post: dict[str, Any]) -> str:
    meta = post.get("meta")
    if not isinstance(meta, dict) or not isinstance(meta.get("original_link"), str):
        raise WorkflowError("post meta.original_link is missing")
    return meta["original_link"]
