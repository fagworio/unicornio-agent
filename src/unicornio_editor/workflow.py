"""Application workflows shared by the CLI and integration tests."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from .backup import SnapshotStore
from .builder import append_canonical_footer
from .checklist import run_pre_publish_checklist
from .config import Config
from .editorial_schema import validate_editorial
from .html_cleaner import clean_html
from .list_quality import validate_list_content
from .media.converter import convert_to_webp, prepare_featured_webp
from .media.downloader import download_image
from .media.inserter import append_featured_credit, insert_media
from .media.wordpress_media import upload_image
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

    media_results, featured_id, featured_credit = _execute_media_plan(editorial, config, client)
    html = editorial["cleaned_html"]
    if media_results and not config.dry_run:
        plan = [
            {
                "paragraph_index": result["paragraph_index"],
                "media_url": result["media_url"],
                "alt_text": result["alt_text"],
                "credit_text": result["credit_text"],
            }
            for result in media_results
            if not result.get("featured")
        ]
        if plan:
            html = insert_media(html, plan)
    editorial_with_media = {**editorial, "cleaned_html": html}
    content, trailer = compose_final_content(editorial_with_media, config, original_link_of(post))
    if featured_credit and not config.dry_run:
        content = append_featured_credit(content, featured_credit)
    checklist = run_pre_publish_checklist(
        post={**post, "featured_media": featured_id or post.get("featured_media")},
        editorial=editorial_with_media,
        content=content,
        backup_path=backup,
        config=config,
        client=client,
    )
    validate_list_content(_post_title(post) or editorial["seo"]["title"], content)
    if config.dry_run:
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "dry_run": True,
            "backup": str(backup),
            "content_preview": content,
            "trailer": trailer,
            "media_plan_results": media_results,
            "checklist": checklist,
        }

    latest = client.get_post(post_id)
    _require_pending(latest)
    update_payload: dict[str, Any] = {
        "content": {"raw": content},
        "meta": {
            **build_meta(editorial["seo"], latest.get("meta", {})),
            **build_processing_markers(
                editorial["site_relevance"]["decision"],
                editorial["site_relevance"]["confidence"],
            ),
        },
    }
    if featured_id:
        update_payload["featured_media"] = featured_id
    result = client.update_post(post_id, update_payload)
    return {
        "post_id": post_id,
        "wordpress_changed": True,
        "dry_run": False,
        "backup": str(backup),
        "status_after": result.get("status"),
        "trailer": trailer,
        "media_plan_results": media_results,
        "featured_media": result.get("featured_media"),
        "checklist": checklist,
    }


def _execute_media_plan(
    editorial: dict[str, Any],
    config: Config,
    client: WordPressClient,
) -> tuple[list[dict[str, Any]], int | None, str | None]:
    """Download, convert to WebP, upload and report the editorial media plan.

    Featured candidates are prepared at exactly 1200x720. In dry-run the plan
    is reported but never executed (uploads are write operations).
    """
    plan = editorial.get("media_plan") or []
    if not plan:
        return [], None, None
    if config.dry_run:
        return (
            [
                {
                    "paragraph_index": item.get("paragraph_index"),
                    "status": "blocked",
                    "detail": "dry-run nao executa download/upload de midia",
                }
                for item in plan
            ],
            None,
            None,
        )
    results: list[dict[str, Any]] = []
    featured_id: int | None = None
    featured_credit: str | None = None
    with tempfile.TemporaryDirectory(prefix="unicornio-media-") as directory:
        tmp = Path(directory)
        for position, item in enumerate(plan):
            evidence = {
                name: item[name]
                for name in (
                    "source_page_url",
                    "direct_image_url",
                    "author",
                    "license",
                    "license_url",
                    "captured_at",
                    "credit_text",
                    "alt_text",
                )
            }
            suffix = Path(item["direct_image_url"].split("?", 1)[0]).suffix or ".jpg"
            source = download_image(item["direct_image_url"], tmp / f"source_{position}{suffix}")
            is_featured = bool(item.get("is_featured"))
            if is_featured:
                webp = prepare_featured_webp(source, tmp / f"featured_{position}.webp")
            else:
                webp = convert_to_webp(source, tmp / f"inline_{position}.webp")
            media = upload_image(client, webp, evidence)
            media_id = media.get("id")
            media_url = media.get("source_url")
            if not media_id or not media_url:
                raise WorkflowError(f"media upload returned no id/source_url (item {position})")
            result: dict[str, Any] = {
                "paragraph_index": item["paragraph_index"],
                "media_id": media_id,
                "media_url": media_url,
                "alt_text": item["alt_text"],
                "credit_text": item["credit_text"],
                "featured": is_featured,
            }
            results.append(result)
            if is_featured:
                featured_id = media_id
                featured_credit = item["credit_text"]
    return results, featured_id, featured_credit


def _discover_trailer(editorial: dict[str, Any], config: Config) -> dict[str, str] | None:
    """Discover a YouTube trailer for game content; fail-closed to None."""
    game_name = editorial.get("game_name")
    if not isinstance(game_name, str) or not game_name.strip():
        return None
    try:
        return find_game_trailer(game_name, timeout=config.http_timeout)
    except TrailerError:
        return None


def compose_final_content(
    editorial: dict[str, Any],
    config: Config,
    original_link: str | None,
) -> tuple[str, dict[str, str] | None]:
    """Build the final content: cleaned HTML + optional trailer embed + canonical footer.

    Returns ``(content, trailer)`` so callers can report what was embedded.
    """
    html = editorial["cleaned_html"]
    trailer = _discover_trailer(editorial, config)
    if trailer is not None:
        html = html.rstrip() + "\n\n" + build_trailer_html(trailer)
    return append_canonical_footer(html, original_link), trailer


def original_link_of(post: dict[str, Any]) -> str | None:
    """Read the ``original_link`` custom field from the post meta (REST edit context)."""
    return _original_link(post)


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
