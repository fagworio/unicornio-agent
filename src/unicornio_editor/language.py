"""Deterministic, conservative language checks for editorial text.

This is intentionally a small local heuristic.  It is used to decide whether
localization is necessary, not to translate text.  Proper names, URLs and
short snippets remain ``uncertain`` instead of triggering an unnecessary LLM
rewrite.
"""

from __future__ import annotations

import re
from html import unescape
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
    english_fields = [name for name, value in fields.items() if value["language"] == "en"]
    failing_fields = []
    if body["language"] == "en" and body["confidence"] >= 0.7:
        failing_fields.append("content")
    for name in ("seo_title", "meta_description"):
        value = fields[name]
        if value["language"] == "en" and value["confidence"] >= 0.78:
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
        "fields": fields,
    }


def localization_required(report: dict[str, Any] | None) -> bool:
    """Only unequivocal full-article English activates localization."""
    return bool(
        isinstance(report, dict)
        and report.get("language") == "en"
        and float(report.get("confidence") or 0) >= 0.7
    )


__all__ = ["detect_language", "editorial_language_report", "localization_required", "visible_text"]
