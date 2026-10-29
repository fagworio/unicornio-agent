"""Sanitizacao deterministica de texto para captions/créditos de imagem.

Garante que captions e créditos exibidos sao TEXTO PURO (sem tags HTML). Um
credit_text vindo do modelo pode carregar markup (ex.: <p>Credit: Nintendo</p>)
que, enviado cru ao WordPress como caption/title ou inserido no content,
renderiza tags quebradas. Aqui removemos tudo e mantemos apenas o texto legivel.
"""

from __future__ import annotations

import html as _html
import ipaddress
import re
import unicodedata
from urllib.parse import urlsplit


def plain_text(value):
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1\s*>", " ", text)
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = _html.unescape(text)
    text = re.sub(r"[\s\u00a0]+", " ", text).strip()
    return text


def sanitize_title(value):
    """Normalize a WordPress title to plain text before editorial decisions.

    Imported titles sometimes retain HTML entities, including the permissive
    legacy spelling ``&amp`` without a trailing semicolon. ``html.unescape``
    handles both forms, so titles such as ``Heart &amp; Soul`` are matched and
    displayed as ``Heart & Soul``.
    """
    return plain_text(value)


_COMMON_SECOND_LEVEL_DOMAINS = {
    "ac", "asn", "co", "com", "edu", "firm", "gen", "go", "gov",
    "id", "ind", "mil", "net", "ne", "or", "org", "pro",
}


def source_site_name(source_page_url: str) -> str:
    """Derive a display label from the verified source page hostname only.

    This labels the website that supplied the page; it does not identify an
    image author or imply ownership or permission. Query strings, paths,
    credentials, and image-CDN hostnames are deliberately not used.
    """
    try:
        parsed = urlsplit(str(source_page_url or "").strip())
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return ""
        host = parsed.hostname.rstrip(".").casefold()
    except (TypeError, ValueError):
        return ""
    if not host:
        return ""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass

    labels = [label for label in host.split(".") if label]
    if not labels:
        return ""
    registrable_label = labels[-1] if len(labels) == 1 else labels[-2]
    if (
        len(labels) >= 3
        and len(labels[-1]) == 2
        and labels[-2] in _COMMON_SECOND_LEVEL_DOMAINS
    ):
        registrable_label = labels[-3]
    try:
        registrable_label = registrable_label.encode("ascii").decode("idna")
    except UnicodeError:
        pass
    words = [word for word in re.split(r"[-_]+", registrable_label) if word]
    return " ".join(word.upper() if len(word) <= 3 else word.capitalize() for word in words)


def credit_text_with_source_site(credit_text: str, source_page_url: str) -> str:
    """Preserve an existing caption and add one factual source-site credit.

    The generated line names only the host of the verified source page. It
    never fabricates an author's name, license, or legal authorization.
    """
    return credit_text_with_source_name(credit_text, source_site_name(source_page_url))


def credit_text_with_source_name(credit_text: str, site: str) -> str:
    """Add a source-site label already reduced to safe display text."""
    existing = plain_text(credit_text)
    site = plain_text(site)
    if not site:
        return existing
    # Site names are derived from host labels, but keep this formatter safe
    # when callers pass a persisted or externally assembled display value.
    site = re.sub(r"[^\w .&'-]", "", site).strip(" .")
    if not site:
        return existing
    normalized_site = unicodedata.normalize("NFKD", site).encode("ascii", "ignore").decode().casefold()
    normalized_site = re.sub(r"[^a-z0-9]+", " ", normalized_site).strip()
    if normalized_site:
        for clause in re.split(r"[.;\n]+", existing):
            normalized_clause = unicodedata.normalize("NFKD", clause).encode("ascii", "ignore").decode().casefold()
            normalized_clause = re.sub(r"[^a-z0-9]+", " ", normalized_clause).strip()
            has_credit_marker = bool(
                re.search(r"\b(?:credit|credito|fonte|source|via)\b", normalized_clause)
            )
            if (
                has_credit_marker
                and f" {normalized_site} " in f" {normalized_clause} "
            ):
                return existing
    source_credit = f"Crédito da imagem: {site}."
    return f"{existing} {source_credit}" if existing else source_credit


_FIGURE_RE = re.compile(r"<figure\b[^>]*>.*?</figure>", re.IGNORECASE | re.DOTALL)


def _figcaption_text(figure):
    m = re.search(r"<figcaption\b[^>]*>(.*?)</figcaption>", figure, re.IGNORECASE | re.DOTALL)
    return plain_text(m.group(1)) if m else ""


def dedupe_credit_figures(html):
    """Remove figuras ORFAS de credito duplicado (sem <img> cujo texto repete).

    No post de producao cada imagem vinha com DUAS figuras consecutivas:
    <figure><img><figcaption>Credit...</figcaption></figure> seguida de
    <figure><figcaption>Credit...</figcaption></figure> (sem img, repetindo o
    mesmo credito). Aqui removemos a figura orfa cujo figcaption (texto puro)
    ja aparece numa figura COM imagem do mesmo post.
    """
    if not isinstance(html, str):
        return ""
    if "<figcaption" not in html.lower():
        return html
    figures = _FIGURE_RE.findall(html)
    with_image_credits = set()
    for fig in figures:
        if "<img" in fig:
            caption = _figcaption_text(fig)
            if caption:
                with_image_credits.add(caption)
    if not with_image_credits:
        return html

    def _maybe_drop(match):
        fig = match.group(0)
        if "<img" in fig:
            return fig
        if re.search(r"<(iframe|video|picture|source)\b", fig, re.IGNORECASE):
            return fig
        caption = _figcaption_text(fig)
        if caption and caption in with_image_credits:
            return ""
        return fig

    return _FIGURE_RE.sub(_maybe_drop, html)


__all__ = [
    "plain_text", "sanitize_title", "dedupe_credit_figures",
    "source_site_name", "credit_text_with_source_site", "credit_text_with_source_name",
]
