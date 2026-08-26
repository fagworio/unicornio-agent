"""Deterministic HTML cleanup for imported WordPress post content."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser
from typing import Union
from urllib.parse import urlparse


Text = str


@dataclass
class _Node:
    tag: str | None
    attrs: list[tuple[str, str | None]] = field(default_factory=list)
    children: list[Union["_Node", Text]] = field(default_factory=list)


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node(None)
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag.lower(), attrs)
        self.stack[-1].children.append(node)
        if tag.lower() not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack[-1].children.append(_Node(tag.lower(), attrs))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)

    def handle_comment(self, data: str) -> None:
        return


_DROP_TAGS = {"img", "script", "style", "iframe", "object", "embed", "form", "input", "button", "select", "textarea"}
_UNWRAP_TAGS = {"article", "div"}
_VOID_TAGS = {"area", "base", "br", "col", "hr", "link", "meta", "param", "source", "track", "wbr"}
_ALLOWED_ATTRS = {"class", "id", "title", "alt", "href", "src", "target", "rel", "width", "height"}
_LICENSE_TOKEN = re.compile(
    r"cc\s*0"
    r"|cc\s*by(?:[\s-]*[-–]?\s*(?:sa|nc|nd|nc-sa|nc-nd))?"
    r"|creative\s*commons"
    r"|public\s*domain"
    r"|dominio\s*publico"
    r"|dom[ií]nio\s*p[uú]blico"
    r"|uso\s*com\s*credito"
)
_CREDIT_MARKER = re.compile(r"credito\s+da\s+imagem|^credito\s*:")


def _repair_orphan_media(html: str) -> str:
    if not isinstance(html, str) or "<img" not in html.lower():
        return html
    _FIG_WITH_IMG = re.compile(
        r"(<figure\b[^>]*>.*?</figure>)\s*(<img\b[^>]*>)", re.IGNORECASE | re.DOTALL
    )
    _IMG_WITH_FIG = re.compile(
        r"(<img\b[^>]*>)\s*(<figure\b[^>]*>.*?</figure>)", re.IGNORECASE | re.DOTALL
    )

    def _fold(match: re.Match[str]) -> str:
        figure, img = match.group(1), match.group(2)
        if "<img" in figure.lower():
            return match.group(0)
        caption = re.search(r"<figcaption>", figure, re.IGNORECASE)
        if not caption or "credito da imagem" not in _normalize(figure):
            return match.group(0)
        return figure[: caption.start()] + img + figure[caption.start():]

    result = html
    for _ in range(10):
        folded = _FIG_WITH_IMG.sub(_fold, result)
        folded = _IMG_WITH_FIG.sub(_fold, folded)
        if folded == result:
            break
        result = folded
    return result


# Shortcode nativo do WordPress: [caption ...]<img .../> Crédito...[/caption]
_WP_CAPTION_BLOCK_RE = re.compile(
    r"\[caption\b[^\]]*\].*?\[/caption\]", re.IGNORECASE | re.DOTALL
)
_WP_CAPTION_PLACEHOLDER = "__WP_CAPTION_%d__"


def _extract_wp_captions(html: str) -> tuple[str, list[tuple[str, str]]]:
    """Troca blocos [caption]...[/caption] por placeholders preservando-os intactos.

    O _TreeParser (HTMLParser) nao entende o shortcode: trata [caption ...] como
    texto e o <img> interno como elemento solto, que o _serialize descartaria.
    Para nao perder a imagem, extraimos cada bloco ANTES do parse e o
    re-inserimos DEPOIS.
    """
    if not isinstance(html, str) or "[caption" not in html.lower():
        return html, []
    store: list[tuple[str, str]] = []

    def _store(match: re.Match[str]) -> str:
        key = _WP_CAPTION_PLACEHOLDER % len(store)
        store.append((key, match.group(0)))
        return key

    return _WP_CAPTION_BLOCK_RE.sub(_store, html), store


def _sanitize_wp_caption_block(block: str) -> str:
    """Sanitiza a legenda de um [caption] preservando o <img> (texto puro)."""
    img = re.search(r"<img\b[^>]*>", block, re.IGNORECASE)
    if not img:
        return block
    head = block[: img.start()]
    tail = block[img.end():]
    caption = re.sub(r"<[^>]+>", " ", tail)
    caption = re.sub(r"[\s\u00a0]+", " ", caption).strip()
    if not caption.lower().startswith("crédito da imagem:"):
        caption = "Crédito da imagem: " + caption
    return head + img.group(0) + " " + caption


def clean_html(html: str) -> str:
    """Return safe, normalized HTML while preserving editorial text.

    Imported inline images are dropped UNLESS they live inside a <figure> whose
    <figcaption> carries a complete credit block, OR inside a WordPress
    [caption]...[/caption] shortcode (rendered by WP as a figure with credit).
    Both are preserved so verified licensed imagery is not re-discovered.
    """
    if not isinstance(html, str):
        raise TypeError("html must be a string")
    extracted, captions = _extract_wp_captions(html)
    parser = _TreeParser()
    parser.feed(extracted)
    parser.close()
    cleaned = "".join(_serialize(child) for child in parser.root.children).strip()
    if not captions:
        return cleaned
    for key, block in captions:
        cleaned = cleaned.replace(key, _sanitize_wp_caption_block(block))
    return cleaned


def _serialize(value: _Node | str, keep_licensed_image: bool = False) -> str:
    if isinstance(value, str):
        return escape(value, quote=False)
    text = _node_text(value).strip().lower()
    if value.tag == "img":
        return _serialize_img(value) if keep_licensed_image else ""
    if value.tag in _DROP_TAGS:
        return ""
    if value.tag == "h3" and "confira mais novidades em nosso portal de notícias" in text:
        return ""
    if value.tag == "em" and text.startswith("fonte:"):
        return ""
    if value.tag == "figure":
        licensed = _has_complete_credit(value)
        children = "".join(
            _serialize(child, keep_licensed_image=licensed) for child in value.children
        )
    else:
        children = "".join(_serialize(child) for child in value.children)
    if value.tag in _UNWRAP_TAGS:
        return children
    attrs = _safe_attrs(value.attrs)
    attrs_text = "".join(
        f' {name}="{escape(attr_value, quote=True)}"'
        for name, attr_value in attrs
        if attr_value is not None
    )
    if value.tag in _VOID_TAGS:
        return f"<{value.tag}{attrs_text} />"
    return f"<{value.tag}{attrs_text}>{children}</{value.tag}>"


def _serialize_img(node: _Node) -> str:
    attrs = _safe_attrs(node.attrs)
    attrs_text = "".join(
        f' {name}="{escape(attr_value, quote=True)}"'
        for name, attr_value in attrs
        if attr_value is not None
    )
    return f"<img{attrs_text} />"


def _has_complete_credit(figure: _Node) -> bool:
    for child in figure.children:
        if isinstance(child, _Node) and child.tag == "figcaption":
            text = _node_text(child)
            break
    else:
        return False
    if not text.strip():
        return False
    normalized = _normalize(text)
    if not _CREDIT_MARKER.search(normalized):
        return False
    if not _LICENSE_TOKEN.search(normalized):
        return False
    return "http://" in normalized or "https://" in normalized


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text.lower())
    return decomposed.encode("ascii", "ignore").decode("ascii")


def _node_text(node: _Node) -> str:
    return "".join(child if isinstance(child, str) else _node_text(child) for child in node.children)


def _safe_attrs(attrs: list[tuple[str, str | None]]) -> list[tuple[str, str | None]]:
    result = []
    for name, value in attrs:
        name = name.lower()
        if name not in _ALLOWED_ATTRS or name.startswith("on"):
            continue
        if name == "href" and value is not None:
            parsed = urlparse(value.strip())
            if parsed.scheme and parsed.scheme not in {"http", "https", "mailto"}:
                continue
        result.append((name, value))
    return result
