"""Resolvedor de origem para candidatos discovery_only (SourceResolver).

O Yandex aumenta o recall mas entrega imagens SEM a página de origem. Descartar
tudo perde recall; aceitar sem origem quebra a segurança. O resolver tenta
descobrir ONDE a imagem foi publicada — e só então a cadeia
``query -> página -> URL -> bytes -> subject`` volta a existir.

Estratégias, na ordem de melhor yield:

* **C — domínios oficiais**: a entidade do subject tem publishers conhecidos
  (registry); procurar primeiro dentro deles costuma achar a página oficial.
* **B — URL/filename**: o nome do arquivo (``metroid-prime-4-keyart.jpg``)
  costuma reproduzir o slug da matéria; a busca textual localiza páginas
  candidatas.
* **A — metadata do resultado**: quando o engine já oferece a página, nada a
  fazer (o caminho normal do Bing).

A busca só LOCALIZA páginas candidatas. A prova continua determinística:
``validate_discovered_candidate()`` confirma que a imagem consta na página.
Nunca aceitamos uma origem por "parecer provável".
"""

from __future__ import annotations

import re
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from .official_sources import dominios_oficiais

_Busca = Callable[[str], list[dict[str, Any]]]


def _slug_do_filename(image_url: str) -> str:
    """'metroid-prime-4-keyart.jpg' -> 'metroid prime 4 keyart'."""
    nome = unquote(str(image_url or "").split("?")[0].rsplit("/", 1)[-1])
    nome = re.sub(r"\.[a-z0-9]{2,5}$", "", nome, flags=re.IGNORECASE)
    return " ".join(re.sub(r"[-_+.]+", " ", nome).split())


def _host(image_url: str) -> str:
    try:
        return (urlparse(str(image_url or "")).hostname or "").lower()
    except ValueError:
        return ""


def _buscador_padrao(query: str) -> list[dict[str, Any]]:
    """Busca de PÁGINAS candidatas (não de imagens) via Bing.

    Usa o mesmo circuit breaker das engines de imagem: se o Bing está em
    cooldown, o resolver simplesmente não encontra nada agora e o candidato
    permanece discovery_only (nunca vira ACCEPT sem prova de origem).
    """
    from . import search as _search
    from urllib.parse import quote_plus

    if not _search.engine_disponivel("bing"):
        return []
    try:
        # Busca WEB (não de imagens): o resolver procura PÁGINAS candidatas.
        html = _search._fetch(
            f"https://www.bing.com/search?q={quote_plus(query)}", 25.0
        )
    except Exception:  # noqa: BLE001 - resolver é best-effort
        return []
    if not html:
        return []
    paginas: list[dict[str, Any]] = []
    vistos: set[str] = set()
    # Links de resultado do Bing: <h2><a href="https://pagina">
    for trecho in re.findall(r'<h2[^>]*>\s*<a[^>]+href="(https?://[^"]+)"', html):
        if trecho in vistos or "bing.com" in trecho:
            continue
        vistos.add(trecho)
        paginas.append({"source_page_url": trecho})
    return paginas


def _queries(candidate: dict[str, Any], subject: str, extra: str = "") -> list[str]:
    """Queries de localização, da mais forte para a mais fraca."""
    imagem = str(candidate.get("direct_image_url") or "")
    queries: list[str] = []
    for dominio in dominios_oficiais(subject, extra=extra)[:3]:
        queries.append(f"site:{dominio} \"{subject}\"")
    slug = _slug_do_filename(imagem)
    if slug and len(slug) >= 8:
        queries.append(f"\"{slug}\"")
        queries.append(f"{subject} {slug.split()[0]}")
    if not queries and subject:
        queries.append(f"{subject} key art")
    return queries


def _mesma_url(a: str, b: str) -> bool:
    """Mesma URL ignorando esquema/host-casing e barra final."""
    def _norm(u: str) -> str:
        try:
            partes = urlparse(str(u or "").strip())
        except ValueError:
            return ""
        caminho = (partes.path or "").rstrip("/")
        return f"{(partes.hostname or '').lower()}{caminho}"

    return bool(a) and _norm(a) == _norm(b)


