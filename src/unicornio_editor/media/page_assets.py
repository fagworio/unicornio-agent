"""Shared extraction of image assets declared by an origin page.

The verifier and evidence scorer must inspect the same page representation. This
module intentionally does not decide relevance or provenance; it only extracts
assets and their local HTML context.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin


@dataclass(frozen=True)
class PageAsset:
    url: str
    original_url: str
    source_attribute: str
    alt: str = ""
    width: str = ""
    height: str = ""
    figcaption: str = ""
    heading: str = ""


class _AssetParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.assets: list[PageAsset] = []
        self._pending_context: dict[str, str] = {}
        self._script_type = ""
        self._script_text: list[str] = []

    def _add(self, raw: str, attr: str, attrs: dict[str, str], **context: str) -> None:
        raw = (raw or "").strip()
        if not raw:
            return
        for item in raw.split(",") if attr in {"srcset", "data-srcset"} else [raw]:
            original = item.strip().split(" ", 1)[0]
            if not original:
                continue
            self.assets.append(PageAsset(
                url=urljoin(self.base_url, original),
                original_url=original,
                source_attribute=attr,
                alt=attrs.get("alt", "").strip(),
                width=attrs.get("width", "").strip(),
                height=attrs.get("height", "").strip(),
                **context,
            ))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): v or "" for k, v in attrs}
        tag = tag.lower()
        if tag in {"img", "source"}:
            for key in ("src", "data-src", "data-lazy-src", "data-original", "srcset", "data-srcset"):
                if attr_map.get(key):
                    self._add(attr_map[key], key, attr_map)
        elif tag == "meta" and attr_map.get("content") and (
            attr_map.get("property", "").lower() in {"og:image", "twitter:image"}
            or attr_map.get("name", "").lower() == "twitter:image"
        ):
            self._add(attr_map["content"], attr_map.get("property") or attr_map.get("name", "meta"), attr_map)
        elif tag == "link" and attr_map.get("href") and attr_map.get("rel", "").lower() == "image_src":
            self._add(attr_map["href"], "link:image_src", attr_map)
        elif tag == "script":
            self._script_type = attr_map.get("type", "").lower()
            self._script_text = []

    def handle_data(self, data: str) -> None:
        if self._script_type == "application/ld+json":
            self._script_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "script" or self._script_type != "application/ld+json":
            return
        try:
            value: Any = json.loads("".join(self._script_text))
        except (TypeError, ValueError):
            value = None
        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for key in ("contentUrl", "thumbnailUrl", "image"):
                    value = node.get(key)
                    if isinstance(value, str):
                        self._add(value, f"jsonld:{key}", {})
                    elif isinstance(value, dict):
                        walk(value)
                    elif isinstance(value, list):
                        for item in value: walk(item)
                for item in node.values():
                    if isinstance(item, (dict, list)): walk(item)
            elif isinstance(node, list):
                for item in node: walk(item)
        walk(value)
        self._script_type = ""
        self._script_text = []


def extract_page_assets(html: str, base_url: str = "") -> list[PageAsset]:
    if not isinstance(html, str) or not html:
        return []
    parser = _AssetParser(base_url)
    try:
        parser.feed(html)
    except Exception:  # malformed HTML remains best-effort
        pass
    out: list[PageAsset] = []
    seen: set[str] = set()
    for asset in parser.assets:
        if not asset.url or asset.url in seen:
            continue
        seen.add(asset.url)
        out.append(asset)
    return out


__all__ = ["PageAsset", "extract_page_assets"]
