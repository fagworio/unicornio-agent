"""Batch envelopes for stateless editorial runs.

The batch layer is deliberately an orchestration boundary, not a second
editorial engine.  It prepares self-contained, per-post envelopes for Hermes
and records every item independently so a malformed or blocked post cannot
invalidate the rest of a batch.
"""

from __future__ import annotations

import datetime
import json
import re
import uuid
from pathlib import Path
from typing import Any, Iterable

from .checklist import required_image_count
from .content_quality import word_count
from .media.relevance import extract_entities
from .state import read_state
from .workflow import (
    _featured_diagnosis,
    _images_summary,
    prepare_post,
)

BATCH_SCHEMA_VERSION = 1
_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


class BatchError(ValueError):
    """Raised when a batch envelope cannot be safely created or read."""


def load_editorial_batch(path: Path | str) -> dict[str, Any]:
    """Load and validate the stateless editorial response envelope.

    Validation happens for the whole file before any post is applied. The
    preferred response uses ``results`` keyed by ``post_id``; legacy ``items``
    remains accepted. A result may be ``needs_retry`` without an editorial
    object, isolating that retry from successful siblings.
    This
    prevents a malformed item late in a batch from producing a partial,
    surprising run caused by an input typo.
    """
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BatchError(f"batch editorial invalido: {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise BatchError("batch editorial precisa ser um objeto JSON")
    version = payload.get("schema_version", BATCH_SCHEMA_VERSION)
    if version != BATCH_SCHEMA_VERSION:
        raise BatchError(
            f"schema_version de batch nao suportado: {version!r} "
            f"(esperado {BATCH_SCHEMA_VERSION})"
        )
    batch_id = validate_batch_id(str(payload.get("batch_id") or "batch-input"))
    # The preferred model response is ``results``; accept the earlier
    # ``items`` spelling for compatibility with already-generated envelopes.
    items = payload.get("results")
    if items is None:
        items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise BatchError("batch editorial precisa de uma lista nao vazia em 'items'")
    if len(items) > 10:
        raise BatchError("batch editorial excede o limite seguro de 10 posts")

    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()
    for position, item in enumerate(items):
        if not isinstance(item, dict):
            raise BatchError(f"items[{position}] precisa ser um objeto")
        post_id = item.get("post_id")
        if isinstance(post_id, bool) or not isinstance(post_id, int) or post_id <= 0:
            raise BatchError(f"items[{position}].post_id invalido")
        if post_id in seen:
            raise BatchError(f"post_id duplicado no batch: {post_id}")
        status = str(item.get("status") or "ok").strip().lower()
        if status not in {"ok", "needs_retry"}:
            raise BatchError(f"items[{position}].status invalido: {status!r}")
        editorial = item.get("editorial")
        if status == "ok" and not isinstance(editorial, dict):
            raise BatchError(f"items[{position}].editorial precisa ser um objeto")
        if status == "needs_retry" and editorial is not None and not isinstance(editorial, dict):
            raise BatchError(f"items[{position}].editorial precisa ser objeto ou ausente")
        seen.add(post_id)
        normalized.append({
            "post_id": post_id,
            "status": status,
            "reason": str(item.get("reason") or "").strip(),
            "editorial": editorial if isinstance(editorial, dict) else {},
        })
    return {
        "schema_version": BATCH_SCHEMA_VERSION,
        "batch_id": batch_id,
        "items": normalized,
    }


def load_vision_batch(path: Path | str) -> dict[str, Any]:
    """Load the independent-candidate envelope used by ``vision-batch``."""
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BatchError(f"batch de visao invalido: {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise BatchError("batch de visao precisa ser um objeto JSON")
    version = payload.get("schema_version", BATCH_SCHEMA_VERSION)
    if version != BATCH_SCHEMA_VERSION:
        raise BatchError(f"schema_version de visao nao suportado: {version!r}")
    batch_id = validate_batch_id(str(payload.get("batch_id") or "vision-input"))
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise BatchError("batch de visao precisa de items[] nao vazio")
    if len(items) > 20:
        raise BatchError("batch de visao excede o limite seguro de 20 imagens")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, item in enumerate(items):
        if not isinstance(item, dict):
            raise BatchError(f"items[{position}] precisa ser um objeto")
        candidate_id = str(item.get("candidate_id") or "").strip()
        image_url = str(item.get("image_url") or "").strip()
        subject = str(item.get("subject") or "").strip()
        if not candidate_id or candidate_id in seen:
            raise BatchError(f"candidate_id ausente ou duplicado: {candidate_id!r}")
        if not image_url.startswith(("http://", "https://")):
            raise BatchError(f"image_url invalida em {candidate_id}")
        if not subject:
            raise BatchError(f"subject vazio em {candidate_id}")
        seen.add(candidate_id)
        normalized.append(
            {
                "candidate_id": candidate_id,
                "image_url": image_url,
                "subject": subject,
                "post_id": item.get("post_id"),
                "require_key_art": bool(item.get("require_key_art")),
            }
        )
    return {
        "schema_version": BATCH_SCHEMA_VERSION,
        "batch_id": batch_id,
        "items": normalized,
    }


def load_media_resolve_batch(path: Path | str) -> dict[str, Any]:
    """Load the deterministic media-resolution envelope.

    This is intentionally separate from the editorial response: the model
    proposes subjects and deficits once, then Python performs discovery,
    provenance checks, scoring and deduplication for all posts.
    """
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BatchError(f"batch de midia invalido: {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise BatchError("batch de midia precisa ser um objeto JSON")
    batch_id = validate_batch_id(str(payload.get("batch_id") or "media-input"))
    items = payload.get("posts")
    if not isinstance(items, list) or not items:
        raise BatchError("batch de midia precisa de posts[] nao vazio")
    if len(items) > 2:
        raise BatchError("batch de midia aceita no maximo 2 posts")
    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()
    for position, item in enumerate(items):
        if not isinstance(item, dict):
            raise BatchError(f"posts[{position}] precisa ser um objeto")
        post_id = item.get("post_id")
        if isinstance(post_id, bool) or not isinstance(post_id, int) or post_id <= 0:
            raise BatchError(f"posts[{position}].post_id invalido")
        if post_id in seen:
            raise BatchError(f"post_id duplicado no batch de midia: {post_id}")
        subject = str(item.get("subject") or "").strip()
        needed = item.get("needed", 1)
        if not subject:
            raise BatchError(f"subject vazio em post {post_id}")
        if isinstance(needed, bool) or not isinstance(needed, int) or not 1 <= needed <= 6:
            raise BatchError(f"needed invalido em post {post_id} (use 1..6)")
        seen.add(post_id)
        normalized.append({
            "post_id": post_id,
            "subject": subject,
            "needed": needed,
            "query": str(item.get("query") or subject).strip(),
            "article_title": str(item.get("article_title") or "").strip(),
            "size": str(item.get("size") or "xga").strip(),
            "ratio": str(item.get("ratio") or "w").strip(),
            "limit": min(10, max(1, int(item.get("limit") or max(needed, 3)))),
            "engine": str(item.get("engine") or "auto").strip(),
        })
    return {"schema_version": BATCH_SCHEMA_VERSION, "batch_id": batch_id, "posts": normalized}


def new_batch_id() -> str:
    """Return a filesystem-safe, human-auditable batch identifier."""
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"batch-{stamp}-{uuid.uuid4().hex[:8]}"


def validate_batch_id(value: str) -> str:
    value = str(value or "").strip()
    if not _BATCH_ID_RE.fullmatch(value):
        raise BatchError(
            "batch_id invalido: use apenas letras, numeros, '.', '_' ou '-' "
            "(ate 96 caracteres)"
        )
    return value


def batch_directory(root: Path | str, batch_id: str) -> Path:
    """Return the runtime directory for one batch."""
    return Path(root) / "work" / "batches" / validate_batch_id(batch_id)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def _post_title(post: dict[str, Any]) -> str:
    title = post.get("title")
    if isinstance(title, dict):
        return str(title.get("raw") or title.get("rendered") or "").strip()
    return str(title or "").strip()


def _post_content(post: dict[str, Any]) -> str:
    content = post.get("content")
    if isinstance(content, dict):
        return str(content.get("raw") or content.get("rendered") or "")
    return str(content or "")


def _seo_snapshot(post: dict[str, Any]) -> dict[str, str]:
    meta = post.get("meta")
    if not isinstance(meta, dict):
        return {}
    fields = {
        "title": "rank_math_title",
        "meta_description": "rank_math_description",
        "focus_keyword": "rank_math_focus_keyword",
    }
    return {
        name: str(meta[key]).strip()
        for name, key in fields.items()
        if isinstance(meta.get(key), str) and meta[key].strip()
    }


def _context_for_post(
    *,
    post: dict[str, Any],
    prepared: dict[str, Any],
    batch_id: str,
    config: Any,
    client: Any,
    root: Path,
) -> dict[str, Any]:
    """Build one complete but bounded input envelope for a model batch."""
    title = _post_title(post)
    source_content = _post_content(post)
    cleaned_html = str(prepared.get("cleaned_html") or "")
    entities = extract_entities(title=title, content_html=cleaned_html)
    words = word_count(cleaned_html)
    images = _images_summary(cleaned_html, title, entities)
    featured = _featured_diagnosis(client, post, entities)
    state = read_state(post)
    meta = post.get("meta") if isinstance(post.get("meta"), dict) else {}
    requirements = {
        "required_inline_images": required_image_count(
            words, title=title, content=cleaned_html
        ),
        "internal_links_enabled": bool(config.internal_links_enabled),
        "featured_required": True,
        "credit_required": True,
        "source_page_required_for_new_media": True,
    }
    return {
        "schema_version": BATCH_SCHEMA_VERSION,
        "batch_id": batch_id,
        "post_id": post.get("id"),
        "status": post.get("status"),
        "state": state,
        "title": title,
        "date": post.get("date"),
        "link": post.get("link"),
        "original_link": prepared.get("original_link") or meta.get("original_link"),
        "source_content": source_content,
        "cleaned_html": cleaned_html,
        "seo_existing": _seo_snapshot(post),
        "featured_media": post.get("featured_media"),
        "entities": sorted(entities),
        "word_count": words,
        "images": images,
        "featured": featured,
        "requirements": requirements,
        "artifacts": {
            "backup": prepared.get("backup"),
            "prepared": str(root / "backups" / str(post.get("id")) / "prepared.json"),
        },
    }


def prepare_batch(
    client: Any,
    config: Any,
    root: Path,
    post_ids: Iterable[int],
    *,
    batch_id: str | None = None,
) -> dict[str, Any]:
    """Snapshot and prepare multiple pending posts independently.

    The full context is written to one file per post.  The returned projection
    is intentionally compact so it can be consumed by a stateless model run.
    """
    batch_id = validate_batch_id(batch_id or new_batch_id())
    directory = batch_directory(root, batch_id)
    requested = [int(post_id) for post_id in post_ids]
    if not requested or len(requested) > 2:
        raise BatchError("prepare-batch aceita de 1 a 2 posts")
    items: list[dict[str, Any]] = []
    contexts: list[dict[str, Any]] = []
    seen: set[int] = set()

    for post_id in requested:
        if post_id in seen:
            items.append({"post_id": post_id, "status": "error", "error": "post_id duplicado"})
            continue
        seen.add(post_id)
        try:
            post = client.get_post(post_id)
            prepared = prepare_post(client, root, post_id)
            envelope = _context_for_post(
                post=post,
                prepared=prepared,
                batch_id=batch_id,
                config=config,
                client=client,
                root=root,
            )
            context_file = directory / f"post-{post_id}.json"
            _write_json(context_file, envelope)
            # Keep the legacy single-post artifact useful when a batch item is
            # later processed by the existing apply/rework flow.
            _write_json(root / "backups" / str(post_id) / "prepared.json", prepared)
            contexts.append(envelope)
            items.append(
                {
                    "post_id": post_id,
                    "status": "prepared",
                    "title": envelope["title"],
                    "word_count": envelope["word_count"],
                    "required_inline_images": envelope["requirements"][
                        "required_inline_images"
                    ],
                    "images": {
                        key: envelope["images"].get(key)
                        for key in ("valid", "missing", "irrelevant", "non_webp")
                        if key in envelope["images"]
                    },
                    "context_file": str(context_file),
                    "backup": prepared.get("backup"),
                }
            )
        except Exception as exc:  # noqa: BLE001 - isolate one post in a batch
            items.append(
                {
                    "post_id": post_id,
                    "status": "error",
                    "error": str(exc)[:240],
                }
            )

    manifest = {
        "schema_version": BATCH_SCHEMA_VERSION,
        "batch_id": batch_id,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"
        ),
        "requested_ids": requested,
        "count": len(items),
        "prepared": sum(1 for item in items if item.get("status") == "prepared"),
        "failed": sum(1 for item in items if item.get("status") == "error"),
        "items": items,
    }
    editorial_input = directory / "editorial.input.json"
    _write_json(
        editorial_input,
        {
            "schema_version": BATCH_SCHEMA_VERSION,
            "batch_id": batch_id,
            "posts": contexts,
        },
    )
    manifest["editorial_input"] = str(editorial_input)
    _write_json(directory / "manifest.json", manifest)
    return {
        "schema_version": BATCH_SCHEMA_VERSION,
        "batch_id": batch_id,
        "manifest": str(directory / "manifest.json"),
        "count": len(items),
        "prepared": manifest["prepared"],
        "failed": manifest["failed"],
        "items": items,
        "editorial_input": str(editorial_input),
    }


__all__ = [
    "BATCH_SCHEMA_VERSION",
    "BatchError",
    "batch_directory",
    "new_batch_id",
    "load_editorial_batch",
    "load_vision_batch",
    "load_media_resolve_batch",
    "prepare_batch",
    "validate_batch_id",
]
