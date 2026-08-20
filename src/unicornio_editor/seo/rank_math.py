"""Rank Math post-meta mapping."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class RankMathError(ValueError):
    """Raised when SEO data cannot be mapped safely."""


def build_meta(seo: Mapping[str, Any], existing: Mapping[str, Any] | None = None) -> dict[str, Any]:
    expected = {"title", "meta_description", "focus_keyword"}
    if not isinstance(seo, Mapping) or set(seo) != expected:
        raise RankMathError("SEO payload must contain title, meta_description and focus_keyword only")
    title = _string(seo["title"], "title")
    description = _string(seo["meta_description"], "meta_description")
    keyword = _string(seo["focus_keyword"], "focus_keyword")
    if len(title) > 65:
        raise RankMathError("Rank Math title must contain at most 65 characters")
    if not 120 <= len(description) <= 160:
        raise RankMathError("Rank Math description must contain 120 to 160 characters")
    result = dict(existing or {})
    result.update(
        {
            "rank_math_title": title,
            "rank_math_description": description,
            "rank_math_focus_keyword": keyword,
        }
    )
    return result


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RankMathError(f"SEO {name} must be a non-empty string")
    return value.strip()
