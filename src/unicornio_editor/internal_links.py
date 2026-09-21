"""Internal-link enrichment for UnicornioHater posts (deterministic, no LLM).

Every category term that unambiguously refers to a portal category is linked to
its canonical category URL on the FIRST natural occurrence, at most once per
URL, using a plain internal link (`<a href="...">text</a>` — follow, no
target=_blank, no rel=nofollow).

Why deterministic and not AI-driven: the map is static, the rules are lexical,
and applying it in code is free and never "drifts". Semantic/context-gated terms
(ex. bare "manga" when not about Japanese comics, "max" isolated, "teaser",
"análise", "crítica", "DC" isolated) are deliberately EXCLUDED here: a string
replacer cannot judge context, and a wrong link is worse than no link.

Safety guarantees (all enforced by code, not by prompt):
- Never links inside an existing `<a>`, heading (h1-h6), script or style.
- Never links in the middle of a word (unicode word-boundary match).
- Never repeats the same URL more than once per article.
- Only the most specific term matches at a position (longer/multi-word first),
  so "PlayStation 5" wins over "PlayStation" and "Nintendo Switch 2" over "Switch".
- Does not alter surrounding text or HTML; only inserts anchors around a match.
"""

from __future__ import annotations

import re
from html import escape
from urllib.parse import urlparse

# --- Internal link map (category URL -> unambiguous terms) -------------------
# Terms are listed MOST SPECIFIC first within each category (multi-word before
# single-word, longer before shorter) because the matcher uses alternation in
# order at each position. Terms requiring editorial context are NOT here.
_INTERNAL_LINK_MAP: list[tuple[str, list[str]]] = [
    # Streaming
    ("https://www.unicorniohater.com.br/amazon-prime-video/",
     ["Amazon Prime Video", "Prime Video", "streaming da Amazon"]),
    ("https://www.unicorniohater.com.br/apple-tv/",
     ["Apple TV+", "Apple TV Plus", "streaming da Apple"]),
    ("https://www.unicorniohater.com.br/disney/",
     ["Walt Disney", "Disney+", "Disney"]),
    ("https://www.unicorniohater.com.br/hbo-max/",
     ["HBO Max", "streaming da HBO"]),
    ("https://www.unicorniohater.com.br/hulu/", ["Hulu"]),
    ("https://www.unicorniohater.com.br/netflix/", ["Netflix"]),
    ("https://www.unicorniohater.com.br/paramount/",
     ["Paramount+", "Paramount +", "Paramount Plus"]),
    ("https://www.unicorniohater.com.br/star/", ["Star+", "Star Plus"]),
    # Entretenimento
    ("https://www.unicorniohater.com.br/animes/", ["animes", "anime"]),
    ("https://www.unicorniohater.com.br/desenhos/",
     ["desenhos animados", "desenho animado", "séries animadas", "série animada"]),
    ("https://www.unicorniohater.com.br/filmes/", ["filmes", "filme", "cinema"]),
    ("https://www.unicorniohater.com.br/manga/", ["mangá", "mangás"]),
    ("https://www.unicorniohater.com.br/quadrinhos/",
     ["histórias em quadrinhos", "quadrinhos", "quadrinho", "HQs", "HQ", "comics"]),
    ("https://www.unicorniohater.com.br/series/",
     ["séries de TV", "série de TV", "séries", "série"]),
    ("https://www.unicorniohater.com.br/trailers/", ["trailers", "trailer"]),
    ("https://www.unicorniohater.com.br/musica/", ["músicas", "música"]),
    # Universos e franquias
    ("https://www.unicorniohater.com.br/dc/", ["DC Comics", "Universo DC", "DCU"]),
    ("https://www.unicorniohater.com.br/marvel/",
     ["Marvel Comics", "Universo Marvel", "Marvel", "MCU"]),
    ("https://www.unicorniohater.com.br/star-wars/",
     ["Star Wars", "Guerra nas Estrelas"]),
    # Games
    ("https://www.unicorniohater.com.br/games/",
     ["jogos eletrônicos", "video games", "videogames", "games"]),
    ("https://www.unicorniohater.com.br/games/mobile/",
     ["jogos mobile", "games mobile", "jogos para celular"]),
    ("https://www.unicorniohater.com.br/games/nintendo-switch/",
     ["Nintendo Switch 2", "Switch 2", "Nintendo Switch"]),
    ("https://www.unicorniohater.com.br/games/pc/",
     ["jogos para PC", "games para PC", "PC gaming", "PC"]),
    ("https://www.unicorniohater.com.br/games/playstation/",
     ["PlayStation 6", "PlayStation 5", "PlayStation 4", "PlayStation", "Playstation", "PS6", "PS5", "PS4"]),
    ("https://www.unicorniohater.com.br/games/steam/", ["Steam Deck", "Steam"]),
    ("https://www.unicorniohater.com.br/games/x-box/",
     ["Xbox Series X", "Xbox Series S", "Xbox Series", "Xbox One", "X-Box", "Xbox"]),
    ("https://www.unicorniohater.com.br/games-retro/",
     ["retro gaming", "retrogames", "retro games", "jogos retrô", "jogos retro", "games retrô", "games retro"]),
    ("https://www.unicorniohater.com.br/emuladores/",
     ["emuladores", "emulador", "emulação", "emulator"]),
    ("https://www.unicorniohater.com.br/games/google-stadia/",
     ["Google Stadia", "Stadia"]),
    # Livros e leitura
    ("https://www.unicorniohater.com.br/livros/", ["livros", "livro", "literatura"]),
    ("https://www.unicorniohater.com.br/livros/leitura-casual/", ["Leitura Casual"]),
    # Ciência e tecnologia
    ("https://www.unicorniohater.com.br/ciencia/",
     ["científica", "científico", "cientistas", "ciência"]),
    ("https://www.unicorniohater.com.br/tecnologia/",
     ["tecnologia e inovação", "inovação", "tecnologia"]),
    # Conteúdo editorial (somente termos inequívocos)
    ("https://www.unicorniohater.com.br/flashback/", ["Flashback"]),
    ("https://www.unicorniohater.com.br/nostalgia/",
     ["nostálgica", "nostálgico", "nostalgia"]),
    ("https://www.unicorniohater.com.br/esportes/", ["esportes"]),
]

