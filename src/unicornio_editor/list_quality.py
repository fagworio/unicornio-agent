"""Validation rules for numbered lists, rankings, and recommendation articles."""

from __future__ import annotations

import re
from html.parser import HTMLParser


class ListContentError(ValueError):
    """Raised when a list/ranking article violates its structural contract."""


_LIST_TERMS = re.compile(
    r"\b(?:top|melhores|piores|recomenda(?:ções|cao)|seleção|selecao|lista|ranking|jogos|animes|filmes|séries|series|personagens|smartphones)\b",
    re.IGNORECASE,
)
_COUNT = re.compile(r"\b(\d{1,3})\b")
_NUMBERED_H2 = re.compile(r"^\s*(\d{1,3})\s*[.)-]\s+(.+?)\s*$")


class _BlockParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str, bool]] = []
        self._tag: str | None = None
        self._attrs: list[tuple[str, str | None]] = []
        self._text: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"h2", "p", "figure", "img", "article"} and self._depth == 0:
            self._tag = tag
            self._attrs = attrs
            self._text = []
            if tag == "img":
                self.blocks.append((tag, "", True))
            else:
                self._depth = 1
        elif self._tag is not None:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._tag is None:
            return
        if tag == self._tag and self._depth == 1:
            self.blocks.append((self._tag, " ".join("".join(self._text).split()), False))
            self._tag = None
            self._attrs = []
            self._text = []
            self._depth = 0
        elif self._depth:
            self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._tag is not None:
            self._text.append(data)


def detect_list_format(title: str, html: str = "") -> int | None:
    """Return the promised item count, or None when the article is not a list."""
    title_match = _COUNT.search(title or "")
    if not title_match:
        return None
    if _LIST_TERMS.search(title or "") or re.search(r"<h2[^>]*>\s*\d+\s*[.)-]", html or "", re.I):
        return int(title_match.group(1))
    return None


def validate_list_content(title: str, html: str) -> dict[str, int | bool]:
    """Validate exact count and H2 -> image -> description order."""
    promised = detect_list_format(title, html)
    if promised is None:
        return {"is_list": False, "items": 0, "passed": True}
    if re.search(r"<article\b", html, re.IGNORECASE):
        raise ListContentError("article tag must be removed from post content")
    parser = _BlockParser()
    parser.feed(html)
    blocks = parser.blocks
    # Relevance-first policy: a listicle may legitimately have zero images
    # (absence beats a wrong image), so the image-order contract is waived
    # when the content carries no image at all; numbering and count still
    # hold. The structural contract only applies to posts that HAVE images.
    has_any_image = any(tag in {"figure", "img"} for tag, _, _ in blocks)
    headings = [(i, value) for i, (tag, value, _) in enumerate(blocks) if tag == "h2"]
    if len(headings) != promised:
        raise ListContentError(f"title promises {promised} items but content has {len(headings)} H2 items")
    expected = list(range(promised, 0, -1)) if headings and _number(headings[0][1]) == promised else list(range(1, promised + 1))
    seen: list[int] = []
    for position, (index, heading) in enumerate(headings):
        match = _NUMBERED_H2.match(heading)
        if not match:
            raise ListContentError(f"item {position + 1} must have a numbered H2")
        number, name = int(match.group(1)), match.group(2).strip()
        if not name or len(name) < 3 or ":" not in name:
            raise ListContentError(f"item {number} H2 must identify the item and its description")
        seen.append(number)
        if not has_any_image:
            continue
        if index + 1 >= len(blocks) or blocks[index + 1][0] not in {"figure", "img"}:
            raise ListContentError(f"item {number} must have its image immediately after H2")
        if index + 2 >= len(blocks) or blocks[index + 2][0] != "p":
            raise ListContentError(f"item {number} description must follow the image")
    if seen != expected:
        raise ListContentError(f"invalid item numbering: expected {expected}, got {seen}")
    return {"is_list": True, "items": len(headings), "promised": promised, "passed": True}


def _number(heading: str) -> int | None:
    match = _NUMBERED_H2.match(heading)
    return int(match.group(1)) if match else None
