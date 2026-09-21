"""Evidência determinística de imagem (Fases 6-11 do documento de correções).

Nenhuma imagem entra porque o agente disse que ela é correta. Ela entra porque
existe uma cadeia verificável:

    query -> página de origem -> URL da imagem -> bytes -> subject -> seção -> hash

O que este módulo resolve:

* **subject** (Fases 7-8): artigo normal -> entidade principal do título;
  listicle -> 1 subject por H2 (o item citado). O subject é o que a imagem
  precisa representar, e é contra ELE que cada imagem é validada (Fase 9) —
  nunca contra o conjunto global de entidades do artigo.
* **contexto da origem** (Fase 10): título da página, og:title, figcaption,
  alt original, heading próximo, filename e URL — extraídos sem IA.
* **score de evidência** (Fase 11): pesos objetivos + veredito
  (``deterministic_match`` / ``ambiguous`` / ``reject``). Visão/LLM só entra em
  ``ambiguous``.

Evidência CONFIÁVEL (de origem): filename, og:title, page title, alt original,
figcaption original, heading próximo, URL da página, query.
Evidência NÃO confiável (escrita pelo agente): alt/caption/crédito gerados —
existem para acessibilidade e crédito, jamais como prova de relevância.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlparse

from .relevance import extract_entities, normalize

_H2_RE = re.compile(r"<h2\b[^>]*>(.*?)</h2>", re.IGNORECASE | re.DOTALL)
_TITLE_RE = re.compile(r"<title\b[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_ITEM_RE = re.compile(r"^\s*(\d{1,3})\s*[.)\u2013-]\s*")
_FIGCAPTION_RE = re.compile(r"<figcaption\b[^>]*>(.*?)</figcaption>", re.IGNORECASE | re.DOTALL)
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r'([a-zA-Z-]+)\s*=\s*["\']([^"\']*)["\']')

# Pesos do score (Fase 11) — evidência de origem, nunca texto do agente.
PESOS: dict[str, int] = {
    "filename": 5,
    "og_title": 5,
    "page_title": 4,
    "alt_original": 4,
    "figcaption": 3,
    "heading": 3,
    "page_url": 2,
    "query": 1,
}
PENALIDADES: dict[str, int] = {
    "missing_source": -10,
    "not_in_source": -10,
    "duplicate_frame": -10,
}
LIMIAR_MATCH = 7
LIMIAR_AMBIGUO = 4


def post_subjects(
    title: str = "",
    content_html: str = "",
    *,
    focus_keyword: str = "",
    game_name: str | None = None,
) -> list[dict[str, Any]]:
    """Subjects do post (Fases 7-8).

    Listicle (H2 numerados): 1 subject por item — ``{"item", "heading",
    "subject"}``. Artigo normal: a entidade principal do título (a frase mais
    específica, ex. "Metroid Prime 4").

    Cada subject é a chave de validação da SUA imagem: a imagem do item 1 não
    pode ser aceita por evidência do item 3 (Fase 9).
    """
    # Lista = H2 REALMENTE numerados ("1. Bleach"). Um artigo comum também tem
    # H2 ("O que sabemos até agora") e antes cada um virava item — o subject
    # saía do H2 em vez da entidade do título ("O que sabemos até agora" no
    # lugar de "metroid prime 4"), destruindo o score em notícias normais.
    numerados: list[tuple[int, str]] = []
    for h2 in _H2_RE.findall(content_html or ""):
        limpo = re.sub(r"\s+", " ", _TAG_RE.sub(" ", h2)).strip()
        achado = _ITEM_RE.match(limpo)
        if not achado:
            continue
        # O H2 inteiro (menos o numero) e o subject: "Cyberpunk: Edgerunners"
        # nomeia a obra em si — cortar no ":" perderia metade do nome.
        texto_item = _ITEM_RE.sub("", limpo).strip()
        if texto_item:
            numerados.append((int(achado.group(1)), texto_item))
    if len(numerados) >= 2:
        return [
            {"item": numero, "heading": texto, "subject": texto}
            for numero, texto in numerados
        ]

    principal = _entidade_principal(title)
    if not principal:
        entidades = extract_entities(
            title=title,
            content_html=content_html,
            focus_keyword=focus_keyword,
            game_name=game_name,
        )
        if not entidades:
            return []
        principal = max(sorted(entidades), key=len)
    return [{"item": None, "heading": principal, "subject": principal}]


_NOME_PROPRIO_RE = re.compile(
    r"[A-Z\u00c0-\u00dd][\w'\u2019.\-]*(?:\s+(?:[A-Z\u00c0-\u00dd][\w'\u2019.\-]*|\d+))*"
)


def _entidade_principal(title: str) -> str:
    """Entidade principal do título (Fase 7).

    "Nintendo anuncia novo trailer de Metroid Prime 4" -> "metroid prime 4".
    Heurística determinística: a MAIOR sequência de palavras capitalizadas
    (nomes próprios) do título. O título literal não serve como subject — foi
    ele que fizera uma key art correta pontuar zero, porque "nintendo anuncia
    novo trailer de metroid prime 4" nunca aparece no filename/og:title.
    """
    texto = _TAG_RE.sub(" ", str(title or "")).strip()
    if not texto:
        return ""
    grupos = [g.strip() for g in _NOME_PROPRIO_RE.findall(texto)]
    grupos = [g for g in grupos if len(g) >= 4]
    if not grupos:
        return normalize(texto)
    return normalize(max(grupos, key=len))



# Tipo de conteúdo do artigo -> termo de contexto que desambigua o item.
# Um H2 isolado ("Pluto") traz o planeta do National Geographic; com o tipo do
# artigo a busca fica no domínio certo ("Pluto anime") sem custo nenhum.
_CONTEXTO_POR_TIPO: tuple[tuple[str, str], ...] = (
    ("anime", ("anime", "animes")),
    ("manga", ("manga", "mangas")),
    ("game", ("jogo", "jogos", "game", "games", "videogame", "videogames")),
    ("filme", ("filme", "filmes")),
    ("serie", ("serie", "series")),
    ("quadrinho", ("quadrinho", "quadrinhos", "hq", "hqs", "comic", "comics")),
    ("personagem", ("personagem", "personagens")),
    ("temporada", ("temporada", "temporadas")),
)


def tipo_de_conteudo(article_title: str) -> str:
    """Termo de contexto do artigo (o "Pluto anime" vem daqui)."""
    alvo = normalize(article_title or "")
    if not alvo:
        return ""
    for termo, variantes in _CONTEXTO_POR_TIPO:
        if any(re.search(rf"\b{re.escape(v)}", alvo) for v in variantes):
            return termo
    return ""


def item_query(subject: str, article_title: str = "", *, extra: str = "") -> str:
    """Query ESPECÍFICA para o subject de um item (Fase 8).

    ``item_query("Pluto", "10 melhores animes")`` -> ``"Pluto anime"``.
    Sem o contexto do artigo, ``Pluto`` devolve o planeta; com ele, o recall
    fica no domínio editorial do post. Nunca duplica o termo já presente.
    """
    base = " ".join(str(subject or "").split()).strip()
    if not base:
        return ""
    contexto = tipo_de_conteudo(article_title) or tipo_de_conteudo(extra)
    if not contexto:
        return base
    if re.search(rf"\b{re.escape(contexto)}\b", normalize(base)):
        return base
    return f"{base} {contexto}"


def source_context(
    html: str,
    image_url: str,
    *,
    base_url: str = "",
) -> dict[str, str]:
    """Contexto da página de origem em volta da imagem (Fase 10).

    Devolve ``page_title``, ``og_title``, ``alt_original``, ``figcaption``,
    ``heading`` e ``filename``. Sem IA: só o que a própria página declara.
    """
    if not isinstance(html, str) or not html:
        return {}
    contexto: dict[str, str] = {}

    titulo = _TITLE_RE.search(html)
    if titulo:
        contexto["page_title"] = _TAG_RE.sub(" ", titulo.group(1)).strip()

    og = re.search(
        r'<meta\b[^>]*property=["\']og:title["\'][^>]*content=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    )
    if og:
        contexto["og_title"] = og.group(1).strip()

    alvo = normalize(unquote(str(image_url or "").split("?")[0].rsplit("/", 1)[-1]))
    for tag in _IMG_TAG_RE.findall(html):
        attrs = dict(_ATTR_RE.findall(tag))
        fonte = attrs.get("src") or attrs.get("data-src") or ""
        if alvo and normalize(unquote(fonte.split("/")[-1])) != alvo:
            continue
        alt = (attrs.get("alt") or "").strip()
        if alt:
            contexto["alt_original"] = alt
        break

    legenda = _FIGCAPTION_RE.search(html)
    if legenda:
        contexto["figcaption"] = _TAG_RE.sub(" ", legenda.group(1)).strip()

    cabecalhos = re.findall(r"<h[1-4]\b[^>]*>(.*?)</h[1-4]>", html, re.IGNORECASE | re.DOTALL)
    if cabecalhos:
        contexto["heading"] = _TAG_RE.sub(" ", cabecalhos[0]).strip()

    if base_url:
        contexto["page_url"] = base_url
    nome = unquote(str(image_url or "").split("?")[0].rsplit("/", 1)[-1])
    if nome:
        contexto["filename"] = nome
    return contexto


def _casa(subject: str, texto: str) -> bool:
    """O subject aparece no texto? (frase exata ou todos os tokens-chave.)"""
    if not subject or not texto:
        return False
    alvo = normalize(subject).strip()
    fonte = normalize(texto)
    if not alvo or not fonte:
        return False
    if len(alvo) >= 4 and alvo in fonte:
        return True
    tokens = [t for t in alvo.split() if len(t) >= 3]
    return bool(tokens) and all(t in fonte for t in tokens)


def evidence_score(
    subject: str,
    *,
    filename: str = "",
    og_title: str = "",
    page_title: str = "",
    alt_original: str = "",
    figcaption: str = "",
    heading: str = "",
    page_url: str = "",
    query: str = "",
    source_page_present: bool = True,
    image_in_source: bool = True,
    duplicate_frame: bool = False,
) -> dict[str, Any]:
    """Score determinístico de evidência (Fase 11).

    Ordem dos gates (o score NUNCA substitui proveniência):
      A. proveniência (hard): sem `source_page_url` -> ``unresolved_source``;
         imagem não listada na página -> ``source_mismatch``;
      C. diversidade: pHash duplicado -> ``duplicate_frame``;
      B. relevância (só então): ``>=7`` deterministic_match, ``4-6``
         ambiguous (único lugar para visão), ``<4`` reject.
    """
    campos = {
        "filename": filename,
        "og_title": og_title,
        "page_title": page_title,
        "alt_original": alt_original,
        "figcaption": figcaption,
        "heading": heading,
        "page_url": page_url,
        "query": query,
    }
    evidencias: list[str] = []
    score = 0
    for chave, valor in campos.items():
        if _casa(subject, valor):
            score += PESOS[chave]
            evidencias.append(chave)

    base = {
        "subject": subject,
        "matched": evidencias,
        "evidence": {chave: valor for chave, valor in campos.items() if valor},
    }

    # GATE A — PROVENIÊNCIA (hard gate, não penalidade). Sem origem não existe
    # ACCEPT possível: nenhuma soma de sinais de relevância compensa ausência de
    # proveniência. Era -10 e podia ser "vencido" por muitos +5/+4 — o que
    # permitiria publicar uma imagem bonita de origem desconhecida.
    if not source_page_present:
        return {**base, "score": 0, "gate": "provenance",
                "verdict": "unresolved_source", "penalties": ["missing_source"],
                "needs_vision": False,
                "reason": "sem página de origem: proveniência não resolvida"}
    if not image_in_source:
        return {**base, "score": 0, "gate": "provenance",
                "verdict": "source_mismatch", "penalties": ["not_in_source"],
                "needs_vision": False,
                "reason": "imagem não consta na página de origem (bytes/link divergem)"}

    # GATE C — DIVERSIDADE: frame repetido nunca entra, mesmo com score alto.
    if duplicate_frame:
        return {**base, "score": score + PENALIDADES["duplicate_frame"],
                "gate": "diversity", "verdict": "duplicate_frame",
                "penalties": ["duplicate_frame"], "needs_vision": False,
                "reason": "mesmo frame visual de outra imagem do artigo (pHash)"}

    # GATE B — RELEVÂNCIA (só depois da proveniência válida).
    if score >= LIMIAR_MATCH:
        veredito = "deterministic_match"
    elif score >= LIMIAR_AMBIGUO:
        veredito = "ambiguous"
    else:
        veredito = "reject"
    return {
        **base,
        "score": score,
        "gate": "relevance",
        "verdict": veredito,
        "penalties": [],
        # A visão só entra em ambíguo E com proveniência já válida: uma IA
        # dizendo "parece Metroid" não resolve origem/licenciamento.
        "needs_vision": veredito == "ambiguous",
    }



def dedupe_by_phash(
    aprovados: list[dict[str, Any]],
    rejeitados: list[dict[str, Any]],
    *,
    threshold: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """GATE C antecipado: pHash ANTES da seleção/upload (Fase 12).

    O mesmo frame servido por URLs diferentes entrava várias vezes no plano e só
    era detectado no checklist — depois do download e do upload, com a Media
    Library já suja e o post indo para rework. Aqui os aprovados são agrupados
    por similaridade visual e só o MELHOR de cada grupo sobrevive (a lista chega
    ordenada: origem oficial primeiro, depois score de evidência).

    Fail-soft: se não houver hashes suficientes (imagem inacessível), nada é
    descartado — a política cheia continua valendo no checklist.
    """
    if len(aprovados) < 2:
        return aprovados, rejeitados
    from .visual_hash import image_hashes, similar_image_pairs

    urls = [str(c.get("direct_image_url") or "") for c in aprovados]
    hashes = image_hashes([u for u in urls if u])
    if len(hashes) < 2:
        return aprovados, rejeitados
    kwargs = {} if threshold is None else {"threshold": threshold}
    duplicados = {u2 for _u1, u2, _d in similar_image_pairs(urls, hashes=hashes, **kwargs)}
    if not duplicados:
        return aprovados, rejeitados
    mantidos: list[dict[str, Any]] = []
    for cand in aprovados:
        url = str(cand.get("direct_image_url") or "")
        # fingerprint fica no candidato: o upload o persiste no índice local.
        if hashes.get(url):
            cand["phash"] = str(hashes[url])
        if url in duplicados:
            cand["evidence"] = {
                **(cand.get("evidence") or {}),
                "score": 0,
                "verdict": "duplicate_frame",
                "gate": "diversity",
                "needs_vision": False,
                "reason": "mesmo frame visual (pHash) de outro candidato melhor colocado",
            }
            cand["needs_vision"] = False
            rejeitados.append(cand)
        else:
            mantidos.append(cand)
    return mantidos, rejeitados


def subject_for_image(image_url: str, subjects: list[dict[str, Any]]) -> str:
    """Qual subject a imagem deve representar (Fase 9).

    Com uma lista de subjects (listicle), casa pela posição/heading declarada
    na própria URL/filename; sem match, devolve o subject único do artigo.
    """
    if not subjects:
        return ""
    if len(subjects) == 1:
        return str(subjects[0].get("subject") or "")
    alvo = normalize(unquote(str(image_url or "")))
    for item in subjects:
        if _casa(str(item.get("subject") or ""), alvo):
            return str(item.get("subject") or "")
    return ""


__all__ = [
    "PESOS",
    "dedupe_by_phash",
    "item_query",
    "tipo_de_conteudo",
    "PENALIDADES",
    "LIMIAR_MATCH",
    "LIMIAR_AMBIGUO",
    "post_subjects",
    "source_context",
    "evidence_score",
    "subject_for_image",
]
