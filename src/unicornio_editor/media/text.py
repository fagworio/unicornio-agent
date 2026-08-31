"""Sanitizacao deterministica de texto para captions/créditos de imagem.

Garante que captions e créditos exibidos sao TEXTO PURO (sem tags HTML). Um
credit_text vindo do modelo pode carregar markup (ex.: <p>Credit: Nintendo</p>)
que, enviado cru ao WordPress como caption/title ou inserido no content,
renderiza tags quebradas. Aqui removemos tudo e mantemos apenas o texto legivel.
"""

from __future__ import annotations

import html as _html
import re


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


__all__ = ["plain_text", "sanitize_title", "dedupe_credit_figures"]
