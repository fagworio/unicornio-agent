"""Validation rules for numbered lists, rankings, and recommendation articles."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser


class ListContentError(ValueError):
    """Raised when a list/ranking article violates its structural contract."""


_LIST_TERMS = re.compile(
    r"\b(?:top|melhores|piores|recomenda(?:ções|cao)|seleção|selecao|lista|ranking|jogos|animes|filmes|séries|series|personagens|smartphones)\b",
    re.IGNORECASE,
)
_COUNT = re.compile(r"\b(\d{1,3})\b")
_COUNT_WITH_TERM = re.compile(
    r"\b(?:top\s+)?(\d{1,3})\s*(?:\s+(melhores|piores|animes|jogos|filmes|séries|series|personagens|smartphones|recomendações|recomendacoes|seleção|selecao|lista|ranking))\b",
    re.IGNORECASE,
)

_NUMBERED_HEADING = re.compile(r"^\s*(\d{1,3})\s*[.)-]\s+(.+?)\s*$")
_HEADING_RE = re.compile(
    r"<(?P<tag>h[23])\b[^>]*>(?P<body>.*?)</(?P=tag)>",
    re.IGNORECASE | re.DOTALL,
)
_AUXILIARY_HEADING = re.compile(
    r"\b(?:confira|leia\s+também|veja\s+também|saiba\s+mais|fontes?|referências?|referencias|conclus(?:ão|ao|ões|oes)|considerações|consideracoes|compartilhe|assine|siga)\b",
    re.IGNORECASE,
)
_LIST_INTENT = re.compile(
    r"\b(?:top|melhores|piores|recomenda(?:ções|coes|cao)|sele(?:ção|cao)|lista|ranking|lista(?:s)?\s+de)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ListItem:
    """One item in the canonical listicle structure.

    ``paragraph_index`` is the zero-based paragraph immediately following the
    item heading.  It is shared by media evidence, insertion and the checklist
    so an H3 item nested below a grouping H2 cannot silently fall back to the
    article-wide subject.
    """

    number: int | None
    heading: str
    tag: str
    heading_start: int
    heading_end: int
    paragraph_index: int
    group_heading: str = ""
    identity: str = ""
    position: int = 0

    @property
    def item_title(self) -> str:
        return self.heading

    @property
    def html_position(self) -> int:
        return self.heading_start


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
        if tag in {"h2", "h3", "p", "figure", "img", "article"} and self._depth == 0:
            self._tag = tag
            self._attrs = attrs
            self._text = []
            if tag == "img":
                self.blocks.append((tag, "", True))
            else:
                self._depth = 1
        elif self._tag is not None and tag not in {"img", "br", "hr", "input", "source", "wbr"}:
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

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Handle HTML void elements without closing their parent block."""
        if tag.lower() == "img":
            if self._tag is None:
                self.blocks.append(("img", "", True))
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._tag is not None:
            self._text.append(data)


def extract_list_items(
    title: str = "",
    html: str = "",
    categories: object = (),
) -> list[ListItem]:
    """Extract the structural list items used by every downstream gate.

    Older content used numbered H2s.  Newer/editorial content commonly uses a
    single grouping H2 followed by numbered H3s (for example, five
    recommendations under ``Recomendações``).  Only numbered H2s, or numbered
    H3s that actually belong to an H2 group, are list items.  Thus a normal
    article merely mentioning five films remains a normal article.
    """
    source = str(html or "")
    headings: list[dict[str, object]] = []
    current_group = ""
    for match in _HEADING_RE.finditer(source):
        tag = str(match.group("tag")).lower()
        text = " ".join(re.sub(r"<[^>]+>", " ", match.group("body")).split())
        if tag == "h2":
            current_group = text
        numbered = _NUMBERED_HEADING.match(text)
        headings.append(
            {
                "tag": tag,
                "text": text,
                "match": match,
                "numbered": numbered,
                "group": current_group if tag == "h3" else "",
            }
        )

    numbered_h2 = [row for row in headings if row["tag"] == "h2" and row["numbered"]]
    if len(numbered_h2) >= 2:
        selected = numbered_h2
    else:
        grouped_h3 = [
            row for row in headings
            if row["tag"] == "h3" and row["numbered"] and row["group"]
        ]
        # A numbered H3 without a containing H2 is an ordinary subheading,
        # not proof that the article is a listicle.
        selected = grouped_h3 if len(grouped_h3) >= 2 else []
        if not selected:
            descriptive_h3 = [
                row for row in headings
                if row["tag"] == "h3"
                and row["group"]
                and not row["numbered"]
                and _is_descriptive_item(row, source)
            ]
            if len(descriptive_h3) >= 2 and _has_list_intent(title, categories, headings):
                selected = descriptive_h3

    items: list[ListItem] = []
    for position, row in enumerate(selected, start=1):
        numbered = row["numbered"]
        match = row["match"]
        if not isinstance(match, re.Match):
            continue
        number = int(numbered.group(1)) if isinstance(numbered, re.Match) else None
        heading = numbered.group(2).strip() if isinstance(numbered, re.Match) else str(row["text"])
        group_heading = str(row["group"] or "")
        identity = _item_identity(group_heading, heading, number, position)
        paragraph_index = len(re.findall(r"<p\b", source[: match.start()], flags=re.IGNORECASE))
        items.append(
            ListItem(
                number=number,
                heading=heading,
                tag=str(row["tag"]),
                heading_start=match.start(),
                heading_end=match.end(),
                paragraph_index=paragraph_index,
                group_heading=group_heading,
                identity=identity,
                position=position,
            )
        )
    return items