# Tags inside which we never insert a link.
_PROTECTED_TAGS = frozenset(
    {"a", "h1", "h2", "h3", "h4", "h5", "h6", "script", "style"}
)
_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
     "meta", "param", "source", "track", "wbr"}
)
_TAG_TOKEN_RE = re.compile(r"(<!--.*?-->|<[^>]+>)", re.DOTALL)
_TAG_NAME_RE = re.compile(r"^</?s*([a-zA-Z0-9]+)")


def _flatten_rules() -> list[tuple[str, str]]:
    """Flatten to (term, url) and sort by specificity (multi-word, then length)."""
    rules: list[tuple[str, str]] = []
    for url, terms in _INTERNAL_LINK_MAP:
        for term in terms:
            term = term.strip()
            if term:
                rules.append((term, url))
    # Multi-word first, then longer string first, then original order.
    rules.sort(key=lambda ru: (ru[0].count(" "), len(ru[0])), reverse=True)
    return rules


# term (casefolded) -> url, built once.
_TERM_TO_URL: dict[str, str] = {}
for _term, _url in _flatten_rules():
    _TERM_TO_URL.setdefault(_term.casefold(), _url)


def _term_pattern(term: str) -> str:
    """Word-boundary regex for one term (unicode word chars on both sides)."""
    return r"(?<!\w)" + re.escape(term) + r"(?!\w)"


# Full alternation, most specific first; case-insensitive.
_TERM_ALTERNATION = re.compile(
    "|".join(_term_pattern(t) for t, _ in _flatten_rules()),
    re.IGNORECASE,
)


def _link_anchor(term: str, url: str) -> str:
    """Internal follow link: no target=_blank, no rel=nofollow."""
    return f'<a href="{escape(url, quote=True)}">{escape(term)}</a>'


def _is_tag(token: str) -> bool:
    return token.startswith("<")


def _tag_name(token: str) -> str | None:
    m = _TAG_NAME_RE.match(token)
    return m.group(1).lower() if m else None


def _is_closing(token: str) -> bool:
    return bool(re.match(r"^</", token))


_A_HREF_RE = re.compile(
    r'<a\b[^>]*\bhref=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL
)

# Hosts do próprio portal (host vazio = URL relativa "/categoria/").
_INTERNAL_HOSTS = frozenset({"unicorniohater.com.br", ""})


