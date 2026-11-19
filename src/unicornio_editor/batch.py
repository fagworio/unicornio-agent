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
from .language import detect_language, localization_required
from .media.relevance import extract_entities
from .media.inserter import plan_normal_media_insertions
from .list_quality import detect_list_format, extract_list_items, list_item_media_assets
from .state import read_state, uncertain_second_pass_eligible
from .workflow import (
    _acquire_post_lock,
    _featured_diagnosis,
    _images_summary,
    is_post_lock_blocked,
    prepare_post,
    resolve_editorial_defaults,
)

BATCH_SCHEMA_VERSION = 1
MEDIA_BATCH_REPLACEMENT_RESERVE = 2
MEDIA_BATCH_MAX_INLINE_TARGETS = 12
_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_EDITORIAL_FAILURE_CLASSES = frozenset({
    "api_http_error", "timeout", "transport_error", "invalid_response",
    "editorial_schema_invalid", "editorial_content_retry",
    "request_input_invalid", "configuration_error",
})


class BatchError(ValueError):
    """Raised when a batch envelope cannot be safely created or read."""


def resolve_batch_editorial_defaults(client: Any, batch: dict[str, Any]) -> dict[str, Any]:
    """Fill omitted editorial fields from the current WordPress snapshot.

    Hermes may return ``cleaned_html: null`` when no rewrite is needed.  The
    deterministic media bridge still needs the current HTML to place inline
    images, so resolve the same V1 defaults used by ``apply`` before joining
    the media manifest.  This performs read-only WordPress reads only.
    """
    items: list[dict[str, Any]] = []
    for raw_item in batch.get("items") or []:
        item = dict(raw_item)
        if str(item.get("status") or "ok") == "ok":
            post_id = int(item["post_id"])
            post = client.get_post(post_id)
            item["editorial"] = resolve_editorial_defaults(
                dict(item.get("editorial") or {}), post
            )
        items.append(item)
    return {**batch, "items": items}


