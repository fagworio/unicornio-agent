"""Level-2 offline replay using deterministic local compose/quality code."""

from pathlib import Path
from typing import Any, cast

from ..content_quality import ContentQualityError, validate_content_quality
from ..html_cleaner import clean_html
from .replay import replay_snapshot


def _editorial(context: dict[str, Any], previous) -> dict[str, Any]:
    source = cast(dict[str, Any], context.get("editorial")) if isinstance(context.get("editorial"), dict) else {}
    result: dict[str, Any] = {"decision": "process"}
    result.update(source)
    return result


def _media(context: dict[str, Any], previous, editorial):
    return previous.media


def _compose(context: dict[str, Any], editorial: dict[str, Any], media):
    raw = context.get("content", "")
    if isinstance(raw, dict):
        raw = raw.get("raw", "")
    content = clean_html(str(raw or ""))
    return {"content": content, "title": context.get("title", {}), "editorial": editorial, "media": media}


def _validate(context: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    media = candidate["media"]
    if media.missing > 0:
        return {"passed": False, "failures": [{"gate": "imagens_no_corpo"}]}
    title = candidate.get("title", {})
    title_text = title.get("raw", "") if isinstance(title, dict) else str(title)
    editorial = candidate.get("editorial", {})
    try:
        validate_content_quality(
            candidate["content"],
            title=title_text,
            focus_keyword=str(editorial.get("focus_keyword", "")),
            matched_topics=editorial.get("matched_topics", []),
        )
    except ContentQualityError:
        return {"passed": False, "failures": [{"gate": "qualidade_texto"}]}
    return {"passed": True, "failures": []}


def replay_snapshot_level2(path: str | Path) -> dict[str, Any]:
    return replay_snapshot(path, {"editorial": _editorial, "media": _media, "compose": _compose, "validate": _validate})
