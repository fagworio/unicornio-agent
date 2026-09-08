"""Deterministic policy deciding when featured-image pixels need inspection."""

from __future__ import annotations

import re
import unicodedata
from typing import Any
from urllib.parse import urlparse


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


def vision_cache_subject(editorial: dict[str, Any]) -> str:
    """Stable work identity for cache reuse, falling back to the SEO title."""
    game_name = editorial.get("game_name")
    if isinstance(game_name, str) and game_name.strip():
        return game_name.strip()
    seo = editorial.get("seo") or {}
    return str(seo.get("title") or "").strip()


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
