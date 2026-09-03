"""Deterministic editorial-quality gates for human-readable content."""

from __future__ import annotations

import re
from collections.abc import Iterable
from html import unescape


class ContentQualityError(ValueError):
    """Raised when content fails a publication-quality gate."""


# Portuguese stopwords that carry no keyword signal; a focus keyword may
# contain them (e.g. "remakes de animes clássicos") without them needing to
# appear verbatim in the copy.
_PT_STOPWORDS = frozenset(
    "de e o a os as do da dos das em para com que no na num numa por ao aos à às um uma".split()
)


def _keyword_in_text(keyword: str, text: str) -> bool:
    """Keyword relevance: every significant token of the keyword must occur
    in the text, in order (gaps allowed).

    A strict substring check is too brittle for Portuguese SEO keywords:
    "bass x machina netflix" never matches "Bass x Machina: Netflix ..." due
    to punctuation, while "vazamentos gta 6" never matches "vazamentos de
    GTA 6" due to the stopword. The tokens are the signal, and they must
    still appear in the same relative order in the target text.
    """
    tokens = re.findall(r"[\wÀ-ÿ]+", keyword.casefold())
    significant = [t for t in tokens if t not in _PT_STOPWORDS]
    if not significant:
        return keyword.casefold() in text.casefold()
    haystack = iter(re.findall(r"[\wÀ-ÿ]+", text.casefold()))
    return all(any(token == candidate for candidate in haystack) for token in significant)


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
    # Quantidade de imagens pertence exclusivamente ao gate
    # ``imagens_no_corpo``. Mantê-la aqui também produzia duas falhas para a
    # mesma causa e tornava ``qualidade_texto`` impossível de interpretar na
    # telemetria.
    topics = {item.strip().casefold() for item in matched_topics if isinstance(item, str) and item.strip()}
    allowed = {item.strip().casefold() for item in allowed_topics if isinstance(item, str) and item.strip()}
    if not topics:
        raise ContentQualityError("content must have matched editorial topics")
    if allowed and not topics.intersection(allowed):
        raise ContentQualityError("content has no topic aligned with the site")
    body = re.sub(r"<[^>]+>", " ", html).casefold()
    title_folded = title.casefold()
    keyword = focus_keyword.casefold().strip()
    if not _keyword_in_text(keyword, title_folded) or not _keyword_in_text(keyword, body):
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
    return {"words": words, "passed": True}


def validate_centered_images(html: str) -> None:
    images = re.findall(r"<figure\b[^>]*>.*?</figure>|<img\b[^>]*>", html, re.IGNORECASE | re.DOTALL)
    for image in images:
        if not re.search(r"aligncenter|text-align\s*:\s*center", image, re.IGNORECASE):
            raise ContentQualityError("every content image must be centered")
