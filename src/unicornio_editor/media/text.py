"""Sanitizacao deterministica de texto para captions/créditos de imagem.

Garante que captions e créditos exibidos sao TEXTO PURO (sem tags HTML como
`<p>`, `<figure>`, `<figcaption>`). Um credit_text vindo do modelo pode
carregar markup (ex.: `<p>Credit: Nintendo</p>`) que, enviado cru ao WordPress
como caption/title ou inserido no content, renderiza tags quebradas. Aqui
removemos tudo e mantemos apenas o texto legivel.
"""

from __future__ import annotations

import html as _html
import re


def plain_text(value: str | None) -> str:
    """Remove tags HTML e decodifica entidades, retornando texto limpo.

    Conservador e deterministico:
      - remove <script>/<style> e seu conteudo;
      - remove TODAS as tags (deixa o texto interno);
      - decodifica entidades HTML (&amp; -> &, &lt;p&gt; -> <p> ...);
      - normaliza espacos/linhas.
    Nao altera o significado: mantem autores, nomes, licencas, URLs como texto.
    """
    if value is None:
        return ""
    text = str(value)
    # Remove blocos de script/style por completo.
    text = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1\s*>", " ", text)
    # Remove comentarios HTML.
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    # Remove tags, mantendo o texto interno.
    text = re.sub(r"<[^>]+>", " ", text)
    # Decodifica entidades (inclui &lt;p&gt; vindo de texto escapado).
    text = _html.unescape(text)
    # Normaliza espacos e quebras de linha.
    text = re.sub(r"[\s\u00a0]+", " ", text).strip()
    return text


_FIGURE_RE = re.compile(r"<figure\b[^>]*>.*?</figure>", re.IGNORECASE | re.DOTALL)


def dedupe_credit_figures(html: str) -> str:
    """Remove figuras ORFAS de credito duplicado (sem <img> cujo texto repete).

    No post de producao cada imagem vinha com DUAS figuras consecutivas:
    `<figure><img><figcaption>Credit...</figcaption></figure>` seguida de
    `<figure><figcaption>Credit...</figcaption></figure>` (sem img, repetindo
    o mesmo credito). Aqui removemos a figura orfa cujo figcaption (texto puro)
    ja aparece numa figura COM imagem do mesmo post.

    Conservador: so remove <figure> sem <img> e sem outras midia que repitam
    exatamente o mesmo texto de credito de uma figura com imagem.
    """
    if not isinstance(html, str):
        return ""
    if "<figcaption" not in html.lower():
        return html
    figures = _FIGURE_RE.findall(html)
    # Texto de credito das figuras que possuem imagem (sao as "donas").
    with_image_credits: set[str] = set()
    for fig in figures:
        if "<img" in fig:
            caption = _figcaption_text(fig)
            if caption:
                with_image_credits.add(caption)
    if not with_image_credits:
        return html

    def _maybe_drop(match: re.Match[str]) -> str:
        fig = match.group(0)
        if "<img" in fig:
            return fig  # nunca remove figura com imagem
        # Outras midia alem de <img> (iframe, video) tambem sao "donas".
        if re.search(r"<(iframe|video|picture|source)\b", fig, re.IGNORECASE):
            return fig
        caption = _figcaption_text(fig)
        if caption and caption in with_image_credits:
            return ""  # figura orfa duplicando um credito ja presente
        return fig

    return _FIGURE_RE.sub(_maybe_drop, html)


def _figcaption_text(figure: str) -> str:
    m = re.search(r"<figcaption\b[^>]*>(.*?)</figcaption>", figure, re.IGNORECASE | re.DOTALL)
    return plain_text(m.group(1)) if m else ""


__all__ = ["plain_text", "dedupe_credit_figures"]
