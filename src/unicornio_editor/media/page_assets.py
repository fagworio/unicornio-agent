"""Shared extraction of image assets declared by an origin page.

The verifier and evidence scorer must inspect the same page representation. This
module intentionally does not decide relevance or provenance; it only extracts
assets and their local HTML context.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote, urljoin, urlparse


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
    context_kind: str = "unknown"


class _AssetParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.assets: list[PageAsset] = []
        self._pending_context: dict[str, str] = {}
        self._script_type = ""
        self._script_text: list[str] = []
        self._heading = ""
        self._heading_tag = ""
        self._heading_text: list[str] = []
        self._figure_assets: list[int] | None = None
        self._figcaption = False
        self._figcaption_text: list[str] = []
        self._scope_stack: list[tuple[str, str]] = []

    def _context_kind(self) -> str:
        markers = " ".join(
            f"{tag} {marker}" for tag, marker in self._scope_stack
        ).casefold()
        tokens = set(re.findall(r"[a-z0-9]+", markers))
        excluded = {
            "aside", "footer", "nav", "sidebar", "related", "recommend",
            "recommended", "author", "avatar", "comment", "comments",
            "advert", "advertising", "banner", "sponsor", "podcast", "audio",
        }
        if tokens & excluded or any(
            token.startswith("ad-") or token.endswith("-ad") for token in tokens
        ):
            return "excluded"
        if any(tag in {"main", "article", "figure"} for tag, _marker in self._scope_stack):
            return "article_body"
        if any(tag == "header" for tag, _marker in self._scope_stack):
            return "article_header"
        return "unknown"

    def _push_scope(self, tag: str, attrs: dict[str, str]) -> None:
        if tag not in {"main", "article", "header", "aside", "footer", "nav", "section", "div", "figure"}:
            return
        marker = " ".join(
            value for key in ("id", "class", "role")
            if (value := attrs.get(key, "").strip())
        )
        self._scope_stack.append((tag, marker))

    def _pop_scope(self, tag: str) -> None:
        for index in range(len(self._scope_stack) - 1, -1, -1):
            if self._scope_stack[index][0] == tag:
                del self._scope_stack[index:]
                return

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
                heading=self._heading,
                context_kind=context.pop("context_kind", self._context_kind()),
                **context,
            ))
            if self._figure_assets is not None:
                self._figure_assets.append(len(self.assets) - 1)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): v or "" for k, v in attrs}
        tag = tag.lower()
        self._push_scope(tag, attr_map)
        if tag in {"h1", "h2", "h3", "h4"}:
            self._heading_tag = tag
            self._heading_text = []
        elif tag == "figure":
            self._figure_assets = []
        elif tag == "figcaption":
            self._figcaption = True
            self._figcaption_text = []
        if tag in {"img", "source"}:
            for key in ("src", "data-src", "data-lazy-src", "data-original", "srcset", "data-srcset"):
                if attr_map.get(key):
                    self._add(attr_map[key], key, attr_map)
        elif tag == "meta" and attr_map.get("content") and (
            attr_map.get("property", "").lower() in {"og:image", "twitter:image"}
            or attr_map.get("name", "").lower() == "twitter:image"
        ):
            self._add(
                attr_map["content"],
                attr_map.get("property") or attr_map.get("name", "meta"),
                attr_map,
                context_kind="metadata",
            )
        elif tag == "link" and attr_map.get("href") and attr_map.get("rel", "").lower() == "image_src":
            self._add(attr_map["href"], "link:image_src", attr_map, context_kind="metadata")
        elif tag == "script":
            self._script_type = attr_map.get("type", "").lower()
            self._script_text = []

    def handle_data(self, data: str) -> None:
        if self._script_type == "application/ld+json":
            self._script_text.append(data)
        if self._heading_tag:
            self._heading_text.append(data)
        if self._figcaption:
            self._figcaption_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == self._heading_tag:
            self._heading = " ".join("".join(self._heading_text).split())
            self._heading_tag = ""
            self._heading_text = []
        elif tag == "figcaption" and self._figcaption:
            caption = " ".join("".join(self._figcaption_text).split())
            for index in self._figure_assets:
                asset = self.assets[index]
                self.assets[index] = PageAsset(
                    url=asset.url, original_url=asset.original_url,
                    source_attribute=asset.source_attribute, alt=asset.alt,
                    width=asset.width, height=asset.height,
                    figcaption=caption, heading=asset.heading,
                    context_kind=asset.context_kind,
                )
            self._figcaption = False
            self._figcaption_text = []
        elif tag == "figure":
            self._figure_assets = None
        if tag != "script" or self._script_type != "application/ld+json":
            self._pop_scope(tag)
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
                        self._add(value, f"jsonld:{key}", {}, context_kind="metadata")
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
        self._pop_scope(tag)


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


def rank_page_assets(
    assets: list[PageAsset],
    candidate_url: str,
    *,
    subject: str = "",
    limit: int = 10,
) -> list[PageAsset]:
    """Rank likely source assets before visual comparison.

    This keeps logos, avatars and related-post thumbnails from consuming the
    bounded verification budget before the actual candidate asset is tested.
    Exact URL/filename matches remain deterministic; contextual signals only
    decide the order of the bounded visual checks.
    """
    target = urlparse(unquote(str(candidate_url or "")))
    target_path = target.path.rstrip("/").casefold()
    target_name = target_path.rsplit("/", 1)[-1]
    target_stem = re.sub(r"\.[a-z0-9]{2,5}$", "", target_name, flags=re.I)
    subject_tokens = {token for token in re.findall(r"[\wÀ-ÿ]+", str(subject).casefold()) if len(token) >= 3}
    generic_tokens = {
        "logo", "avatar", "banner", "ad", "advert", "related", "author",
        "audio", "podcast", "soundcloud", "spotify", "artwork",
    }

    def score(asset: PageAsset) -> tuple[int, int, int]:
        parsed = urlparse(asset.url)
        path = unquote(parsed.path.rstrip("/")).casefold()
        name = path.rsplit("/", 1)[-1]
        stem = re.sub(r"\.[a-z0-9]{2,5}$", "", name, flags=re.I)
        text = " ".join((asset.alt, asset.figcaption, asset.heading)).casefold()
        points = 0
        if path == target_path:
            points += 1000
        if name == target_name or (target_stem and stem == target_stem):
            points += 500
        if target.hostname and parsed.hostname and target.hostname.casefold() == parsed.hostname.casefold():
            points += 30
        if target_stem and target_stem in stem:
            points += 40
        if asset.context_kind == "excluded":
            points -= 10000
        elif asset.context_kind == "article_body":
            points += 500
        elif asset.context_kind == "article_header":
            points += 200
        elif asset.context_kind == "metadata":
            points += 100
        points += 25 * sum(1 for token in subject_tokens if token in text)
        if any(token in generic_tokens for token in re.split(r"[-_ .]+", stem)):
            points -= 300
        if asset.source_attribute.startswith(("meta", "jsonld", "link")):
            points += 10
        try:
            area = int(asset.width or 0) * int(asset.height or 0)
        except ValueError:
            area = 0
        return points, area, -len(path)

    return sorted(assets, key=score, reverse=True)[: max(1, int(limit))]


__all__ = ["PageAsset", "extract_page_assets", "rank_page_assets"]