def _parece_arquivo_bruto(url: str) -> bool:
    """A "página" é o próprio arquivo de imagem (não prova nada)."""
    caminho = (urlparse(str(url or "")).path or "").lower()
    return bool(re.search(r"\.(jpe?g|png|webp|gif|avif|bmp)$", caminho))


def resolve_candidate_source(
    candidate: dict[str, Any],
    subject: str,
    *,
    extra: str = "",
    busca: _Busca | None = None,
    max_paginas: int = 5,
    verifier: Any = None,
) -> dict[str, Any]:
    """Tenta resolver a origem de um candidato (discovery_only ou não).

    Devolve o candidato (cópia enriquecida) com ``source_page_url`` e
    ``source_resolution`` quando conseguir localizar uma página plausível, e
    sempre com ``candidate_pages`` (todas as páginas plausíveis encontradas).

    Duas correções da auditoria:

    * **múltiplas páginas**: antes o resolver devolvia a PRIMEIRA página e, se
      ela não contivesse a imagem, o candidato era rejeitado — mesmo havendo
      uma segunda página que provava a imagem. Com ``verifier`` (a validação
      determinística) o resolver testa as páginas em ordem e fica com a
      primeira que realmente contém a imagem.
    * **mesmo host**: página e imagem no mesmo domínio
      (``example.com/materia`` + ``example.com/imagem.jpg``) é origem
      excelente; o que não vale é a página SER a imagem.
    """
    resultado = dict(candidate)
    if str(candidate.get("source_page_url") or "").strip():
        resultado["source_resolution"] = "already_present"
        return resultado

    buscar = busca or _buscador_padrao
    paginas_candidatas: list[str] = []
    query_origem = ""
    for query in _queries(candidate, subject, extra):
        try:
            paginas = buscar(query) or []
        except Exception:  # noqa: BLE001
            paginas = []
        for pagina in paginas[:max_paginas]:
            url = str(pagina.get("source_page_url") or "").strip()
            if not url or url in paginas_candidatas:
                continue
            if _mesma_url(url, str(candidate.get("direct_image_url") or "")):
                continue
            if _parece_arquivo_bruto(url):
                continue
            paginas_candidatas.append(url)
            if not query_origem:
                query_origem = query
    resultado["candidate_pages"] = paginas_candidatas[: max_paginas * 2]
    if not paginas_candidatas:
        resultado["source_resolution"] = "unresolved"
        resultado["source_resolution_query"] = ""
        return resultado

    # Com verifier: escolhe a primeira página que REALMENTE contém a imagem.
    if verifier is not None:
        for pagina in paginas_candidatas:
            try:
                veredito = verifier({**candidate, "source_page_url": pagina})
            except Exception:  # noqa: BLE001 - verificação é best-effort aqui
                continue
            if veredito.get("valid"):
                resultado["source_page_url"] = pagina
                resultado["source_resolution"] = "verified_page"
                resultado["source_resolution_query"] = query_origem
                return resultado
        resultado["source_resolution"] = "unresolved"
        resultado["source_resolution_query"] = query_origem
        return resultado

    resultado["source_page_url"] = paginas_candidatas[0]
    resultado["source_resolution"] = (
        "official_domain" if query_origem.startswith("site:") else "filename_search"
    )
    resultado["source_resolution_query"] = query_origem
    return resultado


__all__ = ["resolve_candidate_source", "resolve_batch"]


def resolve_batch(
    candidates: list[dict[str, Any]],
    subject: str,
    *,
    extra: str = "",
    busca: _Busca | None = None,
    max_paginas: int = 5,
) -> list[dict[str, Any]]:
    """Aplica o resolver aos candidatos sem origem (best-effort)."""
    return [
        resolve_candidate_source(c, subject, extra=extra, busca=busca, max_paginas=max_paginas)
        for c in candidates
    ]