def detect_list_format(title: str, html: str = "", categories: object = ()) -> int | None:
    """Return the promised item count, or None when the article is not a list.

    O numero no titulo e uma PROMESSA, nunca a prova. "Nostalgia Sem Wifi
    Festival: 40 jogos retrô e música" e um EVENTO que oferece 40 jogos, nao um
    listicle de 40 itens e descricoes — e a regra antiga (numero do titulo
    decide sozinho) exigia 40 IMAGENS para um artigo normal, travando o post
    para sempre. "10 animes para ficar de olho" tinha o mesmo destino.

    A prova estrutural e a mesma adotada no post_subjects(): H2 numerados ou
    H3 numerados dentro de um H2 agrupador. Sem essa estrutura o post segue a
    politica 2/4/6.
    """
    items = extract_list_items(title, html, categories)
    if len(items) < 2:
        return None  # sem estrutura de lista: o titulo nao decide
    # Com a estrutura presente, a contagem vem do TITULO quando ele promete —
    # a conferencia prometido x realizado continua no validate_list_content.
    match = _COUNT_WITH_TERM.search(title or "")
    if match:
        return int(match.group(1))
    top_match = re.search(r"\btop\s+(\d{1,3})\b", title or "", re.IGNORECASE)
    if top_match:
        return int(top_match.group(1))
    numbered = [item.number for item in items if item.number is not None]
    return max(numbered) if numbered else len(items)


def validate_list_content(title: str, html: str, categories: object = ()) -> dict[str, int | bool]:
    """Validate exact count and item-heading -> image -> description order."""
    promised = detect_list_format(title, html, categories)
    if promised is None:
        return {"is_list": False, "items": 0, "passed": True}
    if re.search(r"<article\b", html, re.IGNORECASE):
        raise ListContentError("article tag must be removed from post content")
    parser = _BlockParser()
    parser.feed(html)
    blocks = parser.blocks
    items = extract_list_items(title, html, categories)
    if len(items) != promised:
        raise ListContentError(
            f"title promises {promised} items but content has {len(items)} numbered list items"
        )
    heading_indexes = []
    for item in items:
        match = next(
            (index for index, (tag, value, _is_void) in enumerate(blocks)
             if tag == item.tag and _heading_name(value, item) is not None),
            None,
        )
        if match is None:
            raise ListContentError(f"item {item.position} heading is missing from the content blocks")
        heading_indexes.append((match, item))
    expected = list(range(promised, 0, -1)) if heading_indexes and heading_indexes[0][1].number == promised else list(range(1, promised + 1))
    seen: list[int] = []
    for position, (index, item) in enumerate(heading_indexes):
        number, name = item.number, item.heading
        if not name or len(name) < 3 or (number is not None and ":" not in name):
            raise ListContentError(f"item {item.position} {item.tag.upper()} must identify the item and its description")
        if number is not None:
            seen.append(number)
        if index + 1 >= len(blocks) or blocks[index + 1][0] not in {"figure", "img"}:
            raise ListContentError(f"item {number} must have its image immediately after {item.tag.upper()}")
        if index + 2 >= len(blocks) or blocks[index + 2][0] != "p":
            raise ListContentError(f"item {number} description must follow the image")
    if seen and seen != expected:
        raise ListContentError(f"invalid item numbering: expected {expected}, got {seen}")
    return {"is_list": True, "items": len(items), "promised": promised, "passed": True}


