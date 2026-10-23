"""Insert uploaded media only at safe block boundaries."""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Mapping
from html import escape
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse


class MediaInsertionError(ValueError):
    """Raised when a media plan cannot be safely placed."""

    def __init__(self, message: str, *, code: str = "media_insertion_invalid") -> None:
        super().__init__(message)
        self.code = code


_EMPTY_PARAGRAPH_RE = re.compile(r"(?P<open><p\b[^>]*>)(?P<body>[\s\S]*?)</p>", re.IGNORECASE)
_EMPTY_PARAGRAPH_BODY_RE = re.compile(
    r"(?:(?:\s|&nbsp;|&#0*160;|&#x0*a0;)|<br\s*/?>)*",
    re.IGNORECASE,
)


class _BalancedMediaHtmlParser(HTMLParser):
    """Conservative structural check used before claiming verified capacity."""

    _VOID = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.valid = True

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.casefold()
        if normalized not in self._VOID:
            if normalized == "p" and "p" in self.stack:
                self.valid = False
            self.stack.append(normalized)

    def handle_startendtag(self, _tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        return

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        if normalized in self._VOID:
            self.valid = False
            return
        if not self.stack or self.stack[-1] != normalized:
            self.valid = False
            return
        self.stack.pop()


def _media_html_is_structurally_balanced(html: str) -> bool:
    parser = _BalancedMediaHtmlParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return False
    return parser.valid and not parser.stack


def remove_empty_paragraphs(html: str) -> dict[str, Any]:
    """Remove blank paragraphs, preserving simple id/name anchors as anchors.

    Only id/name attributes are transferable to an empty ``<a>`` target.
    Paragraphs with any other attributes remain untouched. Images, links,
    comments, shortcodes, and all non-empty text are preserved.
    """
    if not isinstance(html, str):
        raise MediaInsertionError("HTML must be a string")
    removed = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal removed
        if not _EMPTY_PARAGRAPH_BODY_RE.fullmatch(match.group("body")):
            return match.group(0)
        attributes = match.group("open")[2:-1]
        parsed: list[tuple[str, str]] = []
        cursor = 0
        attribute_re = re.compile(
            r"\s+([A-Za-z_:][A-Za-z0-9_.:-]*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))"
        )
        for attribute in attribute_re.finditer(attributes):
            if attributes[cursor:attribute.start()].strip():
                return match.group(0)
            name = attribute.group(1).casefold()
            value = next(
                (part for part in attribute.groups()[1:] if part is not None),
                "",
            )
            if name not in {"id", "name"} or any(old_name == name for old_name, _ in parsed):
                return match.group(0)
            parsed.append((name, value))
            cursor = attribute.end()
        if attributes[cursor:].strip():
            return match.group(0)
        if not parsed:
            removed += 1
            return ""
        # Preserve an anchor target but remove the empty block's layout. Only
        # id/name attributes are transferred; arbitrary paragraph attributes
        # are never copied onto a different element.
        anchor_attributes = " ".join(
            f'{name}="{escape(unescape(value), quote=True)}"'
            for name, value in parsed
        )
        removed += 1
        return f"<a {anchor_attributes}></a>"

    cleaned = _EMPTY_PARAGRAPH_RE.sub(replace, html)
    return {
        "html": cleaned,
        "audit": {"removed": removed, "changed": cleaned != html},
    }


def minimum_normal_article_paragraphs(
    image_count: int,
    *,
    minimum_spacing: int = 3,
    allow_after_body: bool = True,
) -> int:
    """Paragraph target for media slots including the explicit body-end slot.

    Normal articles reserve the first image until after two substantive lead
    paragraphs. Each further image needs ``minimum_spacing`` more paragraphs.
    The final image may follow the last paragraph when that paragraph ends the
    editorial body; generated trailer/footer blocks are appended afterwards.
    When the input has meaningful tail markup after its last paragraph, callers
    must set ``allow_after_body=False`` and retain the prior internal boundary.
    """
    if isinstance(image_count, bool) or image_count < 0:
        raise MediaInsertionError("image_count must be non-negative")
    if isinstance(minimum_spacing, bool) or minimum_spacing < 1:
        raise MediaInsertionError("minimum_spacing must be positive")
    if image_count == 0:
        return 0
    return (2 if allow_after_body else 3) + minimum_spacing * (image_count - 1)


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
    """Remove only images belonging to an invalidated attachment.

    Used by the V2 visual-reconciliation journal before a pending post is
    recomposed.  It never guesses by slot, ALT or filename; unrelated images
    and editorial text remain intact.
    """
    if not isinstance(html, str):
        raise MediaInsertionError("HTML must be a string")
    def url_key(value: str) -> str:
        parsed = urlparse(unescape(str(value or "").strip()))
        if not parsed.scheme or not parsed.netloc:
            return str(value or "").strip().split("?", 1)[0]
        path = re.sub(r"-\d+x\d+(?=\.[^./]+$)", "", parsed.path)
        return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}{path.rstrip('/') or '/'}"

    targets = {url_key(str(url)) for url in urls if str(url).strip()}
    if not targets:
        return html

    def remove_invalid_image_sources(match: re.Match[str]) -> str:
        fragment = match.group(0)
        src = re.search(r"\bsrc\s*=\s*(['\"])(.*?)\1", fragment, re.IGNORECASE | re.DOTALL)
        if src and url_key(src.group(2)) in targets:
            return ""

        def clean_srcset(attr: re.Match[str]) -> str:
            quote, value = attr.group(2), attr.group(3)
            kept = []
            for part in value.split(","):
                entry = part.strip()
                if not entry:
                    continue
                candidate = entry.split()[0]
                if url_key(candidate) not in targets:
                    kept.append(entry)
            return f"srcset={quote}{', '.join(kept)}{quote}" if kept else ""

        fragment = re.sub(
            r"\b(srcset)\s*=\s*(['\"])(.*?)\2",
            clean_srcset,
            fragment,
            flags=re.IGNORECASE | re.DOTALL,
        )
        for name in ("data-src", "data-lazy-src"):
            attr = re.search(r"\b" + name + r"\s*=\s*(['\"])(.*?)\1", fragment, re.IGNORECASE | re.DOTALL)
            if attr and url_key(attr.group(2)) in targets:
                fragment = fragment[:attr.start()] + fragment[attr.end():]
        return fragment

    # A figure can legitimately contain gallery/credit markup and more than
    # one image. Remove only the exact ``img`` nodes; never discard siblings.
    output = re.sub(
        r"<img\b[^>]*>\s*",
        remove_invalid_image_sources,
        html,
        flags=re.IGNORECASE,
    )

    def clean_empty_figure(match: re.Match[str]) -> str:
        body = match.group(2)
        if re.search(r"<img\b", body, flags=re.IGNORECASE):
            return match.group(0)
        body = re.sub(
            r"<figcaption\b[^>]*>.*?</figcaption>\s*",
            "",
            body,
            flags=re.IGNORECASE | re.DOTALL,
        )
        body = re.sub(
            r"<(?:p|div)\b(?=[^>]*\bclass\s*=\s*['\"][^'\"]*(?:image-credit|wp-caption-text)[^'\"]*['\"])[^>]*>.*?</(?:p|div)>\s*",
            "",
            body,
            flags=re.IGNORECASE | re.DOTALL,
        )
        plain = unescape(re.sub(r"<[^>]+>", "", body)).replace("\xa0", " ").strip()
        if not plain:
            return ""
        return body

    output = re.sub(
        r"<figure\b([^>]*)>(.*?)</figure>\s*",
        clean_empty_figure,
        output,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return output


def normal_media_urls_to_insert(
    html: str,
    media_urls: list[str] | tuple[str, ...],
    featured_url: str = "",
) -> list[str]:
    """Return normal-article media URLs that still need HTML insertion.

    This is the shared accounting used by MEDIA preflight and COMPOSE. It
    excludes URLs already present in the current HTML, the featured asset,
    empty values, and duplicates while preserving first-seen order.
    """
    if not isinstance(html, str) or not isinstance(media_urls, (list, tuple)):
        raise MediaInsertionError("HTML and media URLs have invalid types")
    featured = _canonical_media_url(str(featured_url or "").strip())
    existing_urls = {
        _canonical_media_url(match.group(2))
        for match in re.finditer(
            r"\bsrc\s*=\s*(['\"])(.*?)\1",
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
    }
    selected: list[str] = []
    seen: set[str] = set()
    for value in media_urls:
        media_url = str(value or "").strip()
        canonical = _canonical_media_url(media_url)
        if not media_url or canonical in existing_urls or canonical == featured or canonical in seen:
            continue
        selected.append(media_url)
        seen.add(canonical)
    return selected


def _canonical_media_url(value: str) -> str:
    parsed = urlparse(unescape(str(value or "").strip()))
    if not parsed.scheme or not parsed.netloc:
        return str(value or "").strip().split("?", 1)[0]
    path = re.sub(r"-\d+x\d+(?=\.[^./]+$)", "", parsed.path)
    return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}{path.rstrip('/') or '/'}"


def _safe_media_tail(value: str) -> bool:
    """Accept only trailing, self-contained media blocks after the body text."""
    tail = str(value or "")
    if not tail.strip():
        return True
    block_patterns = (
        re.compile(r"\[caption\b[^\]]*\][\s\S]*?\[/caption\]", re.IGNORECASE),
        re.compile(r"<figure\b[^>]*>[\s\S]*?</figure>", re.IGNORECASE),
        re.compile(r"<img\b[^>]*>", re.IGNORECASE),
    )
    remainder = tail
    changed = True
    while changed:
        changed = False
        for pattern in block_patterns:
            match = pattern.search(remainder)
            if match and re.search(r"<img\b", match.group(0), re.IGNORECASE):
                remainder = remainder[:match.start()] + remainder[match.end():]
                changed = True
                break
    return not remainder.strip()


def normal_media_slot_capacity(
    html: str,
    *,
    minimum_spacing: int = 3,
) -> dict[str, Any]:
    """Describe safe normal-article slots, reserving every existing image.

    The count is the maximum number of *additional* images that can be placed
    without violating the established lead and spacing rules. Existing images
    occupy their nearest paragraph boundary even when they are not V2-approved;
    approval/counting remains a separate media-evidence decision.
    """
    if not isinstance(html, str):
        raise MediaInsertionError("HTML must be a string")
    if isinstance(minimum_spacing, bool) or minimum_spacing < 1:
        raise MediaInsertionError("minimum_spacing must be positive")
    structurally_verified = _media_html_is_structurally_balanced(html)
    paragraph_matches = list(
        re.finditer(r"<p\b[^>]*>([\s\S]*?)</p>", html, flags=re.IGNORECASE)
    )
    paragraph_ends = [
        match.end() for match in re.finditer(r"</p>\s*", html, flags=re.IGNORECASE)
    ]
    paragraph_count = len(paragraph_matches)
    tail = html[paragraph_ends[-1]:] if paragraph_ends else html
    body_end_available = bool(paragraph_ends and _safe_media_tail(tail))
    last_slot = (
        paragraph_count - 1
        if body_end_available
        else max(0, paragraph_count - 2)
    )
    substantive = [
        index for index, match in enumerate(paragraph_matches)
        if _plain_word_count(match.group(1)) >= 12
    ]
    first_slot = substantive[1] if len(substantive) >= 2 else None
    existing_slots: set[int] = set()
    for image in re.finditer(r"<img\b[^>]*>", html, flags=re.IGNORECASE):
        containing = next(
            (
                index for index, paragraph in enumerate(paragraph_matches)
                if paragraph.start() <= image.start() < paragraph.end()
            ),
            None,
        )
        if containing is not None:
            slot = containing
        else:
            slot = bisect_right(paragraph_ends, image.start()) - 1
            if slot < 0:
                # Images before the editorial lead still reserve its earliest
                # boundaries; they do not become verified inline coverage.
                slot = 0
            if paragraph_count:
                slot = min(slot, paragraph_count - 1)
        existing_slots.add(slot)

    if first_slot is None:
        candidates: list[int] = []
    else:
        candidates = [
            slot for slot in range(first_slot, last_slot + 1)
            if all(abs(slot - existing) >= minimum_spacing for existing in existing_slots)
        ]
    maximum: list[int] = []
    for slot in candidates:
        if not maximum or slot - maximum[-1] >= minimum_spacing:
            maximum.append(slot)
    return {
        "verified": structurally_verified,
        "paragraph_count": paragraph_count,
        "substantive_paragraphs": len(substantive),
        "first_slot": first_slot,
        "last_slot": last_slot,
        "body_end_available": body_end_available,
        "existing_media_slots": sorted(existing_slots),
        "available_slots": candidates,
        "capacity": len(maximum) if structurally_verified else 0,
        "capacity_slots": maximum if structurally_verified else [],
        "minimum_spacing": minimum_spacing,
    }


def _select_spaced_slots(
    candidates: list[int], count: int, minimum_spacing: int,
) -> list[int]:
    """Choose a deterministic, evenly distributed feasible subset of slots."""
    if count == 0:
        return []
    if count == 1:
        target = (candidates[0] + candidates[-1]) / 2
        return [min(candidates, key=lambda slot: (abs(slot - target), slot))]
    targets = [
        candidates[0] + (candidates[-1] - candidates[0]) * index / (count - 1)
        for index in range(count)
    ]
    memo: dict[tuple[int, int], tuple[float, tuple[int, ...]] | None] = {}

    def solve(position: int, previous_index: int) -> tuple[float, tuple[int, ...]] | None:
        key = (position, previous_index)
        if key in memo:
            return memo[key]
        if position == count:
            return (0.0, ())
        best: tuple[float, tuple[int, ...]] | None = None
        remaining = count - position - 1
        for index in range(previous_index + 1, len(candidates) - remaining):
            slot = candidates[index]
            if previous_index >= 0 and slot - candidates[previous_index] < minimum_spacing:
                continue
            suffix = solve(position + 1, index)
            if suffix is None:
                continue
            candidate = ((slot - targets[position]) ** 2 + suffix[0], (slot, *suffix[1]))
            if best is None or candidate < best:
                best = candidate
        memo[key] = best
        return best

    selected = solve(0, -1)
    if selected is None:
        raise MediaInsertionError("insufficient spaced paragraph slots", code="insufficient_paragraph_slots")
    return list(selected[1])


def select_normal_media_slots(
    planned_slots: list[int],
    count: int,
    *,
    minimum_spacing: int = 3,
) -> list[int]:
    """Select a deterministic subset from a body-bound verified slot plan."""
    if isinstance(count, bool) or count < 0 or count > len(planned_slots):
        raise MediaInsertionError("requested media count exceeds the verified placement plan")
    if any(isinstance(slot, bool) or not isinstance(slot, int) or slot < 0 for slot in planned_slots):
        raise MediaInsertionError("verified placement plan contains an invalid slot")
    if len(set(planned_slots)) != len(planned_slots):
        raise MediaInsertionError("verified placement plan contains duplicate slots")
    ordered = sorted(planned_slots)
    return _select_spaced_slots(ordered, count, minimum_spacing)


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
    layout = normal_media_slot_capacity(html, minimum_spacing=minimum_spacing)
    count = len(plan)
    if count == 0:
        return []
    # The lead needs two substantive paragraphs before the first inline image.
    # A tag-only or one-line spacer is not editorial lead copy and cannot earn
    # that placement. Slots are zero-based paragraph boundaries.
    if layout["first_slot"] is None:
        raise MediaInsertionError(
            "at least two substantive paragraphs are required before the first inline image",
            code="insufficient_substantive_paragraphs",
        )
    candidates = list(layout["available_slots"])
    if int(layout["capacity"]) < count:
        raise MediaInsertionError(
            f"insufficient valid paragraph slots: requested={count}, available={layout['capacity']}, "
            f"minimum_spacing={minimum_spacing}, first_slot={layout['first_slot']}, "
            f"last_slot={layout['last_slot']}, existing_media_slots={layout['existing_media_slots']}, "
            f"substantive_paragraphs={layout['substantive_paragraphs']}, "
            f"html_paragraphs={layout['paragraph_count']}",
            code="insufficient_paragraph_slots",
        )
    slots = _select_spaced_slots(candidates, count, minimum_spacing)
    planned: list[dict[str, Any]] = []
    for item, slot in zip(plan, slots):
        placement = dict(item)
        placement["paragraph_index"] = int(slot)
        if layout["body_end_available"] and slot == layout["last_slot"]:
            placement["placement"] = "after_editorial_body"
        planned.append(placement)
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
        # Splitting duplicates the opening <p> tag. Only plain paragraphs are
        # eligible so IDs, classes, styling, and accessibility attributes are
        # never duplicated or moved to a different text fragment.
        if (
            not re.fullmatch(r"<p\s*>", match.group("open"), re.IGNORECASE)
            or _plain_word_count(body) < 36
            or "<h" in body.casefold()
            or "[" in body
            or "]" in body
        ):
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
    kept at least three paragraphs apart. Listicles (numbered H2 items or
    numbered H3 items under a grouping H2): each figure goes immediately
    after the numbered heading preceding the
    targeted paragraph, as required by ``validate_list_content``.
    """
    if not isinstance(html, str) or not isinstance(plan, list):
        raise MediaInsertionError("HTML and media plan have invalid types")
    if len(plan) > 12:
        raise MediaInsertionError("at most twelve images are allowed")
    paragraph_ends = [match.end() for match in re.finditer(r"</p>\s*", html, flags=re.IGNORECASE)]
    existing_media_slots = (
        set(normal_media_slot_capacity(html)["existing_media_slots"])
        if not listicle else set()
    )
    placements: list[tuple[int, str, Mapping[str, Any]]] = []
    indexes: list[int] = []
    for item in plan:
        if not isinstance(item, Mapping):
            raise MediaInsertionError("each media placement must be an object")
        required = {"paragraph_index", "media_url", "alt_text", "credit_text", "width", "height"}
        allowed = required | {
            "item_identity", "item_title", "item_group", "html_position",
            "item_number", "section_heading", "section_slot", "subject", "placement",
            "role", "media_id",
        }
        if not required.issubset(item) or set(item) - allowed:
            raise MediaInsertionError("media placement has missing or unknown fields")
        index = item["paragraph_index"]
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise MediaInsertionError("paragraph_index must be non-negative")
        placement_kind = item.get("placement")
        after_editorial_body = placement_kind == "after_editorial_body"
        if "role" in item or "media_id" in item:
            if (
                item.get("role") != "closing_featured_repeat"
                or not after_editorial_body
                or isinstance(item.get("media_id"), bool)
                or not isinstance(item.get("media_id"), int)
                or item.get("media_id", 0) < 1
            ):
                raise MediaInsertionError("only an identified featured repeat may carry a closing role")
        if placement_kind is not None and not after_editorial_body:
            raise MediaInsertionError("unsupported media placement")
        if after_editorial_body and (
            listicle
            or not paragraph_ends
            or index != len(paragraph_ends) - 1
            or not _safe_media_tail(html[paragraph_ends[-1]:])
        ):
            raise MediaInsertionError("after_editorial_body requires the final paragraph boundary")
        if not after_editorial_body and index >= len(paragraph_ends) - (1 if not listicle else 0):
            raise MediaInsertionError("media must be inserted between paragraphs")
        if listicle and after_editorial_body:
            raise MediaInsertionError("listicle media must remain attached to its numbered item")
        if not listicle:
            if index in indexes or any(abs(index - other) < 3 for other in indexes):
                raise MediaInsertionError("images must be at least three paragraphs apart")
            if any(abs(index - existing) < 3 for existing in existing_media_slots):
                raise MediaInsertionError(
                    "new images must be spaced from existing inline media",
                    code="existing_media_spacing_conflict",
                )
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
        placements.append((index, figure, item))
        indexes.append(index)
    if listicle:
        for index, figure, item in sorted(placements, key=lambda value: value[0], reverse=True):
            start = paragraph_ends[index - 1] if index > 0 else 0
            end = paragraph_ends[index]
            headings = list(re.finditer(
                r"<h[23]\b[^>]*>.*?</h[23]>",
                html[start:end],
                re.IGNORECASE | re.DOTALL,
            ))
            target = str(item.get("item_title") or item.get("section_heading") or item.get("subject") or "").strip()

            def heading_text(match: re.Match[str]) -> str:
                value = re.sub(r"<[^>]+>", " ", match.group(0))
                value = " ".join(unescape(value).split())
                value = re.sub(r"^\d{1,3}\s*[.)-]\s*", "", value)
                return value.strip().casefold()

            target_key = re.sub(r"\s+", " ", target).casefold()
            if target_key:
                matches = [candidate for candidate in headings if heading_text(candidate) == target_key]
            else:
                # Legacy plans may not carry the canonical title yet. Infer
                # only when the section contains exactly one item heading;
                # never silently select an arbitrary last heading.
                matches = [
                    candidate for candidate in headings
                    if re.match(r"<h3\b", candidate.group(0), re.IGNORECASE)
                    or re.match(r"<h[23]\b[^>]*>\s*\d{1,3}\s*[.)-]", candidate.group(0), re.IGNORECASE)
                ]
            heading = matches[0] if len(matches) == 1 else None
            if not heading:
                raise MediaInsertionError(
                    f"list item at paragraph {index} has no target H2/H3 before it"
                )
            position = start + heading.end()
            html = html[:position] + figure + html[position:]
        return html
    for index, figure, item in sorted(placements, key=lambda value: value[0], reverse=True):
        position = len(html.rstrip()) if item.get("placement") == "after_editorial_body" else paragraph_ends[index]
        html = html[:position] + figure + html[position:]
    return html


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MediaInsertionError(f"{name} is required")
    return value.strip()
