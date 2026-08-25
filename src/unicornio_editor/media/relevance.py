"""Deterministic image-content relevance gate (no vision, no LLM).

The editorial policy requires every image to represent the exact object,
character, work or subject cited in the post — never a generic concept that
merely shares a keyword (a real bat is NOT evidence for a game vampire; a
convention crowd is NOT evidence for a specific anime).

This module extracts the *distinctive* entities of a post (work/game names,
proper nouns, quoted titles) and rejects any image candidate whose alt text,
credit text or source URL has zero overlap with them. Concept words (vampiro,
jogo, anime, convencao, ...) never count as evidence, so a keyword match on a
generic concept cannot sneak an unrelated image in. Fail-closed: when in
doubt, reject — the editorial rule is "no image beats a wrong image".
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from html import unescape
from urllib.parse import urlparse

# Generic concept/context words that must NOT count as relevance evidence.
# A candidate matching ONLY these is by definition not about the cited work.
CONCEPT_WORDS = frozenset(
    {
        # creatures / archetypes that substitute for specific works
        "vampiro", "vampire", "vampira", "vampiros", "vampires", "morcego",
        "morcegos", "bat", "bats", "lobo", "lobos", "wolf", "wolves",
        "zumbi", "zumbis", "zombie", "zombies", "fantasma", "fantasmas",
        "ghost", "ghosts", "demonio", "demônio", "demonios", "demônios",
        "demon", "demons", "monstro", "monstros", "monster", "monsters",
        "criatura", "criaturas", "creature", "creatures", "bruxa", "bruxas",
        "witch", "witches", "dragao", "dragão", "dragoes", "dragões",
        "dragon", "dragons", "heroi", "herói", "herois", "heróis", "hero",
        "heroes", "vilao", "vilão", "viloes", "vilões", "villain", "villains",
        "cavaleiro", "cavaleiros", "knight", "knights", "guerreiro",
        "guerreiros", "warrior", "warriors", "ninja", "ninjas", "samurai",
        "samurais",
        # media genres / formats
        "jogo", "jogos", "game", "games", "videogame", "videojogo", "gamer",
        "anime", "animes", "manga", "mangá", "mangas", "mangás", "quadrinho",
        "quadrinhos", "comic", "comics", "hq", "bd", "filme", "filmes",
        "movie", "movies", "serie", "série", "series", "show", "shows",
        "temporada", "temporadas", "season", "seasons", "episodio",
        "episódio", "episodios", "episódios", "episode", "episodes",
        "capitulo", "capítulo", "capitulos", "capítulos", "chapter",
        "chapters", "trailer", "trailers", "teaser", "teasers", "visual",
        "keyart", "key art", "gameplay", "screenshot", "screenshots",
        "captura", "capturas", "screen", "cena", "cenas", "scene", "scenes",
        "arte", "artes", "art", "artwork", "artworks", "wallpaper",
        "wallpapers", "capa", "capas", "cover", "covers", "poster", "posters",
        "pôster", "pôsteres", "logo", "logos", "banner", "banners", "imagem",
        "imagens", "image", "images", "foto", "fotos", "photo", "photos",
        "fotografia", "fotografias",
        # generic scene / context nouns
        "evento", "eventos", "convencao", "convenção", "convencoes",
        "convenções", "feira", "feiras", "publico", "público", "publicos",
        "públicos", "audiencia", "audiência", "audiencias", "audiências",
        "mercado", "mercados", "market", "markets", "livraria", "livrarias",
        "loja", "lojas", "store", "stores", "secao", "seção", "secoes",
        "seções", "prateleira", "prateleiras", "shelf", "fa", "fã", "fas",
        "fãs", "fans", "comunidade", "comunidades", "community", "communities",
        "cultura", "culturas", "culture", "cultures", "geek", "geeks", "nerd",
        "nerds", "otaku", "otakus", "estande", "estandes", "stand", "stands",
        "noticia", "notícia", "noticias", "notícias", "news", "novidade",
        "novidades", "lancamento", "lançamento", "lancamentos", "lançamentos",
        "estreia", "estreias",
        # news verbs / filler that appear in titles
        "ganha", "ganham", "chega", "chegam", "revela", "revelam", "estreia",
        "anuncia", "anunciam", "mostra", "mostram", "confirma", "confirmam",
        "recebe", "recebem", "vai", "vao", "vão", "sera", "será", "foi", "sao",
        "são", "esta", "está", "tem", "traz", "trazem", "abre", "encerra",
        "ultrapassa", "supera", "bate", "atinge", "celebra", "marca", "destaca",
        "explora", "analisa", "compara", "lista", "elege", "escolhe", "reune",
        "reúne", "novo", "nova", "novos", "novas", "primeiro", "primeira",
        "melhores", "melhor", "top", "mais", "menos", "sobre", "apos", "após",
        "depois", "antes", "semana", "semana", "ano", "anos", "dia", "dias",
        "aproxima", "aproximam", "prepara", "segue", "continua", "mantem",
        "mantém", "promete", "deve", "pode", "quer", "faz", "fazem", "apresenta",
        "espera", "aguarda", "revelado", "anunciado", "confirmado", "lancado",
        "lançado", "chegado", "final", "grande", "especial", "proxima", "próxima",
        "ultimo", "último", "ultima", "última", "historia", "história", "histórias",
    }
)

# Common Portuguese/English words that carry no distinctive identity.
STOPWORDS = frozenset(
    {
        "a", "o", "as", "os", "um", "uma", "uns", "umas", "de", "da", "do", "das",
        "dos", "em", "no", "na", "nos", "nas", "para", "por", "com", "sem", "que",
        "e", "é", "ou", "se", "ao", "aos", "à", "às", "como", "quando", "onde",
        "qual", "quais", "este", "esta", "estes", "estas", "esse", "essa", "esses",
        "essas", "aquele", "aquela", "seu", "sua", "seus", "suas", "the", "a", "an",
        "of", "to", "in", "on", "at", "for", "with", "and", "or", "from", "by",
        "new", "next", "all", "best", "top", "de", "la", "el", "los", "das", "der",
        "die", "und", "não", "nao", "tambem", "também", "ainda", "ja", "já", "ate",
        "até", "entre", "contra", "durante", "segundo", "sobre", "cada", "todo",
        "toda", "todos", "todas", "outro", "outra", "outros", "outras", "muito",
        "muita", "muitos", "muitas", "pouco", "pouca", "poucos", "poucas",
    }
)

_QUOTE_RE = re.compile(r"[“”\"'«»]([^“”\"'«»]{2,80})[“”\"'«»]")
_TAG_RE = re.compile(r"<[^>]+>")
_H2_RE = re.compile(r"<h2\b[^>]*>(.*?)</h2>", re.IGNORECASE | re.DOTALL)
_FIGURE_RE = re.compile(r"<figure\b[^>]*>(.*?)</figure>", re.IGNORECASE | re.DOTALL)
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_IMG_ATTR_RE = re.compile(r'\b(src|alt)="([^"]*)"', re.IGNORECASE)
_FIGCAPTION_RE = re.compile(
    r"<figcaption\b[^>]*>(.*?)</figcaption>", re.IGNORECASE | re.DOTALL
)


def iter_content_images(content: str) -> list[dict[str, str]]:
    """Extract every inline image with its alt and figcaption credit text."""
    images: list[dict[str, str]] = []

    def _extract(tag_html: str) -> dict[str, str]:
        attrs = dict(_IMG_ATTR_RE.findall(tag_html))
        return {"src": attrs.get("src", ""), "alt": attrs.get("alt", "")}

    for figure in _FIGURE_RE.findall(content or ""):
        tag = _IMG_TAG_RE.search(figure)
        if not tag:
            continue
        item = _extract(tag.group(0))
        caption = _FIGCAPTION_RE.search(figure)
        if caption:
            item["caption"] = re.sub(_TAG_RE, "", caption.group(1)).strip()
        images.append(item)
    remainder = _FIGURE_RE.sub("", content or "")
    for tag in _IMG_TAG_RE.findall(remainder):
        images.append(_extract(tag))
    return images


def normalize(text: str) -> str:
    """Lowercase and strip diacritics so matching survives accents/encoding."""
    decomposed = unicodedata.normalize("NFD", unescape(text or "").lower())
    return decomposed.encode("ascii", "ignore").decode("ascii")


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", text) if len(token) >= 3}


def extract_entities(
    *,
    title: str,
    content_html: str = "",
    focus_keyword: str = "",
    game_name: str | None = None,
) -> set[str]:
    """Distinctive entities of the post: phrases and tokens that an image
    must reference to be considered related to the cited subject.

    Concept words and stopwords are excluded; generic keyword matches never
    count. Quoted strings in the content (e.g. ``"South of Midnight"``) and
    the game name are kept as full phrases.
    """
    entities: set[str] = set()
    raw_phrases: list[str] = []
    if game_name and game_name.strip():
        raw_phrases.append(game_name.strip())
    if focus_keyword and focus_keyword.strip():
        raw_phrases.append(focus_keyword.strip())
    if title and title.strip():
        raw_phrases.append(title.strip())
    # Search quoted names only in the visible TEXT (tags removed) — HTML
    # attributes like alt="..." or href="..." would otherwise leak as fake
    # entities (e.g. ' alt=', ' target=').
    quote_source = _TAG_RE.sub(" ", content_html or "")
    raw_phrases.extend(phrase for phrase in _QUOTE_RE.findall(quote_source) if "=" not in phrase)
    # Listicle H2s name the cited works ("1. Tokyo Ghoul: ...", "3. The
    # Sinking City 2: ..."); without them a list post with a generic title
    # ("7 novos jogos chegam esta semana...") could never match ANY image,
    # because the works only appear in the section headings. Keep the head
    # of each H2 (up to the first separator) and strip leading numbers, so
    # "3. The Sinking City 2: terror em Arkham" yields "the sinking city 2".
    for h2 in _H2_RE.findall(content_html or ""):
        head = re.split(r"[:—–|]", _TAG_RE.sub(" ", h2), maxsplit=1)[0]
        head = re.sub(r"^\s*\d+[.)]?\s*", "", head).strip()
        if head:
            raw_phrases.append(head)

    for phrase in raw_phrases:
        normalized = normalize(phrase)
        tokens = _tokens(normalized)
        if not tokens:
            continue
        meaningful = tokens - CONCEPT_WORDS - STOPWORDS
        if meaningful:
            # The full phrase is the strongest signal (e.g. "oshi no ko",
            # "south of midnight", "redfall").
            entities.add(normalized)
            entities.update(meaningful)
    return {entity for entity in entities if len(entity) >= 3}


def image_is_relevant(
    *,
    alt_text: str = "",
    credit_text: str = "",
    source_url: str = "",
    search_query: str = "",
    entities: Iterable[str],
    source_only: bool = False,
) -> bool:
    """True when the candidate image references at least one distinctive
    entity of the post (alt, credit, source URL or discovery query).

    ``source_only=True`` ignores the agent-written alt/credit and requires the
    real source URL (file/page name) itself to reference the subject. It is
    used for FEATURED candidates: a credit line can decorate a wrong image
    (e.g. a Disney castle captioned "presente em Kingdom Hearts"), but the
    file name of a true key art carries the game/work name. Featured images
    must depict the cited subject itself, never a tangential symbol.

    ``search_query`` is the discovery query that returned this image (e.g.
    "redfall xbox series"). It mirrors the editor's manual flow: if a
    filtered image search returned the candidate, it is the subject sought.
    For featured, the query is only additional evidence — the real source
    URL must still be present, so a fabricated query cannot smuggle a wrong
    image past the gate on its own.
    """
    entity_set = {normalize(entity) for entity in entities if entity}
    if not entity_set:
        return False
    if source_only:
        # Featured: a real source URL must always be present. The query is
        # additional evidence on top of it — it can rescue a generic
        # filename (header.jpg) but can never smuggle an image with no
        # source at all.
        if not str(source_url or "").strip():
            return False
        haystack = " ".join(
            (_url_text(source_url), normalize(search_query))
        )
    else:
        haystack = " ".join(
            (
                normalize(alt_text),
                normalize(credit_text),
                _url_text(source_url),
                normalize(search_query),
            )
        )
    if not haystack.strip():
        return False
    haystack_tokens = _tokens(haystack)
    for entity in entity_set:
        if " " in entity:
            if entity in haystack:
                return True
            continue
        if entity in haystack_tokens:
            return True
    return False


def _url_text(url: str) -> str:
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return ""
    text = " ".join((parsed.netloc, parsed.path, parsed.query))
    return normalize(text).replace("/", " ").replace("-", " ").replace("_", " ").replace(".", " ")