def build_media_resolve_batch_input(
    editorial_batch: dict[str, Any], root: Path | str,
) -> dict[str, Any]:
    """Build the V1 media request from final editorial content and prepared facts.

    ``needed`` is the total number of new candidates the bridge needs: the
    remaining inline deficit plus one new featured candidate only when the
    prepared featured diagnosis says ``provide`` or ``replace``. This keeps the
    featured asset outside the inline-image quota without asking Hermes to do
    arithmetic or infer the role from a prose instruction.
    """
    batch_id = validate_batch_id(str(editorial_batch.get("batch_id") or ""))
    results = editorial_batch.get("results")
    if not isinstance(results, list):
        raise BatchError("editorial batch precisa conter results[]")
    directory = batch_directory(root, batch_id)
    requests: list[dict[str, Any]] = []
    seen: set[int] = set()
    for result in results:
        if not isinstance(result, dict) or result.get("status") != "ok":
            continue
        post_id = result.get("post_id")
        if isinstance(post_id, bool) or not isinstance(post_id, int) or post_id <= 0:
            raise BatchError("post_id inválido ao montar media-resolve-batch")
        if post_id in seen:
            raise BatchError(f"post_id duplicado ao montar batch de mídia: {post_id}")
        seen.add(post_id)
        context_path = directory / f"post-{post_id}.json"
        try:
            context = json.loads(context_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BatchError(f"contexto preparado ausente/inválido para post {post_id}") from exc
        if not isinstance(context, dict) or int(context.get("post_id") or 0) != post_id:
            raise BatchError(f"contexto preparado não corresponde ao post {post_id}")
        editorial = result.get("editorial")
        editorial = editorial if isinstance(editorial, dict) else {}
        relevance = editorial.get("site_relevance")
        if isinstance(relevance, dict) and relevance.get("decision") != "process":
            continue
        title = str(editorial.get("title") or context.get("title") or "").strip()
        cleaned_html = editorial.get("cleaned_html")
        if not isinstance(cleaned_html, str) or not cleaned_html.strip():
            cleaned_html = str(context.get("cleaned_html") or "")
        entities = {
            str(value).strip()
            for value in (context.get("entities") or [])
            if str(value).strip()
        }
        words = word_count(cleaned_html)
        images = _images_summary(cleaned_html, title, entities)
        inline_deficit = max(0, int(images["required"]) - int(images["valid"]))
        required_inline_images = required_image_count(
            words, title=title, content=cleaned_html
        )
        featured = context.get("featured")
        featured_action = str(
            featured.get("action") if isinstance(featured, dict) else "provide"
        ).strip().casefold()
        featured_required = featured_action not in {"ok", "normalize"}
        categories = editorial.get("categories") or context.get("categories") or ()
        promised_items = detect_list_format(title, cleaned_html, categories)
        list_items = extract_list_items(title, cleaned_html, categories)
        targets: list[dict[str, Any]] | None = None
        if promised_items is not None:
            if len(list_items) != promised_items:
                raise BatchError(
                    f"post {post_id} promete {promised_items} itens, mas o HTML contém {len(list_items)}"
                )
            existing_assets = list_item_media_assets(title, cleaned_html, categories)
            covered = {
                str(asset.get("item_identity") or "")
                for asset in existing_assets
                if asset.get("item_identity")
            }
            missing_items = [item for item in list_items if item.identity not in covered]
            inline_target_count = max(inline_deficit, len(missing_items))
            if inline_target_count > MEDIA_BATCH_MAX_INLINE_TARGETS:
                raise BatchError(
                    f"post {post_id} precisa de {inline_target_count} imagens inline; "
                    f"o limite seguro é {MEDIA_BATCH_MAX_INLINE_TARGETS}"
                )
            targets = []
            for item in missing_items:
                targets.append({
                    "target_id": item.identity,
                    "role": "inline",
                    "subject": item.heading,
                    "query": f"{item.heading} {title}".strip(),
                    "paragraph_index": item.paragraph_index,
                    "item_identity": item.identity,
                    "item_title": item.item_title,
                    "item_group": item.group_heading,
                    "item_number": item.number,
                    "section_heading": item.heading,
                    "section_slot": item.paragraph_index,
                    "html_position": item.html_position,
                    "replacement_reserve": 1,
                })
            extra_count = inline_target_count - len(missing_items)
            for extra_index in range(extra_count):
                item = list_items[extra_index % len(list_items)]
                targets.append({
                    "target_id": f"extra-{extra_index + 1}-{item.identity}",
                    "role": "inline",
                    "subject": item.heading,
                    "query": f"{item.heading} {title}".strip(),
                    "paragraph_index": item.paragraph_index,
                    "placement": "after_list_item_description",
                    "item_identity": item.identity,
                    "item_title": item.item_title,
                    "item_group": item.group_heading,
                    "item_number": item.number,
                    "section_heading": item.heading,
                    "section_slot": item.paragraph_index,
                    "html_position": item.html_position,
                    "replacement_reserve": 1,
                })
            if featured_required:
                feature_subject = str(editorial.get("game_name") or title).strip()
                targets.append({
                    "target_id": "featured",
                    "role": "featured",
                    "subject": feature_subject,
                    "query": feature_subject,
                    "replacement_reserve": 1,
                })
        needed = len(targets) if targets is not None else inline_deficit + int(featured_required)
        if needed <= 0:
            continue
        if targets is None and needed > 7:
            raise BatchError(
                f"post {post_id} precisa de {needed} imagens candidatas; "
                "o limite atual de media-resolve-batch é 7"
            )
        subject = str(editorial.get("game_name") or title).strip()
        if not subject:
            raise BatchError(f"subject vazio ao montar mídia para post {post_id}")
        requests.append({
            "post_id": post_id,
            "subject": subject,
            "query": subject,
            "article_title": title,
            "needed": needed,
            "required_inline_images": int(required_inline_images),
            "inline_deficit": inline_deficit,
            "featured_required": featured_required,
            "replacement_reserve": 0 if targets is not None else MEDIA_BATCH_REPLACEMENT_RESERVE,
            **({"targets": targets} if targets is not None else {}),
        })
    if len(requests) > 2:
        raise BatchError("batch de mídia aceita no máximo 2 posts")
    if not requests:
        stale_input = directory / "media.input.json"
        stale_input.unlink(missing_ok=True)
        return {"status": "not_required", "path": None, "count": 0, "posts": []}
    path = directory / "media.input.json"
    _write_json(path, {
        "schema_version": BATCH_SCHEMA_VERSION,
        "batch_id": batch_id,
        "posts": requests,
    })
    return {"status": "required", "path": str(path), "count": len(requests), "posts": requests}


def _media_manifest_candidate(candidate: dict[str, Any], *, subject: str, query: str,
                              post_id: int, index: int, featured: bool,
                              media_source_policy: str = "strict") -> dict[str, Any] | None:
    """Project one verified manifest candidate into the editorial schema.

    ``media-resolve-batch`` deliberately emits a compact candidate projection;
    this bridge keeps only the provenance needed by ``apply-batch`` and never
    invents a usable URL when the resolver did not prove one.
    """
    direct_url = str(
        candidate.get("direct_image_url") or candidate.get("url") or ""
    ).strip()
    source_url = str(
        candidate.get("source_page_url") or candidate.get("source") or ""
    ).strip()
    verdict = str(
        candidate.get("evidence_verdict")
        or candidate.get("verdict")
        or ((candidate.get("evidence") or {}).get("verdict") if isinstance(candidate.get("evidence"), dict) else "")
        or ""
    ).strip()
    credit_text = str(candidate.get("credit_text") or "").strip()
    if not direct_url.startswith(("http://", "https://")):
        return None
    if source_url and not source_url.startswith(("http://", "https://")):
        source_url = ""
    if not source_url and media_source_policy != "credit_only":
        return None
    if not credit_text:
        from .media.text import credit_text_with_hosting_domain, credit_text_with_source_site
        credit_text = (
            credit_text_with_hosting_domain("", direct_url)
            if media_source_policy == "credit_only"
            else credit_text_with_source_site("", source_url)
        )
    if not credit_text:
        return None
    allowed_verdicts = {"deterministic_match"}
    if not source_url and media_source_policy == "credit_only":
        allowed_verdicts.add("credit_only_accepted")
    # Ambiguous candidates still need a definitive decision before assembly.
    if verdict and verdict not in allowed_verdicts:
        return None
    alt_suffix = "imagem principal" if featured else "imagem de apoio"
    item: dict[str, Any] = {
        "paragraph_index": 0,
        "direct_image_url": direct_url,
        "alt_text": f"{subject} - {alt_suffix}"[:240],
        "credit_text": credit_text,
        "is_featured": featured,
        "subject": subject,
        "search_query": query,
        "candidate_id": str(candidate.get("candidate_id") or f"media-{post_id}-{index + 1}"),
        "provenance_mode": (
            "credit_only" if media_source_policy == "credit_only"
            else ("source_page" if source_url else "credit_only")
        ),
    }
    if source_url and media_source_policy != "credit_only":
        item["source_page_url"] = source_url
    origin_evidence = candidate.get("origin_evidence")
    if isinstance(origin_evidence, dict):
        raw_fields = origin_evidence.get("fields")
        allowed_fields = {
            "filename", "og_title", "page_title", "alt_original",
            "figcaption", "heading", "page_url",
        }
        fields = {
            key: str(raw_fields[key])[:500]
            for key in allowed_fields
            if isinstance(raw_fields, dict)
            and isinstance(raw_fields.get(key), str)
            and raw_fields[key].strip()
        }
        if fields:
            item["origin_evidence"] = {
                "verdict": str(origin_evidence.get("verdict") or "")[:48],
                "score": origin_evidence.get("score"),
                "local_score": origin_evidence.get("local_score"),
                "subject": str(origin_evidence.get("subject") or subject)[:200],
                "matched": [
                    str(value)[:48]
                    for value in (origin_evidence.get("matched") or [])
                    if str(value) in allowed_fields
                ],
                "fields": fields,
            }
    item["source_page_verified"] = bool(
        media_source_policy != "credit_only" and source_url and verdict == "deterministic_match"
    )
    item["rights_verified"] = False
    for key in ("media_library_id", "evidence_score", "phash"):
        if candidate.get(key) is not None:
            item[key] = candidate[key]
    if verdict:
        item["evidence_verdict"] = verdict
    return item


def merge_media_manifest_into_editorial(
    batch: dict[str, Any],
    manifest_path: Path | str,
    *,
    media_source_policy: str = "strict",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Attach verified media candidates to an editorial batch deterministically.

    The model output intentionally contains ``media_plan: []``.  This bridge
    joins by ``post_id`` with the sibling ``media.manifest.json`` produced by
    ``media-resolve-batch``.  It writes no WordPress state and does not replace
    an explicitly supplied plan; an empty/invalid manifest stays empty and is
    reported for the normal media gate to classify.
    """
    source = Path(manifest_path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BatchError(f"manifest de midia invalido: {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise BatchError("manifest de midia precisa ser um objeto JSON")
    if str(payload.get("batch_id") or "") != str(batch.get("batch_id") or ""):
        raise BatchError("media.manifest.json pertence a outro batch")
    posts = payload.get("posts")
    if not isinstance(posts, list):
        raise BatchError("media.manifest.json precisa conter posts[]")
    by_post = {
        int(row["post_id"]): row
        for row in posts
        if isinstance(row, dict)
        and isinstance(row.get("post_id"), int)
    }
    merged_items: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "manifest": str(source),
        "posts": 0,
        "assembled": 0,
        "unchanged": 0,
        "fallback_candidates": 0,
        "missing_candidates": [],
        "rejected_candidates": 0,
    }
    for raw_item in batch.get("items") or []:
        item = dict(raw_item)
        editorial = dict(item.get("editorial") or {})
        existing_plan = editorial.get("media_plan")
        if existing_plan:
            report["unchanged"] += 1
            merged_items.append(item)
            continue
        post_id = int(item["post_id"])
        media_row = by_post.get(post_id) or {}
        subject = str(media_row.get("subject") or "").strip()
        query = str(media_row.get("query") or subject).strip()
        featured_required = media_row.get(
            "featured_required", str(media_row.get("role") or "").strip().lower() != "inline"
        )
        if not isinstance(featured_required, bool):
            raise BatchError(f"featured_required inválido em post {post_id}")
        target_rows = media_row.get("targets")
        if isinstance(target_rows, list):
            featured_plan: list[dict[str, Any]] = []
            inline_plan: list[dict[str, Any]] = []
            fallbacks: list[dict[str, Any]] = []
            seen_target_urls: set[str] = set()
            missing_target = False
            for target in target_rows:
                if not isinstance(target, dict):
                    continue
                target_id = str(target.get("target_id") or "").strip()
                target_role = str(target.get("role") or "").strip().lower()
                target_subject = str(target.get("subject") or "").strip()
                target_query = str(target.get("query") or target_subject).strip()
                if not target_id or not target_subject or target_role not in {"inline", "featured"}:
                    raise BatchError(f"target inválido no manifest do post {post_id}")
                reserve = target.get("replacement_reserve", 0)
                if (
                    isinstance(reserve, bool)
                    or not isinstance(reserve, int)
                    or not 0 <= reserve <= MEDIA_BATCH_REPLACEMENT_RESERVE
                ):
                    raise BatchError(f"replacement_reserve inválido no target {target_id}")
                raw_candidates = [
                    *(target.get("reuse") or []),
                    *(target.get("candidates") or []),
                ]
                projected_target: list[dict[str, Any]] = []
                for candidate_index, candidate in enumerate(raw_candidates):
                    if not isinstance(candidate, dict):
                        continue
                    direct_url = str(
                        candidate.get("direct_image_url") or candidate.get("url") or ""
                    ).strip()
                    if not direct_url or direct_url in seen_target_urls:
                        continue
                    seen_target_urls.add(direct_url)
                    planned = _media_manifest_candidate(
                        candidate,
                        subject=target_subject,
                        query=target_query,
                        post_id=post_id,
                        index=candidate_index,
                        featured=target_role == "featured",
                        media_source_policy=media_source_policy,
                    )
                    if planned is None:
                        report["rejected_candidates"] += 1
                        continue
                    planned["target_id"] = target_id
                    for key in (
                        "paragraph_index", "placement", "item_identity", "item_title",
                        "item_group", "item_number", "section_heading", "section_slot",
                        "html_position",
                    ):
                        if key in target:
                            planned[key] = target[key]
                    projected_target.append(planned)
                if not projected_target:
                    missing_target = True
                    continue
                primary = projected_target[0]
                if target_role == "featured":
                    featured_plan.append(primary)
                else:
                    inline_plan.append(primary)
                fallbacks.extend(projected_target[1:1 + reserve])
            html = str(editorial.get("cleaned_html") or "")
            if inline_plan and not html:
                raise BatchError(f"post {post_id} precisa de cleaned_html para montar mídia de lista")
            editorial["media_plan"] = [*featured_plan, *inline_plan]
            item["editorial"] = editorial
            if fallbacks:
                item["media_fallback_candidates"] = fallbacks
                report["fallback_candidates"] += len(fallbacks)
            if missing_target:
                report["missing_candidates"].append(post_id)
            report["assembled"] += int(bool(featured_plan or inline_plan))
            merged_items.append(item)
            continue
        candidates = []
        # Reuse is already enriched and is preferred over new discovery.
        candidates.extend(media_row.get("reuse") or [])
        candidates.extend(media_row.get("candidates") or [])
        projected: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            direct_url = str(candidate.get("direct_image_url") or candidate.get("url") or "").strip()
            if not direct_url or direct_url in seen_urls:
                continue
            seen_urls.add(direct_url)
            planned = _media_manifest_candidate(
                candidate,
                subject=subject,
                query=query,
                post_id=post_id,
                index=len(projected),
                featured=featured_required and not any(row.get("is_featured") for row in projected),
                media_source_policy=media_source_policy,
            )
            if planned is None:
                report["rejected_candidates"] += 1
                continue
            projected.append(planned)
        if projected:
            needed = max(0, int(media_row.get("needed") or 0))
            reserve = media_row.get("replacement_reserve", 0)
            if (
                isinstance(reserve, bool)
                or not isinstance(reserve, int)
                or not 0 <= reserve <= MEDIA_BATCH_REPLACEMENT_RESERVE
            ):
                raise BatchError(f"replacement_reserve invalido no manifest do post {post_id}")
            primary = projected[:needed]
            fallbacks = projected[needed:needed + reserve]
            featured_rows = [row for row in primary if row.get("is_featured")]
            inline = [dict(row, is_featured=False) for row in primary if not row.get("is_featured")]
            if inline:
                html = str(editorial.get("cleaned_html") or "")
                if not html:
                    raise BatchError(f"post {post_id} precisa de cleaned_html para montar o media_plan")
                try:
                    inline = plan_normal_media_insertions(html, inline)
                except Exception as exc:
                    raise BatchError(f"post {post_id} sem slots seguros para media_plan: {exc}") from exc
            editorial["media_plan"] = [*featured_rows, *inline]
            item["editorial"] = editorial
            if fallbacks:
                item["media_fallback_candidates"] = fallbacks
                report["fallback_candidates"] += len(fallbacks)
            report["assembled"] += 1
        else:
            report["missing_candidates"].append(post_id)
        merged_items.append(item)
    report["posts"] = len(merged_items)
    return {**batch, "items": merged_items}, report


def wordpress_text(value: Any, *, field: str) -> str:
    """Normalize a WordPress text field without assuming its wire shape."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        raw = value.get("raw")
        rendered = value.get("rendered")
        if raw is not None and not isinstance(raw, str):
            raise BatchError(f"{field}.raw precisa ser string")
        if rendered is not None and not isinstance(rendered, str):
            raise BatchError(f"{field}.rendered precisa ser string")
        return str(raw or rendered or "").strip()
    if value is None:
        return ""
    raise BatchError(f"{field} precisa ser string ou objeto WordPress")


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
        failure_class = str(item.get("failure_class") or "")
        retryable = item.get("retryable", True)
        normalized.append({
            "post_id": post_id,
            "status": status,
            "reason": str(item.get("reason") or "").strip(),
            "retry_mode": str(item.get("retry_mode") or "").strip(),
            "retry_kind": str(item.get("retry_kind") or "none").strip(),
            "failure_class": failure_class if failure_class in _EDITORIAL_FAILURE_CLASSES else "",
            "retryable": retryable if isinstance(retryable, bool) else True,
            "http_status": (
                int(item["http_status"])
                if isinstance(item.get("http_status"), int)
                and not isinstance(item.get("http_status"), bool)
                and 100 <= item["http_status"] <= 599
                else None
            ),
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
        raw_targets = item.get("targets")
        has_targets = raw_targets is not None
        maximum_needed = MEDIA_BATCH_MAX_INLINE_TARGETS + 1 if has_targets else 7
        if not subject:
            raise BatchError(f"subject vazio em post {post_id}")
        if isinstance(needed, bool) or not isinstance(needed, int) or not 1 <= needed <= maximum_needed:
            raise BatchError(f"needed invalido em post {post_id} (use 1..{maximum_needed})")
        seen.add(post_id)
        role = str(item.get("role") or "").strip().lower()
        role = role if role in {"inline", "featured"} else ""
        featured_required = item.get("featured_required", role != "inline")
        if not isinstance(featured_required, bool):
            raise BatchError(f"featured_required invalido em post {post_id}")
        reserve = item.get("replacement_reserve", 0)
        if (
            isinstance(reserve, bool)
            or not isinstance(reserve, int)
            or not 0 <= reserve <= MEDIA_BATCH_REPLACEMENT_RESERVE
        ):
            raise BatchError(
                f"replacement_reserve invalido em post {post_id} "
                f"(use 0..{MEDIA_BATCH_REPLACEMENT_RESERVE})"
            )
        image_counts: dict[str, int | None] = {}
        for field in ("required_inline_images", "inline_deficit"):
            value = item.get(field)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise BatchError(f"{field} invalido em post {post_id}")
            image_counts[field] = value
        normalized_targets: list[dict[str, Any]] | None = None
        if has_targets:
            if not isinstance(raw_targets, list) or not raw_targets or len(raw_targets) > maximum_needed:
                raise BatchError(f"targets invalidos em post {post_id}")
            normalized_targets = []
            seen_targets: set[str] = set()
            featured_targets = 0
            for target_position, raw_target in enumerate(raw_targets):
                if not isinstance(raw_target, dict):
                    raise BatchError(f"targets[{target_position}] invalido em post {post_id}")
                target_id = str(raw_target.get("target_id") or "").strip()
                target_role = str(raw_target.get("role") or "").strip().lower()
                target_subject = str(raw_target.get("subject") or "").strip()
                target_query = str(raw_target.get("query") or target_subject).strip()
                if not target_id or target_id in seen_targets or len(target_id) > 200:
                    raise BatchError(f"target_id ausente/duplicado em post {post_id}")
                if target_role not in {"inline", "featured"} or not target_subject or not target_query:
                    raise BatchError(f"target[{target_position}] sem role/subject/query em post {post_id}")
                target_reserve = raw_target.get("replacement_reserve", 0)
                if (
                    isinstance(target_reserve, bool)
                    or not isinstance(target_reserve, int)
                    or not 0 <= target_reserve <= MEDIA_BATCH_REPLACEMENT_RESERVE
                ):
                    raise BatchError(f"replacement_reserve invalido em target {target_id}")
                target_row: dict[str, Any] = {
                    "target_id": target_id,
                    "role": target_role,
                    "subject": target_subject,
                    "query": target_query,
                    "replacement_reserve": target_reserve,
                }
                if target_role == "featured":
                    featured_targets += 1
                else:
                    paragraph_index = raw_target.get("paragraph_index")
                    if isinstance(paragraph_index, bool) or not isinstance(paragraph_index, int) or paragraph_index < 0:
                        raise BatchError(f"paragraph_index invalido em target {target_id}")
                    item_identity = str(raw_target.get("item_identity") or "").strip()
                    item_title = str(raw_target.get("item_title") or "").strip()
                    section_heading = str(raw_target.get("section_heading") or "").strip()
                    if not item_identity or not item_title or not section_heading:
                        raise BatchError(f"target inline {target_id} precisa identificar o item/H2")
                    placement = raw_target.get("placement")
                    if placement not in {None, "after_list_item_description"}:
                        raise BatchError(f"placement invalido em target {target_id}")
                    item_number = raw_target.get("item_number")
                    if item_number is not None and (
                        isinstance(item_number, bool) or not isinstance(item_number, int) or item_number < 1
                    ):
                        raise BatchError(f"item_number invalido em target {target_id}")
                    target_row.update({
                        "paragraph_index": paragraph_index,
                        "item_identity": item_identity,
                        "item_title": item_title,
                        "item_group": str(raw_target.get("item_group") or ""),
                        "item_number": item_number,
                        "section_heading": section_heading,
                        "section_slot": raw_target.get("section_slot", paragraph_index),
                        "html_position": raw_target.get("html_position"),
                        **({"placement": placement} if placement else {}),
                    })
                seen_targets.add(target_id)
                normalized_targets.append(target_row)
            if len(normalized_targets) != needed:
                raise BatchError(f"needed deve igualar a quantidade de targets em post {post_id}")
            if featured_targets != int(featured_required):
                raise BatchError(f"featured_required diverge dos targets em post {post_id}")
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
            "role": role,
            "featured_required": featured_required,
            "replacement_reserve": reserve,
            **image_counts,
            **({"targets": normalized_targets} if normalized_targets is not None else {}),
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
    return wordpress_text(post.get("title"), field="title")


def _post_content(post: dict[str, Any]) -> str:
    return wordpress_text(post.get("content"), field="content")


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
    source_language = detect_language(f"{title}\n{source_content}")
    needs_localization = localization_required(source_language)
    localization = {
        "required": needs_localization,
        "reason": "original_content_english" if needs_localization else "not_unequivocally_english",
    }
    meta = post.get("meta") if isinstance(post.get("meta"), dict) else {}
    retry_mode = "uncertain_second_pass" if uncertain_second_pass_eligible(post) else None
    relevance_policy = {
        "allowed_topics": [str(topic) for topic in (config.site_topics or ())],
    }
    previous_relevance_reason = (
        state.get("last_error") or "" if retry_mode else ""
    )
    media_policy = str(getattr(config, "media_source_policy", "strict"))
    requirements = {
        "internal_links_enabled": bool(config.internal_links_enabled),
        # A imagem direta e o crédito são o contrato persistido. A página-fonte
        # é obrigatória apenas no modo estrito; credit_only nunca a representa
        # como evidência de origem ou licença.
        "media_stage": {
            "stage": "media-resolve-batch",
            "runs_after": "editorial-generate-batch",
            "media_source_policy": media_policy,
            "required_inline_images": required_image_count(
                words, title=title, content=cleaned_html
            ),
            "featured_required": True,
            "source_page_required_for_new_media": media_policy != "credit_only",
            "credit_required": True,
            "credit_generation": (
                "hosting_domain_not_author" if media_policy == "credit_only"
                else "source_page_site_label"
            ),
            "editorial_media_plan": [],
            "not_a_reason_for_needs_retry": [
                "imagens",
                "featured",
                "origem",
            ],
        },
    }
    return {
        "schema_version": BATCH_SCHEMA_VERSION,
        "batch_id": batch_id,
        "post_id": post.get("id"),
        "status": post.get("status"),
        "state": state,
        "retry_mode": retry_mode,
        "previous_relevance_reason": previous_relevance_reason,
        "relevance_policy": relevance_policy,
        "title": title,
        "date": post.get("date"),
        "link": post.get("link"),
        "original_link": prepared.get("original_link") or meta.get("original_link"),
        "source_content": source_content,
        "source_language": source_language,
        "localization": localization,
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


_MODEL_FACING_KEYS = (
    "post_id",
    "status",
    "state",
    "retry_mode",
    "previous_relevance_reason",
    "relevance_policy",
    "title",
    "date",
    "link",
    "original_link",
    "source_content",
    "source_language",
    "localization",
    "cleaned_html",
    "seo_existing",
    "entities",
    "word_count",
    "requirements",
)


def _model_facing_context(envelope: dict[str, Any]) -> dict[str, Any]:
    """Project the audit envelope into the subset sent to the editorial model.

    The image facts (``images``, ``featured``) and the image counts inside
    ``requirements.media_stage`` are the business of the deterministic media
    stage, not of a tool-less editorial call. Sending them made the model answer
    ``needs_retry`` ("faltam imagens") for every post, so the batch never
    produced an editorial — observed in the production canary. The full envelope
    (with the image facts) remains in ``post-<id>.json`` for audit and for the
    media stage.
    """
    context = {key: envelope[key] for key in _MODEL_FACING_KEYS if key in envelope}
    requirements = envelope.get("requirements") or {}
    media_stage = requirements.get("media_stage") or {}
    context["requirements"] = {
        "internal_links_enabled": bool(requirements.get("internal_links_enabled")),
        # Deliberadamente SEM required_inline_images/featured_required: a
        # contagem de imagem e da etapa de midia e era o gatilho do needs_retry.
        "media_stage": {
            "runs_after": "editorial-generate-batch",
            "editorial_media_plan": [],
            "not_a_reason_for_needs_retry": list(
                media_stage.get("not_a_reason_for_needs_retry") or []
            ),
        },
    }
    return context


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
            from .pipeline_v2.quarantine import (
                PostQuarantinedError,
                QuarantinePolicyError,
                assert_post_not_quarantined,
            )

            assert_post_not_quarantined(root, post_id, operation="prepare_batch")
            with _acquire_post_lock(root, config, post_id) as lock_handle:
                post = client.get_post(post_id)
                prepared = prepare_post(
                    client, root, post_id, config=config, lock_handle=lock_handle
                )
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
                contexts.append(_model_facing_context(envelope))
                items.append(
                    {
                        "post_id": post_id,
                        "status": "prepared",
                        "title": envelope["title"],
                        "word_count": envelope["word_count"],
                        "required_inline_images": envelope["requirements"]["media_stage"][
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
        except (PostQuarantinedError, QuarantinePolicyError) as exc:
            items.append({
                "post_id": post_id,
                "status": "quarantined",
                "error": str(exc)[:240],
                "error_class": "quarantine",
            })
        except BatchError as exc:
            items.append({"post_id": post_id, "status": "error", "error": str(exc)[:240], "error_class": "contract"})
        except Exception as exc:  # noqa: BLE001 - isolate one post in a batch
            if is_post_lock_blocked(exc):
                items.append({
                    "post_id": post_id,
                    "status": "lock_blocked",
                    "error_code": "post_lock_blocked",
                    "error": str(exc)[:240],
                    "wordpress_changed": False,
                })
                continue
            from .observability import record_unexpected_error
            record_unexpected_error(root, exc, post_id=post_id, run_id=batch_id, file="batch.py", field="prepare_batch")
            items.append(
                {
                    "post_id": post_id,
                    "status": "error",
                    "error": str(exc)[:240],
                    "error_class": "internal",
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
        "lock_blocked": sum(1 for item in items if item.get("status") == "lock_blocked"),
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
        "lock_blocked": manifest["lock_blocked"],
        "failed": manifest["failed"],
        "items": items,
        "editorial_input": str(editorial_input),
    }


__all__ = [
    "BATCH_SCHEMA_VERSION",
    "BatchError",
    "wordpress_text",
    "batch_directory",
    "new_batch_id",
    "load_editorial_batch",
    "load_vision_batch",
    "load_media_resolve_batch",
    "prepare_batch",
    "validate_batch_id",
]
