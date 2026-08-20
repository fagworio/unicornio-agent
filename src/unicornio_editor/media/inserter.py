"""Insert uploaded media only at safe block boundaries."""

from __future__ import annotations

import re
from collections.abc import Mapping
from html import escape
from typing import Any
from urllib.parse import urlparse


class MediaInsertionError(ValueError):
    """Raised when a media plan cannot be safely placed."""


def insert_media(html: str, plan: list[Mapping[str, Any]]) -> str:
    if not isinstance(html, str) or not isinstance(plan, list):
        raise MediaInsertionError("HTML and media plan have invalid types")
    if len(plan) > 4:
        raise MediaInsertionError("at most four inline images are allowed")
    paragraph_ends = [match.end() for match in re.finditer(r"</p>\s*", html, flags=re.IGNORECASE)]
    placements: list[tuple[int, str]] = []
    indexes: list[int] = []
    for item in plan:
        if not isinstance(item, Mapping):
            raise MediaInsertionError("each media placement must be an object")
        required = {"paragraph_index", "media_url", "alt_text", "credit_text"}
        if set(item) != required:
            raise MediaInsertionError("media placement has missing or unknown fields")
        index = item["paragraph_index"]
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise MediaInsertionError("paragraph_index must be non-negative")
        if index >= len(paragraph_ends) - 1:
            raise MediaInsertionError("media must be inserted between paragraphs")
        if index in indexes or any(abs(index - other) < 3 for other in indexes):
            raise MediaInsertionError("images must be at least three paragraphs apart")
        url = item["media_url"]
        if not isinstance(url, str) or url.lower().split("?", 1)[0].rsplit("/", 1)[-1].endswith(".webp") is False:
            raise MediaInsertionError("media_url must point to a WebP file")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise MediaInsertionError("media_url must be an absolute HTTP(S) URL")
        alt = _text(item["alt_text"], "alt_text")
        credit = _text(item["credit_text"], "credit_text")
        if not credit.startswith("Crédito da imagem:"):
            raise MediaInsertionError("credit_text must start with 'Crédito da imagem:'")
        figure = (
            f'<figure><img src="{escape(url, quote=True)}" alt="{escape(alt, quote=True)}" />'
            f"<figcaption>{escape(credit)}</figcaption></figure>"
        )
        placements.append((index, figure))
        indexes.append(index)
    for index, figure in sorted(placements, reverse=True):
        position = paragraph_ends[index]
        html = html[:position] + figure + html[position:]
    return html


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MediaInsertionError(f"{name} is required")
    return value.strip()
