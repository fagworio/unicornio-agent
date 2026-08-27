"""Report-only maintenance diagnostics; this module cannot update WordPress."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any


_IMG_RE = re.compile(r'<img\b[^>]*?src=["\']([^"\']+)', re.IGNORECASE)
_LINK_RE = re.compile(r'<a\b[^>]*?href=["\']([^"\']+)', re.IGNORECASE)


def generate_report(
    posts: Iterable[Mapping[str, Any]],
    *,
    broken_urls: Iterable[str] = (),
    media_records: Iterable[Mapping[str, Any]] = (),
    min_inline_images: int = 1,
) -> list[dict[str, Any]]:
    broken = set(broken_urls)
    findings: list[dict[str, Any]] = []
    for post in posts:
        post_id = post.get("id")
        html = _raw(post)
        image_urls = _IMG_RE.findall(html)
        for url in image_urls:
            if url in broken:
                findings.append(_issue(post_id, "broken_image", url))
            if not url.lower().split("?", 1)[0].endswith(".webp"):
                findings.append(_issue(post_id, "legacy_image_format", url))
        for url in _LINK_RE.findall(html):
            if url in broken:
                findings.append(_issue(post_id, "broken_link", url))
        if "Confira mais novidades em nosso Portal de" not in html or "Fonte:" not in html:
            findings.append(_issue(post_id, "missing_cta_source"))
        meta = post.get("meta") if isinstance(post.get("meta"), Mapping) else {}
        title = meta.get("rank_math_title", "")
        description = meta.get("rank_math_description", "")
        if not isinstance(title, str) or not title.strip() or not isinstance(description, str) or not 120 <= len(description) <= 160:
            findings.append(_issue(post_id, "weak_seo"))
        if not post.get("featured_media"):
            findings.append(_issue(post_id, "missing_featured_media"))
        if len(image_urls) < min_inline_images:
            findings.append(_issue(post_id, "insufficient_media"))
    for media in media_records:
        if not media.get("post"):
            findings.append(_issue(media.get("id"), "orphan_media"))
    return sorted(findings, key=lambda item: (str(item.get("post_id")), item["code"], item.get("value", "")))


def _raw(post: Mapping[str, Any]) -> str:
    content = post.get("content")
    if isinstance(content, Mapping) and isinstance(content.get("raw"), str):
        return content["raw"]
    return ""


def _issue(post_id: Any, code: str, value: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"post_id": post_id, "code": code, "action": "report"}
    if value is not None:
        item["value"] = value
    return item
