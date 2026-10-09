"""Insert uploaded media only at safe block boundaries."""

from __future__ import annotations

import re
from collections.abc import Mapping
from html import escape
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
    paragraph_count = len(re.findall(r"</p>\s*", html, flags=re.IGNORECASE))
    # ``insert_media`` only accepts a boundary before the final paragraph;
    # the last valid index is therefore N-2, not N-1.
    last_slot = max(0, paragraph_count - 2)
    count = len(plan)
    if count == 0:
        return []
    capacity = 1 + (last_slot // minimum_spacing) if paragraph_count >= 2 else 0
    if capacity < count:
        raise MediaInsertionError(
            f"insufficient valid paragraph slots: {count} image(s) require "
            f"at least {minimum_spacing} paragraphs of spacing, but HTML has "
            f"{paragraph_count} paragraph(s)",
            code="insufficient_paragraph_slots",
        )
    if count == 1:
        slots = [last_slot // 2]
    else:
        span = last_slot
        minimum_span = minimum_spacing * (count - 1)
        slack = span - minimum_span
        slots = [
            index * minimum_spacing + round(slack * index / (count - 1))
            for index in range(count)
        ]
    planned: list[dict[str, Any]] = []
    for item, slot in zip(plan, slots):
        planned.append({**dict(item), "paragraph_index": int(slot)})
    return planned


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
