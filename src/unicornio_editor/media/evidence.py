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
    itens: list[dict[str, Any]] = []
    for indice, h2 in enumerate(_H2_RE.findall(content_html or ""), start=1):
        limpo = re.sub(r"\s+", " ", _TAG_RE.sub(" ", h2)).strip()
        numero = _ITEM_RE.match(limpo)
        item = int(numero.group(1)) if numero else indice
        # O H2 inteiro (menos o numero) e o subject: "Cyberpunk: Edgerunners"
        # nomeia a obra em si — cortar no ":" perderia metade do nome.
        subject = _ITEM_RE.sub("", limpo).strip()
        if subject:
            itens.append({"item": item, "heading": subject, "subject": subject})
    if itens:
        return itens

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
    "PENALIDADES",
    "LIMIAR_MATCH",
    "LIMIAR_AMBIGUO",
    "post_subjects",
    "source_context",
    "evidence_score",
    "subject_for_image",
]
