"""Strict validation for the JSON produced by the editorial model."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse


class EditorialValidationError(ValueError):
    """Raised when an editorial result cannot be safely applied."""


_TOP_LEVEL = {
    "site_relevance",
    "cleaned_html",
    "seo",
    "media_plan",
    "needs_trailer",
    "trailer_url",
    "game_name",
}
_RELEVANCE = {"decision", "confidence", "reason", "matched_topics"}
_SEO = {"title", "meta_description", "focus_keyword"}
_MEDIA = {
    "paragraph_index",
    "source_page_url",
    "direct_image_url",
    "author",
    "license",
    "license_url",
    "captured_at",
    "credit_text",
    "alt_text",
    "is_featured",
}


def validate_editorial(payload: Mapping[str, Any], *, min_confidence: float = 0.8) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise EditorialValidationError("editorial result must be an object")
    _keys(payload, _TOP_LEVEL, "top-level", optional={"cleaned_html", "seo"})
    relevance = _object(payload["site_relevance"], "site_relevance")
    _keys(relevance, _RELEVANCE, "site_relevance")
    decision = relevance["decision"]
    if decision not in {"process", "skip"}:
        raise EditorialValidationError("site_relevance.decision must be process or skip")
    confidence = _number(relevance["confidence"], "site_relevance.confidence")
    if not 0 <= confidence <= 1:
        raise EditorialValidationError("site_relevance.confidence must be between 0 and 1")
    if decision == "process" and confidence < min_confidence:
        raise EditorialValidationError("process decision is below the confidence threshold")
    _nonempty_string(relevance["reason"], "site_relevance.reason")
    if not isinstance(relevance["matched_topics"], list) or not all(
        isinstance(item, str) and item.strip() for item in relevance["matched_topics"]
    ):
        raise EditorialValidationError("site_relevance.matched_topics must be a list of strings")

    # cleaned_html is OPTIONAL: when absent or null the workflow reuses the
    # deterministic cleaned content of the prepared post (no-rewrite path).
    # This is the token-economy default: the model must not re-emit text it
    # did not change. CTA/Fonte/rodape are always code-inserted (builder).
    cleaned_html = payload.get("cleaned_html")
    if cleaned_html is not None and not isinstance(cleaned_html, str):
        raise EditorialValidationError("cleaned_html must be a string or null")
    if decision == "process" and isinstance(cleaned_html, str) and not cleaned_html.strip():
        raise EditorialValidationError("cleaned_html cannot be empty when processing")

    # seo is OPTIONAL too: when absent the workflow inherits a valid existing
    # Rank Math meta (token economy — the model must not re-emit SEO the post
    # already has). When the post has no valid meta, apply fails with a clear
    # message telling the model to provide seo.
    if "seo" in payload and payload["seo"] is not None:
        seo = _object(payload["seo"], "seo")
        _keys(seo, _SEO, "seo")
        title = _nonempty_string(seo["title"], "seo.title")
        if len(title) > 65:
            raise EditorialValidationError("seo.title must contain at most 65 characters")
        description = _nonempty_string(seo["meta_description"], "seo.meta_description")
        if not 120 <= len(description) <= 160:
            raise EditorialValidationError("seo.meta_description must contain 120 to 160 characters")
        _nonempty_string(seo["focus_keyword"], "seo.focus_keyword")

    media_plan = payload["media_plan"]
    if not isinstance(media_plan, list) or len(media_plan) > 12:
        raise EditorialValidationError("media_plan must contain between 0 and 12 items")
    normalized_media = []
    featured_count = 0
    for index, item in enumerate(media_plan):
        media = _object(item, f"media_plan[{index}]")
        _keys(media, _MEDIA, f"media_plan[{index}]")
        paragraph_index = media["paragraph_index"]
        if isinstance(paragraph_index, bool) or not isinstance(paragraph_index, int) or paragraph_index < 0:
            raise EditorialValidationError(f"media_plan[{index}].paragraph_index must be non-negative")
        for name in ("author", "license", "captured_at", "credit_text", "alt_text"):
            _nonempty_string(media[name], f"media_plan[{index}].{name}")
        for name in ("source_page_url", "direct_image_url", "license_url"):
            _http_url(media[name], f"media_plan[{index}].{name}")
        is_featured = media["is_featured"]
        if not isinstance(is_featured, bool):
            raise EditorialValidationError(f"media_plan[{index}].is_featured must be boolean")
        if is_featured:
            featured_count += 1
        normalized_media.append(dict(media))
    if featured_count > 1:
        raise EditorialValidationError("media_plan must contain at most one featured image")

    needs_trailer = payload["needs_trailer"]
    if not isinstance(needs_trailer, bool):
        raise EditorialValidationError("needs_trailer must be boolean")
    trailer_url = payload["trailer_url"]
    if needs_trailer:
        _http_url(trailer_url, "trailer_url")
    elif trailer_url is not None:
        raise EditorialValidationError("trailer_url must be null when needs_trailer is false")

    game_name = payload["game_name"]
    if game_name is not None and (not isinstance(game_name, str) or not game_name.strip()):
        raise EditorialValidationError("game_name must be null or a non-empty string")

    result = dict(payload)
    result["media_plan"] = normalized_media
    return result


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EditorialValidationError(f"{name} must be an object")
    return value


def _keys(value: Mapping[str, Any], expected: set[str], name: str, optional: set[str] | None = None) -> None:
    actual = set(value)
    required = expected - (optional or set())
    missing = required - actual
    unknown = actual - expected
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing={sorted(missing)}")
        if unknown:
            details.append(f"unknown={sorted(unknown)}")
        raise EditorialValidationError(f"{name} has invalid fields ({', '.join(details)})")


def _nonempty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EditorialValidationError(f"{name} must be a non-empty string")
    return value


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EditorialValidationError(f"{name} must be a number")
    return float(value)


def _http_url(value: Any, name: str) -> str:
    _nonempty_string(value, name)
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise EditorialValidationError(f"{name} must be an absolute HTTP(S) URL")
    return value