def _number(heading: str) -> int | None:
    match = _NUMBERED_HEADING.match(heading)
    return int(match.group(1)) if match else None


def _category_text(categories: object) -> str:
    if isinstance(categories, str):
        return categories
    if isinstance(categories, dict):
        return " ".join(_category_text(value) for value in categories.values())
    if isinstance(categories, (list, tuple, set, frozenset)):
        return " ".join(_category_text(value) for value in categories)
    return str(categories or "")


def _has_list_intent(title: str, categories: object, headings: list[dict[str, object]]) -> bool:
    context = " ".join([
        str(title or ""),
        _category_text(categories),
        " ".join(str(row.get("group") or "") for row in headings),
    ])
    return bool(_LIST_INTENT.search(context) or re.search(r"\b\d{1,3}\s+(?:jogos|filmes|animes|séries|series)\b", title or "", re.I))


def _is_descriptive_item(row: dict[str, object], source: str) -> bool:
    text = str(row.get("text") or "").strip()
    if not text or _AUXILIARY_HEADING.search(text):
        return False
    if len(re.findall(r"[\wÀ-ÿ]+", text)) < 2:
        return False
    match = row.get("match")
    if not isinstance(match, re.Match):
        return False
    next_heading = _HEADING_RE.search(source, match.end())
    body = source[match.end(): next_heading.start() if next_heading else len(source)]
    return bool(re.search(r"<(?:p|figure|img)\b", body, re.IGNORECASE))


def _slug(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def _item_identity(group: str, heading: str, number: int | None, position: int) -> str:
    group_key = _slug(group) or "root"
    item_key = f"number-{number}" if number is not None else (_slug(heading) or f"position-{position}")
    return f"{group_key}:{item_key}"


def _heading_name(value: str, item: ListItem) -> str | None:
    text = " ".join(str(value or "").split())
    numbered = _NUMBERED_HEADING.match(text)
    name = numbered.group(2).strip() if numbered else text
    return name if name.casefold() == item.heading.casefold() else None


def list_item_media_coverage(
    title: str,
    html: str,
    media_context: object,
    categories: object = (),
) -> dict[str, object]:
    """Validate one distinct, item-matched inline asset per list item.

    This is intentionally independent of the global image quota and of the
    featured asset.  It is shared by checklist, COMPOSE and the final
    publisher contract so a valid image for item A cannot satisfy item B.
    """
    items = extract_list_items(title, html, categories)
    if len(items) < 2:
        return {"applicable": False, "passed": True, "missing": []}
    rows: object = media_context
    if isinstance(media_context, dict):
        rows = media_context.get("inline") or media_context.get("accepted") or ()
    elif media_context is not None:
        rows = getattr(media_context, "inline", ())
    if isinstance(rows, dict):
        rows = rows.get("accepted") or ()
    assets = list(rows) if isinstance(rows, (list, tuple)) else []
    canonical_asset_contract = any(
        isinstance(asset, dict) and str(asset.get("item_identity") or "").strip()
        for asset in assets
    )
    covered: dict[str, list[object]] = {item.identity: [] for item in items}
    used: set[str] = set()
    for asset in assets:
        if not isinstance(asset, dict):
            to_dict = getattr(asset, "to_dict", None)
            asset = to_dict() if callable(to_dict) else {}
        if not isinstance(asset, dict):
            continue
        decision = str((asset.get("visual_verification") or {}).get("decision") or "").upper()
        if decision in {"SAME_IMAGE", "SAME_ART_CROP"}:
            continue
        identity = str(asset.get("item_identity") or "").casefold()
        number = str(asset.get("item_number") or "").casefold()
        subject = str(asset.get("item_title") or asset.get("subject") or "").strip().casefold()
        media_key = str(asset.get("media_id") or asset.get("media_url") or "").strip()
        if not media_key or media_key in used:
            continue
        for item in items:
            expected_subject = item.heading.strip().casefold()
            matches_identity = identity == item.identity.casefold()
            matches_legacy = number in {
                str(item.number or "").casefold(),
                str(item.position).casefold(),
            }
            matches_subject = subject == expected_subject
            if (matches_identity or (not canonical_asset_contract and matches_legacy)) and matches_subject:
                covered[item.identity].append(asset)
                used.add(media_key)
                break
    missing = [item.identity for item in items if not covered[item.identity]]
    return {
        "applicable": True,
        "passed": not missing,
        "required": len(items),
        "covered": len(items) - len(missing),
        "missing": missing,
        "items": [item.identity for item in items],
    }
