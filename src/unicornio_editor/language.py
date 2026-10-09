"""Deterministic, conservative language checks for editorial text.

This is intentionally a small local heuristic.  It is used to decide whether
localization is necessary, not to translate text.  Proper names, URLs and
short snippets remain ``uncertain`` instead of triggering an unnecessary LLM
rewrite.
"""

from __future__ import annotations

import re
import json
from html import unescape
from html.parser import HTMLParser
from math import ceil
from typing import Any


_TOKEN_RE = re.compile(r"[A-Za-zÀ-ÿ]+(?:['’][A-Za-zÀ-ÿ]+)?")
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")

# Distinctive function words and common editorial forms.  Ambiguous words
# such as ``a``, ``as``, ``in`` and ``game`` are deliberately excluded.
_PT_MARKERS = frozenset(
    "de que para com uma um umas uns dos das do em por não nao como foi são sao está esta estão estao isso seu sua seus suas pela pelo pelas pelos nas nos numa num mais sobre depois entre também tambem quando onde ainda muito notícia noticia notícias noticias jogo jogos lançamento lancamento recebeu recebe ganha ganhou nova novo primeiro primeira segundo segundo confirmou revela revelou segundo segundo fontes segundo segundo".split()
)
_EN_MARKERS = frozenset(
    "the and of to in for with this that from has have had was were will would are is its their about after before latest news release released announced announces reveals revealed players player first new game games according confirmed confirms sources into during season episode movie film show".split()
)


class _EditorialBlockParser(HTMLParser):
    """Collect editorial paragraphs while excluding operational markup."""

    _BLOCK_TAGS = frozenset({"p", "h1", "h2", "h3", "h4", "h5", "h6"})
    _IGNORED_TAGS = frozenset({"script", "style", "pre", "code", "figcaption"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict[str, Any]] = []
        self._stack: list[str] = []
        self._current: dict[str, Any] | None = None
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        self._stack.append(tag)
        if tag in self._IGNORED_TAGS:
            self._ignored_depth += 1
        if tag in self._BLOCK_TAGS and self._current is None and self._ignored_depth == 0:
            self._current = {"tag": tag, "quote": "blockquote" in self._stack, "parts": []}

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self._current is not None and tag == self._current["tag"]:
            text = " ".join(str(part) for part in self._current["parts"])
            self.blocks.append({"text": text, "quote": bool(self._current["quote"])})
            self._current = None
        if tag in self._IGNORED_TAGS and self._ignored_depth:
            self._ignored_depth -= 1
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index] == tag:
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self._current is not None and self._ignored_depth == 0:
            self._current["parts"].append(data)


def editorial_blocks(value: str) -> list[dict[str, Any]]:
    """Return visible paragraph/heading blocks and quote provenance."""
    parser = _EditorialBlockParser()
    try:
        parser.feed(str(value or ""))
        parser.close()
    except Exception:  # noqa: BLE001 - language checks must remain fail-soft
        return []
    return [
        {"text": visible_text(str(block.get("text") or "")), "quote": bool(block.get("quote"))}
        for block in parser.blocks
        if visible_text(str(block.get("text") or "")).strip()
    ]


def visible_text(value: str) -> str:
    """Remove markup, URLs and code-like blocks before language scoring."""
    text = str(value or "")
    text = re.sub(r"<(?:script|style|pre|code)\b[^>]*>.*?</(?:script|style|pre|code)>", " ", text, flags=re.I | re.S)
    text = _URL_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    return unescape(text)


def detect_language(value: str, *, minimum_words: int = 18) -> dict[str, Any]:
    """Return ``pt-BR``, ``en``, ``mixed`` or ``uncertain`` with evidence."""
    text = visible_text(value)
    tokens = [token.casefold() for token in _TOKEN_RE.findall(text)]
    pt_hits = [token for token in tokens if token in _PT_MARKERS]
    en_hits = [token for token in tokens if token in _EN_MARKERS]
    total_hits = len(pt_hits) + len(en_hits)
    result: dict[str, Any] = {
        "language": "uncertain",
        "confidence": 0.0,
        "word_count": len(tokens),
        "pt_markers": sorted(set(pt_hits))[:12],
        "en_markers": sorted(set(en_hits))[:12],
    }
    if len(tokens) < minimum_words or total_hits < 3:
        result["confidence"] = round(min(0.59, total_hits / 10), 3)
        return result
    pt_ratio = len(pt_hits) / total_hits
    en_ratio = len(en_hits) / total_hits
    if len(pt_hits) >= 3 and pt_ratio >= 0.68 and pt_ratio - en_ratio >= 0.25:
        result["language"] = "pt-BR"
        result["confidence"] = round(min(0.99, 0.55 + (pt_ratio - en_ratio) * 0.6), 3)
    elif len(en_hits) >= 3 and en_ratio >= 0.68 and en_ratio - pt_ratio >= 0.25:
        result["language"] = "en"
        result["confidence"] = round(min(0.99, 0.55 + (en_ratio - pt_ratio) * 0.6), 3)
    elif len(pt_hits) >= 2 and len(en_hits) >= 2:
        result["language"] = "mixed"
        result["confidence"] = round(min(0.85, 0.5 + min(pt_ratio, en_ratio) * 0.5), 3)
    else:
        result["confidence"] = round(min(0.65, max(pt_ratio, en_ratio)), 3)
    return result


