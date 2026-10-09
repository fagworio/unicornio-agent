"""Insert uploaded media only at safe block boundaries."""

from __future__ import annotations

import re
from collections.abc import Mapping
from html import escape
from html import unescape
from typing import Any
from urllib.parse import urlparse


class MediaInsertionError(ValueError):
    """Raised when a media plan cannot be safely placed."""

    def __init__(self, message: str, *, code: str = "media_insertion_invalid") -> None:
        super().__init__(message)
        self.code = code


def append_featured_credit(html: str, credit_text: str) -> str:
    """Add one visible featured-image credit without duplicating it.

    O credito e sanitizado para TEXTO PURO (nunca HTML) e so e adicionado se
    ainda nao aparecer no conteudo (dedup por texto exato) — evita o caption
    duplicado que aparecia no post de producao.
    """
    if not isinstance(html, str):
        raise MediaInsertionError("HTML must be a string")
    from .text import plain_text

    credit = plain_text(credit_text)
    if not credit:
        return html
    if not credit.startswith("Crédito da imagem:"):
        raise MediaInsertionError("credit_text must start with 'Crédito da imagem:'")
    if credit in html:
        return html  # ja presente (dedup)
    match = re.search(r"</p>\s*", html, flags=re.IGNORECASE)
    figure = f'<p class="image-credit">{escape(credit)}</p>'
    if not match:
        return f"{figure}{html}"
    return html[: match.end()] + figure + html[match.end() :]


def remove_media_urls(html: str, urls: list[str] | tuple[str, ...] | set[str]) -> str:
    """Remove only figures/images whose exact source URL was invalidated.

    Used by the V2 visual-reconciliation journal before a pending post is
    recomposed.  It never guesses by slot, ALT or filename; unrelated images
    and editorial text remain intact.
    """
    if not isinstance(html, str):
        raise MediaInsertionError("HTML must be a string")
    targets = {str(url).strip() for url in urls if str(url).strip()}
    if not targets:
        return html

    def contains_target(fragment: str) -> bool:
        return any(re.search(r"\bsrc\s*=\s*(['\"])" + re.escape(url) + r"\1", fragment, re.IGNORECASE) for url in targets)

    output = re.sub(
        r"<figure\b[^>]*>[\s\S]*?</figure>\s*",
        lambda match: "" if contains_target(match.group(0)) else match.group(0),
        html,
        flags=re.IGNORECASE,
    )
    output = re.sub(
        r"<img\b[^>]*>\s*",
        lambda match: "" if contains_target(match.group(0)) else match.group(0),
        output,
        flags=re.IGNORECASE,
    )
    return output


