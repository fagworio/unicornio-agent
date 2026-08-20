"""Deterministic HTML cleanup for imported WordPress post content."""

from __future__ import annotations

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
_ALLOWED_ATTRS = {"class", "id", "title", "alt", "href", "target", "rel", "width", "height"}


def clean_html(html: str) -> str:
    """Return safe, normalized HTML while preserving editorial text."""
    if not isinstance(html, str):
        raise TypeError("html must be a string")
    parser = _TreeParser()
    parser.feed(html)
    parser.close()
    return "".join(_serialize(child) for child in parser.root.children).strip()


def _serialize(value: _Node | str) -> str:
    if isinstance(value, str):
        return escape(value, quote=False)
    text = _node_text(value).strip().lower()
    if value.tag in _DROP_TAGS:
        return ""
    if value.tag == "h3" and "confira mais novidades em nosso portal de notícias" in text:
        return ""
    if value.tag == "em" and text.startswith("fonte:"):
        return ""
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
