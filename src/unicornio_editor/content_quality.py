"""Deterministic editorial-quality gates for human-readable content."""

from __future__ import annotations

import re
from collections.abc import Iterable
from html import unescape


class ContentQualityError(ValueError):
    """Raised when content fails a publication-quality gate."""


_AI_PATTERNS = (
    "em conclusão",
    "é importante destacar que",
    "vale ressaltar que",
    "neste artigo",
    "no mundo dos",
)


def word_count(html: str) -> int:
    text = unescape(re.sub(r"<[^>]+>", " ", html))
    return len(re.findall(r"\b[\wÀ-ÿ]+\b", text))


def minimum_image_count(words: int) -> int:
    if words <= 600:
        return 2
    if words <= 1000:
        return 4
    return 6


def validate_content_quality(
    html: str,
    *,
    title: str,
    focus_keyword: str,
    image_count: int,
    matched_topics: Iterable[str],
    allowed_topics: Iterable[str] = (),
    related_terms: Iterable[str] = (),
) -> dict[str, int | bool]:
    if not isinstance(html, str) or not html.strip():
        raise ContentQualityError("content must contain readable text")
    if not isinstance(title, str) or not title.strip():
        raise ContentQualityError("title is required")
    if not isinstance(focus_keyword, str) or not focus_keyword.strip():
        raise ContentQualityError("focus keyword is required")
    words = word_count(html)
    required_images = minimum_image_count(words)
    if image_count < required_images:
        raise ContentQualityError(
            f"content with {words} words requires at least {required_images} relevant images"
        )
    topics = {item.strip().casefold() for item in matched_topics if isinstance(item, str) and item.strip()}
    allowed = {item.strip().casefold() for item in allowed_topics if isinstance(item, str) and item.strip()}
    if not topics:
        raise ContentQualityError("content must have matched editorial topics")
    if allowed and not topics.intersection(allowed):
        raise ContentQualityError("content has no topic aligned with the site")
    body = re.sub(r"<[^>]+>", " ", html).casefold()
    title_folded = title.casefold()
    keyword = focus_keyword.casefold().strip()
    if keyword not in title_folded or keyword not in body:
        raise ContentQualityError("focus keyword must occur naturally in title and content")
    for term in related_terms:
        if isinstance(term, str) and term.strip() and term.casefold() not in body:
            raise ContentQualityError(f"semantic related term is absent: {term}")
    if re.search(r"[—–]", html):
        raise ContentQualityError("recurrent em/en dashes are not allowed in editorial copy")
    if any(pattern in body for pattern in _AI_PATTERNS):
        raise ContentQualityError("generic AI-like phrasing detected")
    if words > 600 and not re.search(r"<h[2-4]\b", html, re.IGNORECASE):
        raise ContentQualityError("long content requires descriptive subheadings")
    return {"words": words, "required_images": required_images, "image_count": image_count, "passed": True}


def validate_centered_images(html: str) -> None:
    images = re.findall(r"<figure\b[^>]*>.*?</figure>|<img\b[^>]*>", html, re.IGNORECASE | re.DOTALL)
    for image in images:
        if not re.search(r"aligncenter|text-align\s*:\s*center", image, re.IGNORECASE):
            raise ContentQualityError("every content image must be centered")