def _canonical_url(url: str) -> str:
    """Forma canônica de URL interna (Fase 15).

    ``/series``, ``/series/``, http/https e www/non-www apontam para a MESMA
    página: sem canonicalizar, "uma URL interna no máximo uma vez" não vale
    (o contador trataria a mesma categoria como duas).
    """
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if not path.endswith("/"):
        path += "/"
    # URL relativa ("/series/") e URL absoluta do próprio site apontam para a
    # MESMA página: o que identifica a categoria interna é o PATH. Sem isso o
    # link relativo não era reconhecido e o contador deixava passar um segundo
    # link para a mesma categoria.
    if host in _INTERNAL_HOSTS:
        return path.lower()
    return f"{host}{path}".lower()


# Conjunto canônico das URLs internas administradas pelo mapa.
_INTERNAL_CANON: frozenset[str] = frozenset(
    _canonical_url(url) for url, _terms in _INTERNAL_LINK_MAP
)


def add_internal_links(html: str, *, max_per_url: int = 1, repair_duplicates: bool = True) -> str:
    """Insert internal category links into `html` — deterministic and IDEMPOTENT.

    Fase 15 do documento de correções:

    * **uma URL interna no máximo uma vez por artigo** — contando também os
      links que JÁ existem no HTML. Antes o contador era local à chamada, então
      um segundo parágrafo podia ganhar outro link para a MESMA categoria;
    * **reparo de conteúdo antigo**: quando a mesma URL aparece duas vezes,
      preserva o PRIMEIRO link e devolve os demais ao texto (sem âncora);
    * **idempotência**: ``f(f(html)) == f(html)``.

    Só texto fora de tags protegidas (``<a>``, headings, script, style) é
    processado, em limites de palavra, primeira ocorrência por URL, termo mais
    específico primeiro. Inserção pura — nunca altera o texto ao redor.
    """
    if not isinstance(html, str) or not html.strip():
        return html

    used_urls: dict[str, int] = {}

    # 1) Conta (e canonicaliza) os links internos JÁ presentes no documento.
    def _contar(match: re.Match[str]) -> str:
        canon = _canonical_url(match.group(1))
        if canon in _INTERNAL_CANON:
            used_urls[canon] = used_urls.get(canon, 0) + 1
        return match.group(0)

    html = _A_HREF_RE.sub(_contar, html)

    # 2) Reparo: a partir da segunda ocorrência da mesma URL, devolve o texto.
    if repair_duplicates:
        vistos: set[str] = set()

        def _reparar(match: re.Match[str]) -> str:
            canon = _canonical_url(match.group(1))
            if canon not in _INTERNAL_CANON:
                return match.group(0)
            if canon in vistos:
                return match.group(2)  # mantém o texto, remove a âncora repetida
            vistos.add(canon)
            return match.group(0)

        html = _A_HREF_RE.sub(_reparar, html)

    def _linkify(text: str) -> str:
        if not text:
            return text

        def _repl(match: re.Match[str]) -> str:
            term = match.group(0)
            url = _TERM_TO_URL.get(term.casefold())
            if url is None:
                return term
            canon = _canonical_url(url)
            if used_urls.get(canon, 0) >= max_per_url:
                return term  # keep text; URL already used in this article
            used_urls[canon] = used_urls.get(canon, 0) + 1
            return _link_anchor(term, url)

        return _TERM_ALTERNATION.sub(_repl, text)

    # Tokenize HTML into [text, tag, text, tag, ...] and rebuild, skipping
    # text that sits inside a protected tag.
    tokens = _TAG_TOKEN_RE.split(html)
    stack: list[str] = []
    out: list[str] = []
    for token in tokens:
        if not token:
            continue
        if not _is_tag(token):
            protected = any(tag in _PROTECTED_TAGS for tag in stack)
            out.append(token if protected else _linkify(token))
            continue
        # Tag token: update protection stack, emit unchanged.
        name = _tag_name(token)
        if _is_closing(token):
            if name in stack:
                stack.pop()
        elif name is not None and name not in _VOID_TAGS and not token.rstrip().endswith("/>"):
            stack.append(name)
        out.append(token)
    return "".join(out)


__all__ = ["add_internal_links", "_INTERNAL_LINK_MAP"]