def editorial_language_report(
    *,
    title: str,
    content: str,
    seo_title: str = "",
    meta_description: str = "",
) -> dict[str, Any]:
    """Assess final editorial fields without triggering any provider call."""
    fields = {
        "title": detect_language(title, minimum_words=4),
        "content": detect_language(content),
        "seo_title": detect_language(seo_title, minimum_words=5),
        "meta_description": detect_language(meta_description, minimum_words=8),
    }
    body = fields["content"]
    blocks = editorial_blocks(content)
    paragraph_reports: list[dict[str, Any]] = []
    for block in blocks:
        report = detect_language(str(block["text"]), minimum_words=12)
        paragraph_reports.append({
            "text_preview": str(block["text"])[:160],
            "quoted": bool(block["quote"]),
            **report,
        })
    english_editorial_paragraphs = [
        item for item in paragraph_reports
        if not item["quoted"]
        and item["language"] == "en"
        and float(item["confidence"]) >= 0.7
        and int(item["word_count"]) >= 12
    ]
    english_fields = [name for name, value in fields.items() if value["language"] == "en"]
    failing_fields = []
    if body["language"] == "en" and body["confidence"] >= 0.7:
        failing_fields.append("content")
    nonquoted_paragraphs = [item for item in paragraph_reports if not item["quoted"]]
    paragraph_threshold = max(2, ceil(len(nonquoted_paragraphs) * 0.4))
    if (
        len(english_editorial_paragraphs) >= paragraph_threshold
        and len(english_editorial_paragraphs) >= 2
        and "content" not in failing_fields
    ):
        failing_fields.append("content")
    english_seo_fields = [
        name for name in ("seo_title", "meta_description")
        if fields[name]["language"] == "en" and fields[name]["confidence"] >= 0.78
    ]
    for name in ("seo_title", "meta_description"):
        value = fields[name]
        # A Portuguese article may legitimately retain an English work/brand
        # name in SEO title. Require body evidence or two independent English
        # SEO fields before blocking on metadata alone.
        if (
            value["language"] == "en"
            and value["confidence"] >= 0.78
            and (body["language"] != "pt-BR" or len(english_seo_fields) >= 2)
        ):
            failing_fields.append(name)
    # A title alone can be an official work name.  Require body evidence or
    # two independent SEO/editorial fields before blocking a READY state.
    passed = not failing_fields
    languages = [value["language"] for value in fields.values()]
    if "en" in languages and "pt-BR" in languages:
        final_language = "mixed"
    elif body["language"] == "pt-BR":
        final_language = "pt-BR"
    elif body["language"] == "en":
        final_language = "en"
    else:
        final_language = "uncertain"
    confidence = max(float(value["confidence"]) for value in fields.values())
    return {
        "language": final_language,
        "confidence": round(confidence, 3),
        "passed": passed,
        "failing_fields": failing_fields,
        "english_fields": english_fields,
        "paragraphs": paragraph_reports,
        "editorial_paragraph_count": len(nonquoted_paragraphs),
        "english_editorial_paragraphs": len(english_editorial_paragraphs),
        "fields": fields,
    }


def localization_required(report: dict[str, Any] | None) -> bool:
    """Only unequivocal full-article English activates localization."""
    return bool(
        isinstance(report, dict)
        and report.get("language") == "en"
        and float(report.get("confidence") or 0) >= 0.7
    )


def audit_published_language(client: Any, post_ids: list[int] | tuple[int, ...]) -> dict[str, Any]:
    """Read-only language audit for an explicit set of published posts."""
    rows: list[dict[str, Any]] = []
    for post_id in post_ids:
        post = client.get_post(int(post_id))
        title_payload = post.get("title") or {}
        content_payload = post.get("content") or {}
        meta = post.get("meta") if isinstance(post.get("meta"), dict) else {}
        seo_title = str(meta.get("rank_math_title") or meta.get("_rank_math_title") or "")
        meta_description = str(
            meta.get("rank_math_description") or meta.get("_rank_math_description") or ""
        )
        state_raw = meta.get("_hermes_work_state")
        state: Any = None
        if isinstance(state_raw, str):
            try:
                state = json.loads(state_raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                state = None
        report = editorial_language_report(
            title=str(title_payload.get("raw") or title_payload.get("rendered") or ""),
            content=str(content_payload.get("raw") or content_payload.get("rendered") or ""),
            seo_title=seo_title,
            meta_description=meta_description,
        )
        rows.append({
            "post_id": int(post_id),
            "status": post.get("status"),
            "link": post.get("link"),
            "v2_state": state.get("state") if isinstance(state, dict) else None,
            "language": report,
            "needs_localization": not bool(report.get("passed")),
            "recommended_action": "review_diff_before_any_write" if not report.get("passed") else "no_change",
        })
    return {"read_only": True, "post_ids": [int(item) for item in post_ids], "posts": rows}


__all__ = [
    "detect_language",
    "editorial_blocks",
    "editorial_language_report",
    "audit_published_language",
    "localization_required",
    "visible_text",
]
