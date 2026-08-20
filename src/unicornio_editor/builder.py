"""Canonical CTA and source builder controlled by code, not the model."""

from __future__ import annotations

import re
from html import escape
from urllib.parse import urlparse


class BuilderError(ValueError):
    """Raised when canonical footer inputs are unsafe."""


_CTA = (
    '<hr />\n\n'
    '<h3>Confira mais novidades em nosso Portal de '
    '<a href="https://prod.unicorniohater.com.br/noticias/">Notícias!</a></h3>\n\n'
    '<hr />'
)


def append_canonical_footer(html: str, original_link: str | None) -> str:
    """Replace old generated footer and append exactly one canonical footer."""
    if not isinstance(html, str):
        raise TypeError("html must be a string")
    url = _validate_link(original_link) if original_link else None
    if _has_current_footer(html, url):
        return html
    cleaned = re.sub(
        r'<h3>\s*Confira mais novidades em nosso Portal de .*?</h3>',
        "",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    cleaned = re.sub(r'<em>\s*Fonte:\s*.*?</em>', "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(
        r'<a\b[^>]*>\s*(?:<strong>\s*)?<em>\s*Fonte\s*</em>\s*(?:</strong>\s*)?</a>',
        "",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )
    cleaned = re.sub(
        r"(?:\s*<hr\s*/?>\s*)+$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).rstrip()
    footer = _CTA
    if url:
        label = _source_label(url)
        footer += (
            "\n\n"
            f'<em>Fonte: <a href="{escape(url, quote=True)}" target="_blank" '
            f'rel="nofollow noopener">{escape(label)}</a>.</em>'
        )
    return f"{cleaned}\n\n{footer}" if cleaned else footer


def _validate_link(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BuilderError("original_link is required")
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise BuilderError("original_link must be an absolute HTTP(S) URL")
    return value


def _source_label(value: str) -> str:
    hostname = urlparse(value).hostname or "Fonte original"
    hostname = hostname.removeprefix("www.").split(".", 1)[0]
    return hostname[:1].upper() + hostname[1:]


def _has_current_footer(html: str, original_link: str | None) -> bool:
    if "Confira mais novidades em nosso Portal de " not in html:
        return False
    if original_link is None:
        return "Fonte:" not in html
    return f'href="{escape(original_link, quote=True)}" target="_blank" rel="nofollow noopener"' in html
