"""Application workflows shared by the CLI and integration tests."""

from __future__ import annotations

import datetime
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from .backup import SnapshotStore
from .builder import append_canonical_footer
from .checklist import run_pre_publish_checklist
from .config import Config
from .editorial_schema import validate_editorial
from .html_cleaner import clean_html
from .list_quality import detect_list_format, validate_list_content
from .media.converter import convert_to_webp, prepare_featured_webp
from .media.downloader import download_image
from .media.inserter import append_featured_credit, insert_media
from .media.relevance import extract_entities, image_is_relevant
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
    decision = editorial["site_relevance"]["decision"]
    confidence = float(editorial["site_relevance"].get("confidence") or 0.0)
    if decision == "skip" and confidence < config.min_skip_confidence:
        # Conservative skip (token + accuracy policy): a low-confidence skip is
        # NOT final — record it as uncertain so the post stays pending (out of
        # the processing queue, visible for review) instead of being dropped
        # forever via editorial.latest.json.
        _save_uncertain(root, post_id, editorial)
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "dry_run": config.dry_run,
            "status": "uncertain",
            "skip_reason": editorial["site_relevance"]["reason"],
            "confidence": confidence,
            "backup": str(backup),
        }
    if decision == "process":
        editorial = resolve_editorial_defaults(editorial, post)
    _save_editorial_latest(root, post_id, editorial)
    if decision == "skip":
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "dry_run": config.dry_run,
            "skip_reason": editorial["site_relevance"]["reason"],
            "backup": str(backup),
        }

    media_results, featured_id, featured_credit = _execute_media_plan(editorial, config, client)
    if featured_id is None and not config.dry_run:
        featured_id = _normalize_existing_featured(client, post)
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
            if result.get("media_url") and not result.get("featured")
        ]
        if plan:
            is_list = bool(
                detect_list_format(
                    _post_title(post) or editorial["seo"]["title"], html
                )
            )
            html = insert_media(html, plan, listicle=is_list)
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

    Relevance gate: every candidate must reference a distinctive entity of the
    post (title/keyword/game name). Generic concept matches (e.g. a real bat
    for a game vampire) are rejected before any download/insert — the
    editorial rule is "no image beats a wrong image".
    """
    plan = editorial.get("media_plan") or []
    if not plan:
        return [], None, None
    entities = extract_entities(
        title=str((editorial.get("seo") or {}).get("title") or ""),
        content_html=str(editorial.get("cleaned_html") or ""),
        focus_keyword=str((editorial.get("seo") or {}).get("focus_keyword") or ""),
        game_name=editorial.get("game_name"),
    )

    attachment_cache: dict[int, dict[str, Any]] = {}

    def _attachment_evidence(item: dict[str, Any]) -> dict[str, Any] | None:
        """Resolve a Media Library attachment referenced by ``media_library_id``.

        Reused images are validated against the REAL attachment metadata
        (title/alt/caption/url), never against agent-written text, and are
        re-uploaded as a NEW attachment so the original descriptions are
        never overwritten. Returns None when the item is not a reuse.
        """
        media_id = item.get("media_library_id")
        if not media_id:
            return None
        if media_id not in attachment_cache:
            attachment_cache[media_id] = client.get_media(media_id)
        return attachment_cache[media_id]

    def _rejection_reason(item: dict[str, Any]) -> str | None:
        is_featured = bool(item.get("is_featured"))
        attachment = _attachment_evidence(item)
        if attachment is not None:
            title = str((attachment.get("title") or {}).get("rendered") or "")
            alt = str(attachment.get("alt_text") or "")
            caption = str((attachment.get("caption") or {}).get("rendered") or "")
            url = str(attachment.get("source_url") or "")
            credit = title or caption
            if "crédito da imagem" not in credit.lower():
                return (
                    "reuso da midia library exige credito visivel no attachment original "
                    "(title/caption sem 'Crédito da imagem'); nao usar como fonte"
                )
            source = " ".join(part for part in (url, title, alt, caption) if part)
            if is_featured:
                if not image_is_relevant(
                    alt_text="", credit_text="", source_url=source, entities=entities, source_only=True
                ):
                    listed = ", ".join(sorted(entities)) or "nenhuma"
                    return (
                        "featured reusada deve retratar o assunto citado "
                        f"(attachment sem as entidades: {listed}); escolha key art/imagem do jogo/obra"
                    )
                return None
            if not image_is_relevant(
                alt_text=str(item.get("alt_text") or ""),
                credit_text=str(item.get("credit_text") or ""),
                source_url=source,
                entities=entities,
            ):
                listed = ", ".join(sorted(entities)) or "nenhuma"
                return f"imagem sem relacao com o conteudo (entidades distintas: {listed})"
            return None
        if is_featured:
            # Featured must depict the cited subject itself: only the real
            # source file/page name counts as evidence. The agent-written
            # alt/credit can decorate a wrong image (e.g. a Disney castle
            # captioned "presente em Kingdom Hearts" for a game post), but a
            # true key art file name carries the game/work name.
            if not image_is_relevant(
                alt_text="",
                credit_text="",
                source_url=" ".join(
                    str(item.get(key) or "") for key in ("direct_image_url", "source_page_url")
                ),
                entities=entities,
                source_only=True,
            ):
                listed = ", ".join(sorted(entities)) or "nenhuma"
                return (
                    "featured deve retratar o assunto citado (arquivo/pagina de origem "
                    f"sem as entidades: {listed}); escolha key art/imagem do jogo/obra"
                )
            return None
        if not image_is_relevant(
            alt_text=str(item.get("alt_text") or ""),
            credit_text=str(item.get("credit_text") or ""),
            source_url=" ".join(
                str(item.get(key) or "") for key in ("direct_image_url", "source_page_url")
            ),
            entities=entities,
        ):
            listed = ", ".join(sorted(entities)) or "nenhuma"
            return f"imagem sem relacao com o conteudo (entidades distintas: {listed})"
        return None

    if config.dry_run:
        results: list[dict[str, Any]] = []
        for item in plan:
            reason = _rejection_reason(item)
            results.append(
                {
                    "paragraph_index": item.get("paragraph_index"),
                    "status": "rejected" if reason else "blocked",
                    "detail": reason or "dry-run nao executa download/upload de midia",
                }
            )
        return results, None, None
    results = []
    featured_id: int | None = None
    featured_credit: str | None = None
    with tempfile.TemporaryDirectory(prefix="unicornio-media-") as directory:
        tmp = Path(directory)
        for position, item in enumerate(plan):
            reason = _rejection_reason(item)
            if reason:
                results.append(
                    {
                        "paragraph_index": item.get("paragraph_index"),
                        "status": "rejected",
                        "detail": reason,
                    }
                )
                continue
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
            attachment = _attachment_evidence(item)
            download_url = (
                attachment.get("source_url") if attachment is not None else item["direct_image_url"]
            )
            source = download_image(str(download_url), tmp / f"source_{position}{suffix}")
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


def _normalize_existing_featured(client: WordPressClient, post: dict[str, Any]) -> int | None:
    """Re-prepare an existing featured image at exactly 1280x720 WebP.

    Posts imported with a featured image may carry any size/format; the
    portal rule requires 1280x720 WebP, so the source is re-downloaded and
    re-uploaded through the same conversion path when it does not comply.
    The new attachment KEEPS the original provenance: its file name is
    derived from the source file name and the title/alt are copied as-is
    (fixed bug: the title was serialized as ``str(dict)``), so the
    deterministic featured-relevance gate can still match the work from
    real evidence instead of a generic ``featured-1280x720.webp`` name.
    Returns the (new) attachment id, or None when there is nothing to do.
    """
    featured = post.get("featured_media")
    if not isinstance(featured, int) or featured <= 0:
        return None
    try:
        media = client.get_media(featured)
    except Exception:
        return None
    details = media.get("media_details") or {}
    width, height = details.get("width"), details.get("height")
    source_url = (media.get("source_url") or "").strip()
    if width == 1280 and height == 720 and source_url.lower().endswith(".webp"):
        return featured
    if not source_url:
        return None
    title = str((media.get("title") or {}).get("rendered") or "").strip() or "Imagem de destaque"
    alt = str(media.get("alt_text") or "").strip()
    caption = str((media.get("caption") or {}).get("rendered") or "").strip()
    filename = _featured_filename_from_source(source_url)
    try:
        with tempfile.TemporaryDirectory(prefix="unicornio-featured-") as directory:
            tmp = Path(directory)
            source = download_image(source_url, tmp / "featured_source.jpg")
            webp = prepare_featured_webp(source, tmp / "featured_1280x720.webp")
            new_media = client.upload_media(
                str(webp),
                filename=filename,
                alt_text=alt,
                title=title,
                caption=caption,
            )
    except Exception:
        return None
    new_id = new_media.get("id")
    return new_id if isinstance(new_id, int) else None


def _featured_filename_from_source(source_url: str) -> str:
    """Derive a provenance-carrying filename from the original source URL.

    The re-uploaded featured image must keep the source's evidence in its
    own file name (e.g. ``remothered-red-nuns-legacy-...-1280x720.webp``)
    so the deterministic relevance gate can still match the work. Falls
    back to the generic ``featured-1280x720.webp`` when the source name is
    not usable (non-ascii-only or too short).
    """
    stem = Path(source_url.split("?", 1)[0]).stem or ""
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    if len(slug) < 5:
        return "featured-1280x720.webp"
    return f"{slug[:80].strip('-')}-1280x720.webp"


def resolve_editorial_defaults(editorial: dict[str, Any], post: dict[str, Any]) -> dict[str, Any]:
    """Fill optional editorial fields (seo, cleaned_html) from the post.

    Token-economy defaults: the model must not re-emit content/SEO the post
    already has. ``seo`` is inherited from a valid existing Rank Math meta;
    ``cleaned_html`` reuses the deterministic cleaned content (no-rewrite).
    Raises EditorialValidationError when seo is missing AND the post has no
    valid meta — the model must provide it in that case.
    """
    resolved = dict(editorial)
    if resolved.get("seo") is None:
        resolved["seo"] = _resolve_seo_from_post(post)
    if resolved.get("cleaned_html") is None:
        resolved["cleaned_html"] = clean_html(_raw_content(post))
    return resolved


def _resolve_seo_from_post(post: dict[str, Any]) -> dict[str, Any]:
    from .editorial_schema import EditorialValidationError

    meta = post.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}
    title = meta.get("rank_math_title")
    description = meta.get("rank_math_description")
    keyword = meta.get("rank_math_focus_keyword")
    if (
        isinstance(title, str)
        and title.strip()
        and 0 < len(title.strip()) <= 65
        and isinstance(description, str)
        and 120 <= len(description.strip()) <= 160
        and isinstance(keyword, str)
        and keyword.strip()
    ):
        return {
            "title": title.strip(),
            "meta_description": description.strip(),
            "focus_keyword": keyword.strip(),
        }
    raise EditorialValidationError(
        "seo ausente no JSON e meta Rank Math existente invalida ou inexistente — "
        "o modelo deve fornecer seo (title <= 65, meta_description 120-160, focus_keyword)"
    )


def _save_uncertain(root: Path, post_id: int, editorial: dict[str, Any]) -> None:
    """Record a non-final skip: the post stays pending, out of the queue."""
    try:
        directory = root / "backups" / str(post_id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "uncertain.json").write_text(
            json.dumps(
                {
                    "post_id": post_id,
                    "status": "uncertain",
                    "site_relevance": editorial.get("site_relevance"),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def _save_editorial_latest(root: Path, post_id: int, editorial: dict[str, Any]) -> None:
    """Persist the validated editorial so the publish flow can re-check it."""
    try:
        directory = root / "backups" / str(post_id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "editorial.latest.json").write_text(
            json.dumps(editorial, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def publish_post(
    client: WordPressClient,
    config: Config,
    root: Path,
    post_id: int,
) -> dict[str, Any]:
    """Publish a single post gated by the full pre-publish checklist.

    Sequence: pending check -> editorial.latest.json -> relevance -> snapshot
    -> checklist (all items must pass) -> PUBLISH_ENABLED gate -> publish.
    Every failure returns a ``status`` of skipped/blocked with the reason;
    the post is only ever published when every gate passes.
    """
    post = client.get_post(post_id)
    if post.get("status") != "pending":
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "status": "skipped",
            "reason": f"post status is {post.get('status')}, expected pending",
        }
    editorial_path = root / "backups" / str(post_id) / "editorial.latest.json"
    if not editorial_path.is_file():
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "status": "skipped",
            "reason": "sem editorial.latest.json (post ainda nao passou pelo pipeline)",
        }
    try:
        editorial = validate_editorial(
            json.loads(editorial_path.read_text(encoding="utf-8")),
            min_confidence=config.min_relevance_confidence,
        )
    except (ValueError, OSError) as exc:
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "status": "skipped",
            "reason": f"editorial.latest.json invalido: {exc}",
        }
    if editorial["site_relevance"]["decision"] != "process":
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "status": "skipped",
            "reason": editorial["site_relevance"]["reason"],
        }
    backup = SnapshotStore(root).save(post_id, post)
    checklist = run_pre_publish_checklist(
        post=post,
        editorial=editorial,
        content=_raw_content(post),
        backup_path=backup,
        config=config,
        client=client,
    )
    if checklist["failed"]:
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "status": "blocked",
            "reason": "checklist pre-publicacao com falhas",
            "checklist": checklist,
        }
    if not config.publish_enabled:
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "status": "blocked",
            "reason": "PUBLISH_ENABLED=false (gate de publicacao desligado)",
            "checklist": checklist,
        }
    published_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    result = client.publish(post_id, meta={"_ai_editor_published_at": published_at})
    return {
        "post_id": post_id,
        "wordpress_changed": True,
        "status": "published",
        "status_after": result.get("status"),
        "link": result.get("link"),
        "published_at": published_at,
        "checklist": checklist,
    }


def publish_ready_posts(
    client: WordPressClient,
    config: Config,
    root: Path,
    limit: int = 0,
) -> list[dict[str, Any]]:
    """Publish pending posts that pass the checklist, up to ``limit`` per window.

    ``limit`` counts only successfully published posts; skipped/blocked posts
    do not consume the window quota. ``limit=0`` means no cap.
    """
    outcomes: list[dict[str, Any]] = []
    posts = client.list_pending(per_page=50)
    for candidate in posts:
        candidate_id = candidate.get("id")
        if not isinstance(candidate_id, int):
            continue
        published = sum(1 for outcome in outcomes if outcome.get("wordpress_changed"))
        if limit and published >= limit:
            break
        try:
            outcome = publish_post(client, config, root, candidate_id)
        except Exception as exc:  # noqa: BLE001 - report per post, keep the loop alive
            outcome = {
                "post_id": candidate_id,
                "wordpress_changed": False,
                "status": "error",
                "reason": str(exc),
            }
        outcomes.append(outcome)
    return outcomes


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


def build_queue_report(
    client: WordPressClient,
    root: Path,
    *,
    per_page: int = 50,
    recent_days: int = 7,
) -> dict[str, Any]:
    """Deterministic queue status: pending posts x already-processed ones.

    Read-only. ``edited`` means ``backups/<id>/editorial.latest.json`` exists
    (the post went through the pipeline and waits for the publish cron).
    ``recent_unprocessed_ids`` is the stable line the cron monitor script
    hashes to decide whether an agent run is needed (token economy: no LLM on
    idle). Only posts with ``date_gmt`` inside the last ``recent_days`` are
    monitored, so a months-old pending backlog never wakes the agent and never
    floods the publish flow; the full report still lists every pending post.
    """
    from .content_quality import word_count

    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=recent_days)
    posts = client.list_pending(per_page=per_page)
    rows: list[dict[str, Any]] = []
    unprocessed: list[int] = []
    recent_unprocessed: list[int] = []
    for post in posts:
        post_id = post.get("id")
        if not isinstance(post_id, int):
            continue
        backups_dir = root / "backups" / str(post_id)
        edited = (backups_dir / "editorial.latest.json").is_file()
        prepared = (backups_dir / "prepared.json").is_file()
        uncertain = (backups_dir / "uncertain.json").is_file()
        if not edited and not uncertain:
            unprocessed.append(post_id)
            if _is_recent(post, cutoff):
                recent_unprocessed.append(post_id)
        title = (post.get("title") or {}).get("raw") or (post.get("title") or {}).get("rendered")
        rows.append(
            {
                "id": post_id,
                "date": post.get("date"),
                "date_gmt": post.get("date_gmt"),
                "word_count": word_count((post.get("content") or {}).get("rendered") or ""),
                "prepared": prepared,
                "edited": edited,
                "uncertain": uncertain,
                "title": title,
            }
        )
    rows.sort(key=lambda row: int(row["id"] or 0))
    unprocessed.sort()
    recent_unprocessed.sort()
    return {
        "pending": len(rows),
        "edited": sum(1 for row in rows if row["edited"]),
        "uncertain": sum(1 for row in rows if row["uncertain"]),
        "unprocessed_ids": unprocessed,
        "recent_unprocessed_ids": recent_unprocessed,
        "recent_days": recent_days,
        "posts": rows,
    }


_GAME_HINT_WORDS = frozenset(
    {
        "jogo", "jogos", "game", "games", "gameplay", "demo", "remake",
        "remaster", "dlc", "expansao", "expansão", "expansion", "console",
        "playstation", "ps5", "ps4", "ps3", "xbox", "switch", "nintendo",
        "steam", "gaming", "gameboy", "game boy", "emulador", "emuladores",
        "plataforma", "plataformas", "videogame", "gamepass", "game pass",
    }
)


def _game_hint(title: str) -> bool:
    """Cheap deterministic hint that the post is about a game (LLM confirms)."""
    from .media.relevance import normalize

    tokens = set(re.findall(r"[a-z0-9]+", normalize(title or "")))
    return bool(tokens & _GAME_HINT_WORDS)


def build_cards(
    client: WordPressClient,
    config: Config,
    root: Path,
    *,
    per_page: int | None = None,
) -> dict[str, Any]:
    """Compact per-post cards for the agent (token economy: ONE call).

    Each card carries everything the model needs to write the editorial JSON
    without touching the terminal: title, word count, distinctive entities,
    original link, featured/seo/image gaps, preserved-image count, game hint
    and processing state. Deterministic and read-only.
    """
    from .content_quality import word_count
    from .html_cleaner import clean_html
    from .media.relevance import extract_entities, image_is_relevant, iter_content_images

    per_page = per_page or config.batch_limit
    posts = client.list_pending(per_page=per_page)
    cards: list[dict[str, Any]] = []
    for post in posts:
        post_id = post.get("id")
        if not isinstance(post_id, int):
            continue
        title = (post.get("title") or {}).get("raw") or (post.get("title") or {}).get("rendered") or ""
        raw = (post.get("content") or {}).get("raw") or ""
        rendered = (post.get("content") or {}).get("rendered") or ""
        meta = post.get("meta") or {}
        if not isinstance(meta, dict):
            meta = {}
        entities = extract_entities(title=title, content_html=raw)
        images = iter_content_images(raw)
        relevant_images = [
            item
            for item in images
            if image_is_relevant(
                alt_text=str(item.get("alt") or ""),
                credit_text=str(item.get("caption") or ""),
                source_url=str(item.get("src") or ""),
                entities=entities,
            )
        ]
        preserved = len(re.findall(r"<img\b", clean_html(raw)))
        backups_dir = root / "backups" / str(post_id)
        cards.append(
            {
                "id": post_id,
                "date": post.get("date"),
                "title": title,
                "word_count": word_count(rendered or raw),
                "entities": sorted(entities),
                "original_link": meta.get("original_link"),
                "featured": isinstance(post.get("featured_media"), int) and post["featured_media"] > 0,
                "seo_exists": _seo_is_valid(meta),
                "images": {
                    "total": len(images),
                    "relevantes": len(relevant_images),
                    "preservadas": preserved,
                },
                "game_hint": _game_hint(title),
                "edited": (backups_dir / "editorial.latest.json").is_file(),
                "uncertain": (backups_dir / "uncertain.json").is_file(),
                "prepared": (backups_dir / "prepared.json").is_file(),
            }
        )
    return {"count": len(cards), "cards": cards}


def _seo_is_valid(meta: dict[str, Any]) -> bool:
    title = meta.get("rank_math_title")
    description = meta.get("rank_math_description")
    keyword = meta.get("rank_math_focus_keyword")
    return bool(
        isinstance(title, str)
        and title.strip()
        and len(title.strip()) <= 65
        and isinstance(description, str)
        and 120 <= len(description.strip()) <= 160
        and isinstance(keyword, str)
        and keyword.strip()
    )


def _is_recent(post: dict[str, Any], cutoff: datetime.datetime) -> bool:
    value = post.get("date_gmt") or post.get("date")
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed >= cutoff


def _original_link(post: dict[str, Any]) -> str | None:
    meta = post.get("meta")
    if not isinstance(meta, dict):
        return None
    value = meta.get("original_link")
    return value.strip() if isinstance(value, str) and value.strip() else None
