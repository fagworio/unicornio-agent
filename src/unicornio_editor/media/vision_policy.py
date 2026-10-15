"""Deterministic policy deciding when featured-image pixels need inspection."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping
from urllib.parse import urlparse

VISION_POLICY_VERSION = "featured-editorial-v3-multidimensional"


def trusted_featured_evidence(
    *, image_url: str, source_page_url: str, subject: str, search_query: str
) -> str | None:
    """Return a reason to skip paid vision only for strongly evidenced sources.

    A filename alone is never enough: CDNs can serve a wrong image under a
    plausible slug. We skip pixels only when the image host/source-page pair
    is known and the official page path plus the actual discovery query both
    carry an identifying anchor from the featured subject. All other sources
    remain ambiguous and keep the fail-closed vision check.
    """
    image = urlparse(image_url)
    page = urlparse(source_page_url)
    image_host = (image.hostname or "").lower()
    page_host = (page.hostname or "").lower().removeprefix("www.")
    page_path = page.path or ""
    trusted_pair = (
        (image_host == "image.tmdb.org" and page_host == "anime.com" and page_path.startswith("/shows/"))
        or (
            image_host == "images.justwatch.com"
            and page_host == "justwatch.com"
            and page_path.startswith(("/us/tv-show/", "/us/movie/"))
        )
    )
    if not trusted_pair:
        return None
    if not _has_subject_anchor(subject, page_path) or not _has_subject_anchor(subject, search_query):
        return None
    return f"fonte confiavel {page_host} com obra identificada na pagina e na busca"


_VISION_CATEGORIES = frozenset(
    {"game", "anime", "movie", "series", "person", "general_entertainment"}
)
_GENERIC_FOCUS = frozenset({"game", "games", "jogo", "jogos", "videogame", "anime", "filme", "series"})


def featured_vision_subject(editorial: Mapping[str, Any]) -> str:
    """Return the one stable editorial identity used by every featured gate.

    The resolver is deliberately deterministic so discovery, cache keys and
    final checklist use the same identity.
    """
    return str(resolve_editorial_identity(editorial)["primary_subject"])


def resolve_editorial_identity(editorial: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve a shared primary subject and preserve secondary context.

    Explicit game identity is strongest. Otherwise a concrete focus keyword is
    preferred over grammatical entities extracted from the headline, which
    can select a ministry or company mentioned in a story about a platform.
    Other extracted entities remain context, never a replacement primary.
    """
    game_name = editorial.get("game_name")
    seo = editorial.get("seo") if isinstance(editorial.get("seo"), Mapping) else {}
    focus = str(seo.get("focus_keyword") or editorial.get("focus_keyword") or "").strip()
    title = str(editorial.get("title") or seo.get("title") or "").strip()
    content = str(editorial.get("cleaned_html") or "")
    primary = str(game_name or "").strip() if isinstance(game_name, str) else ""
    if not primary and focus and focus.casefold() not in _GENERIC_FOCUS:
        from .evidence import normalize

        focus_key = normalize(focus).strip()
        article_text = normalize(f"{title} {content}")
        has_structured_subject = any(
            editorial.get(key)
            for key in ("post_subjects", "subjects", "main_entity", "entity")
        )
        if focus_key and focus_key in article_text:
            primary = focus.casefold() if content.strip() else focus
        elif focus_key and not has_structured_subject:
            primary = focus
    for key in ("post_subjects", "subjects"):
        if primary:
            break
        values = editorial.get(key)
        if isinstance(values, str) and values.strip():
            primary = values.strip()
        if isinstance(values, list):
            for value in values:
                candidate = value.get("subject") if isinstance(value, dict) else value
                if isinstance(candidate, str) and candidate.strip():
                    primary = candidate.strip()
                    break
    if not primary:
        for key in ("main_entity", "entity"):
            candidate = editorial.get(key)
            if isinstance(candidate, str) and candidate.strip():
                primary = candidate.strip()
                break
    # Editorial schema deliberately keeps post subjects derived rather than
    # accepting model-supplied top-level identity fields.  Use the same
    # deterministic extractor as the media evidence gate.
    try:
        from .evidence import post_subjects
        derived = post_subjects(
            title=title,
            content_html=content,
            focus_keyword=focus,
            game_name=game_name if isinstance(game_name, str) else None,
        )
        if not primary and derived and str(derived[0].get("subject") or "").strip():
            primary = str(derived[0]["subject"]).strip()
    except Exception:  # noqa: BLE001 - fallback remains deterministic
        pass
    if not primary:
        primary = title

    secondary: list[str] = []
    try:
        from .evidence import editorial_subjects

        for subject in editorial_subjects(
            title=title,
            content_html=content,
            focus_keyword=focus,
            game_name=game_name if isinstance(game_name, str) else None,
        ):
            value = str(subject or "").strip()
            if value and value.casefold() != primary.casefold() and value.casefold() not in {
                item.casefold() for item in secondary
            }:
                secondary.append(value)
    except Exception:  # noqa: BLE001 - identity remains usable without context
        pass
    category = featured_vision_category(editorial)
    return {
        "primary_subject": primary,
        "subject_type": category,
        "secondary_entities": secondary,
        "news_context": title,
        "vision_category": category,
    }


def vision_cache_subject(editorial: Mapping[str, Any]) -> str:
    """Compatibility name for the stable featured identity function."""
    return featured_vision_subject(editorial)


def featured_vision_category(editorial: Mapping[str, Any]) -> str:
    """Map editorial/post type to a safe vision category."""
    raw = " ".join(
        str(editorial.get(key) or "")
        for key in ("editorial_type", "post_type", "content_type", "category")
    ).casefold()
    if any(token in raw for token in ("game", "jogo", "videogame")):
        return "game"
    for category in ("anime", "movie", "series", "person"):
        if category in raw or (category == "series" and "tv" in raw):
            return category
    return "general_entertainment"


def _has_subject_anchor(subject: str, evidence: str) -> bool:
    words = _words(subject)
    haystack = "-".join(_words(evidence))
    if not words or not haystack:
        return False
    # Prefer a two-word work title ("blue-box", "psyren"), then accept a
    # distinctive long single token for one-word works.
    for left, right in zip(words, words[1:]):
        if len(left) >= 3 and len(right) >= 3 and f"{left}-{right}" in haystack:
            return True
    return any(len(word) >= 5 and word in haystack for word in words)


def _words(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode().lower()
    ignored = {"anime", "temporada", "season", "parte", "part", "the", "de", "do", "da"}
    return [word for word in re.findall(r"[a-z0-9]+", normalized) if word not in ignored]
