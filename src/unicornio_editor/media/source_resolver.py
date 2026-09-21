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


def resolve_candidate_source(
    candidate: dict[str, Any],
    subject: str,
    *,
    extra: str = "",
    busca: _Busca | None = None,
    max_paginas: int = 5,
) -> dict[str, Any]:
    """Tenta resolver a origem de um candidato (discovery_only ou não).

    Devolve o candidato (cópia enriquecida) com ``source_page_url`` e
    ``source_resolution`` quando conseguir localizar uma página plausível.
    **Não** prova nada: quem prova é a validação determinística depois.
    """
    resultado = dict(candidate)
    if str(candidate.get("source_page_url") or "").strip():
        resultado["source_resolution"] = "already_present"
        return resultado

    buscar = busca or _buscador_padrao
    for query in _queries(candidate, subject, extra):
        try:
            paginas = buscar(query) or []
        except Exception:  # noqa: BLE001
            paginas = []
        for pagina in paginas[:max_paginas]:
            url = str(pagina.get("source_page_url") or "").strip()
            if not url or _host(url) == _host(str(candidate.get("direct_image_url") or "")):
                continue
            resultado["source_page_url"] = url
            resultado["source_resolution"] = (
                "official_domain" if query.startswith("site:") else "filename_search"
            )
            resultado["source_resolution_query"] = query
            return resultado

    resultado["source_resolution"] = "unresolved"
    resultado["source_resolution_query"] = ""
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