def plan_normal_media_insertions(
    html: str,
    plan: list[Mapping[str, Any]],
    *,
    minimum_spacing: int = 3,
) -> list[dict[str, Any]]:
    """Assign safe, deterministic paragraph slots for a normal article.

    Slots from a previous attempt are deliberately ignored.  The current HTML
    is the source of truth, and the returned positions are distributed across
    the available paragraph boundaries while preserving the established
    spacing policy.
    """
    if not isinstance(html, str) or not isinstance(plan, list):
        raise MediaInsertionError("HTML and media plan have invalid types")
    if minimum_spacing < 1:
        raise MediaInsertionError("minimum_spacing must be positive")
    paragraphs = re.findall(r"<p\b[^>]*>(.*?)</p>\s*", html, flags=re.IGNORECASE | re.DOTALL)
    paragraph_count = len(paragraphs)
    # ``insert_media`` only accepts a boundary before the final paragraph;
    # the last valid index is therefore N-2, not N-1.
    last_slot = max(0, paragraph_count - 2)
    count = len(plan)
    if count == 0:
        return []
    # The lead needs two substantive paragraphs before the first inline image.
    # A tag-only or one-line spacer is not editorial lead copy and cannot earn
    # that placement. Slots are zero-based paragraph boundaries.
    substantive = [
        index for index, paragraph in enumerate(paragraphs)
        if _plain_word_count(paragraph) >= 12
    ]
    if len(substantive) < 2:
        raise MediaInsertionError(
            "at least two substantive paragraphs are required before the first inline image",
            code="insufficient_substantive_paragraphs",
        )
    first_slot = substantive[1]
    capacity = 1 + ((last_slot - first_slot) // minimum_spacing) if last_slot >= first_slot else 0
    if capacity < count:
        raise MediaInsertionError(
            f"insufficient valid paragraph slots: {count} image(s) require "
            f"at least {minimum_spacing} paragraphs of spacing, but HTML has "
            f"{paragraph_count} paragraph(s)",
            code="insufficient_paragraph_slots",
        )
    if count == 1:
        slots = [max(first_slot, last_slot // 2)]
    else:
        span = last_slot - first_slot
        minimum_span = minimum_spacing * (count - 1)
        slack = span - minimum_span
        slots = [
            first_slot + index * minimum_spacing + round(slack * index / (count - 1))
            for index in range(count)
        ]
    planned: list[dict[str, Any]] = []
    for item, slot in zip(plan, slots):
        planned.append({**dict(item), "paragraph_index": int(slot)})
    return planned


def _safe_sentence_boundaries(body: str) -> list[int]:
    """Return raw HTML offsets that are outside inline tags and shortcodes."""
    if "[" in body or "]" in body:
        return []
    boundaries: list[int] = []
    depth = 0
    token_re = re.compile(r"<!--[\s\S]*?-->|<[^>]+>|[^<]+")
    for match in token_re.finditer(body):
        token = match.group(0)
        if token.startswith("<!--"):
            continue
        if token.startswith("<"):
            tag_match = re.match(r"<\s*(/?)\s*([A-Za-z][\w:-]*)", token)
            if not tag_match:
                continue
            closing, tag = tag_match.groups()
            tag = tag.casefold()
            if tag in {"br", "hr", "img", "input", "source", "wbr"} or token.rstrip().endswith("/>"):
                continue
            depth = max(0, depth - 1) if closing else depth + 1
            continue
        if depth:
            continue
        for punctuation in re.finditer(r"[.!?](?=\s+|$)", token):
            end = match.start() + punctuation.end()
            before = body[max(0, end - 3):end]
            if before.count(".") >= 2 and len(before.replace(".", "")) <= 2:
                continue
            boundaries.append(end)
    return boundaries


def _plain_word_count(value: str) -> int:
    text = re.sub(r"<[^>]+>", " ", unescape(value or ""))
    return len(re.findall(r"[A-Za-zÀ-ÿ0-9]+(?:['’][A-Za-zÀ-ÿ0-9]+)?", text))


def _split_paragraph_once(open_tag: str, body: str) -> tuple[str, int] | None:
    """Split one paragraph only when both resulting pieces remain substantial."""
    boundaries = _safe_sentence_boundaries(body)
    if not boundaries or _plain_word_count(body) < 36:
        return None
    total = _plain_word_count(body)
    suitable = [
        boundary for boundary in boundaries
        if _plain_word_count(body[:boundary]) >= 18
        and _plain_word_count(body[boundary:]) >= 18
    ]
    if not suitable:
        return None
    target = total // 2
    boundary = min(suitable, key=lambda item: abs(_plain_word_count(body[:item]) - target))
    first = body[:boundary].rstrip()
    second = body[boundary:].lstrip()
    return f"{open_tag}{first}</p>{open_tag}{second}</p>", boundary


def normalize_normal_article_paragraphs(
    html: str,
    *,
    required_paragraphs: int,
    max_words: int = 1000,
) -> dict[str, Any]:
    """Safely increase paragraph capacity without changing editorial text."""
    if not isinstance(html, str) or required_paragraphs < 1:
        raise MediaInsertionError("HTML and required_paragraphs have invalid types")
    paragraph_re = re.compile(r"(?P<open><p\b[^>]*>)(?P<body>[\s\S]*?)</p>", re.IGNORECASE)
    matches = list(paragraph_re.finditer(html))
    original_words = _plain_word_count(html)
    audit: dict[str, Any] = {
        "original_paragraphs": len(matches),
        "required_paragraphs": required_paragraphs,
        "original_words": original_words,
        "final_words": original_words,
        "changed": False,
        "reason": None,
        "splits": [],
    }
    if original_words > max_words:
        audit["reason"] = "editorial_restructure_required: word_count_limit"
        audit["final_paragraphs"] = len(matches)
        return {"html": html, "audit": audit}
    if len(matches) >= required_paragraphs:
        audit["final_paragraphs"] = len(matches)
        return {"html": html, "audit": audit}

    needed = required_paragraphs - len(matches)
    replacements: dict[int, str] = {}
    candidates: list[tuple[int, int, str, str]] = []
    for index, match in enumerate(matches):
        body = match.group("body")
        if _plain_word_count(body) < 36 or "<h" in body.casefold() or "[" in body or "]" in body:
            continue
        if _safe_sentence_boundaries(body):
            candidates.append((index, _plain_word_count(body), match.group("open"), body))
    candidates.sort(key=lambda item: (-item[1], item[0]))
    for index, _words, open_tag, body in candidates[:needed]:
        split = _split_paragraph_once(open_tag, body)
        if split is None:
            continue
        replacement, boundary = split
        replacements[index] = replacement
        audit["splits"].append({
            "original_paragraph_index": index,
            "boundary_offset": boundary,
            "original_words": _plain_word_count(body),
            "result_words": [_plain_word_count(body[:boundary]), _plain_word_count(body[boundary:])],
        })

    pieces: list[str] = []
    cursor = 0
    for index, match in enumerate(matches):
        pieces.append(html[cursor:match.start()])
        pieces.append(replacements.get(index, match.group(0)))
        cursor = match.end()
    pieces.append(html[cursor:])
    normalized = "".join(pieces)
    final_matches = list(paragraph_re.finditer(normalized))
    audit["final_paragraphs"] = len(final_matches)
    audit["final_words"] = _plain_word_count(normalized)
    audit["changed"] = normalized != html
    source_text = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip()
    result_text = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", normalized))).strip()
    if source_text != result_text or audit["final_words"] != original_words:
        raise MediaInsertionError("structural normalization changed editorial text", code="text_preservation_failed")
    if audit["final_paragraphs"] < required_paragraphs:
        audit["reason"] = "editorial_restructure_required: no_safe_sentence_boundary"
    return {"html": normalized, "audit": audit}


def insert_media(html: str, plan: list[Mapping[str, Any]], *, listicle: bool = False) -> str:
    """Insert uploaded figures at safe block boundaries.

    Normal articles: figures go after the paragraph at ``paragraph_index``,
    kept at least three paragraphs apart. Listicles (numbered H2 items):
    each figure goes immediately after the numbered H2 preceding the
    targeted paragraph, as required by ``validate_list_content``.
    """
    if not isinstance(html, str) or not isinstance(plan, list):
        raise MediaInsertionError("HTML and media plan have invalid types")
    if len(plan) > 12:
        raise MediaInsertionError("at most twelve images are allowed")
    paragraph_ends = [match.end() for match in re.finditer(r"</p>\s*", html, flags=re.IGNORECASE)]
    placements: list[tuple[int, str]] = []
    indexes: list[int] = []
    for item in plan:
        if not isinstance(item, Mapping):
            raise MediaInsertionError("each media placement must be an object")
        required = {"paragraph_index", "media_url", "alt_text", "credit_text", "width", "height"}
        if set(item) != required:
            raise MediaInsertionError("media placement has missing or unknown fields")
        index = item["paragraph_index"]
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise MediaInsertionError("paragraph_index must be non-negative")
        if index >= len(paragraph_ends) - (1 if not listicle else 0):
            raise MediaInsertionError("media must be inserted between paragraphs")
        if not listicle:
            if index in indexes or any(abs(index - other) < 3 for other in indexes):
                raise MediaInsertionError("images must be at least three paragraphs apart")
        url = item["media_url"]
        if not isinstance(url, str) or url.lower().split("?", 1)[0].rsplit("/", 1)[-1].endswith(".webp") is False:
            raise MediaInsertionError("media_url must point to a WebP file")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise MediaInsertionError("media_url must be an absolute HTTP(S) URL")
        width = item["width"]
        height = item["height"]
        if (
            isinstance(width, bool)
            or not isinstance(width, int)
            or isinstance(height, bool)
            or not isinstance(height, int)
            or width <= 0
            or height <= 0
        ):
            raise MediaInsertionError("width and height must be positive integers")
        from .text import plain_text

        alt = plain_text(item["alt_text"])
        credit = plain_text(item["credit_text"])
        if not credit:
            raise MediaInsertionError("credit_text is required")
        if not credit.startswith("Crédito da imagem:"):
            raise MediaInsertionError("credit_text must start with 'Crédito da imagem:'")
        # SEO determinístico (sem IA): title = alt (texto descritivo da obra),
        # garantindo que o <img> nunca fique sem title.
        img_title = alt or credit
        # Padrao NATIVO do WordPress: shortcode [caption ...]...[/caption].
        # O WP renderiza com estilo proprio (aligncenter, legenda) e mantem o
        # layout da galeria; nao usar <figure> manual para nao quebrar o tema.
        img = (
            f'<img src="{escape(url, quote=True)}" '
            f'width="{width}" height="{height}" alt="{escape(alt, quote=True)}" '
            f'title="{escape(img_title, quote=True)}" />'
        )
        figure = (
            f'[caption id="" align="aligncenter" width="{width}"]'
            f"{img} {escape(credit)}[/caption]"
        )
        placements.append((index, figure))
        indexes.append(index)
    if listicle:
        for index, figure in sorted(placements, reverse=True):
            start = paragraph_ends[index - 1] if index > 0 else 0
            end = paragraph_ends[index]
            heading = re.search(r"<h2[^>]*>.*?</h2>", html[start:end], re.IGNORECASE | re.DOTALL)
            if not heading:
                raise MediaInsertionError(
                    f"list item at paragraph {index} has no numbered H2 before it"
                )
            position = start + heading.end()
            html = html[:position] + figure + html[position:]
        return html
    for index, figure in sorted(placements, reverse=True):
        position = paragraph_ends[index]
        html = html[:position] + figure + html[position:]
    return html


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MediaInsertionError(f"{name} is required")
    return value.strip()
