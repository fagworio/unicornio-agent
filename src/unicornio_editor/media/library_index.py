"""Índice local de mídia: fingerprint e proveniência dos uploads (Fase 13).

Ao subir uma imagem guardamos o fingerprint dela — pHash, URL original, página
de origem, subject e o id na Media Library. Sem isso a mesma imagem podia ser
descoberta, baixada e enviada de novo a cada post: a biblioteca enche de
duplicatas e o agente repete trabalho (custo + Media Library suja).

O índice mora no filesystem do agente (``work/media_index.json``), não em meta
do WordPress: o REST descarta meta não registrada e não queremos exigir mais um
mu-plugin para isso.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

_CAMINHO_RELATIVO = Path("work") / "media_index.json"

# O media plan roda em ThreadPoolExecutor(max_workers=4) e cada worker pode
# registrar mídia: sem lock, dois workers leem 10 entradas, cada um grava a sua
# 11ª e a primeira atualização é PERDIDA. Também protege contra duas execuções
# CLI simultâneas (select+replace é atômico no filesystem).
_LOCK = threading.Lock()


def _caminho(root: Path | str) -> Path:
    return Path(root) / _CAMINHO_RELATIVO


def load_index(root: Path | str) -> dict[str, Any]:
    """Índice atual (fail-soft: ausente/corrompido devolve estrutura vazia)."""
    try:
        dados = json.loads(_caminho(root).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"entries": []}
    if not isinstance(dados, dict) or not isinstance(dados.get("entries"), list):
        return {"entries": []}
    return dados


def _salvar(root: Path | str, dados: dict[str, Any]) -> None:
    """Escrita ATÔMICA (tmp + replace): leitor concorrente nunca vê JSON pela
    metade e um crash no meio não corrompe o índice."""
    caminho = _caminho(root)
    try:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        temporario = caminho.with_name(caminho.name + f".{os.getpid()}.tmp")
        temporario.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
        os.replace(temporario, caminho)
    except Exception:  # noqa: BLE001 - deduplicação é otimização, não gate
        pass


def hamming(a: str, b: str) -> int:
    """Distância de Hamming entre dois pHashes em texto binário."""
    if not a or not b:
        return 999
    return sum(1 for x, y in zip(a, b) if x != y)


def register(
    root: Path | str,
    *,
    phash: str = "",
    source_url: str = "",
    source_page: str = "",
    subject: str = "",
    media_id: int | None = None,
    article_id: int | None = None,
) -> None:
    """Registra um upload (ou reaproveitamento) no índice."""
    if not (phash or source_url or subject):
        return
    with _LOCK:
        _registrar_sem_lock(root, phash=phash, source_url=source_url,
                            source_page=source_page, subject=subject,
                            media_id=media_id, article_id=article_id)


def _registrar_sem_lock(
    root: Path | str,
    *,
    phash: str = "",
    source_url: str = "",
    source_page: str = "",
    subject: str = "",
    media_id: int | None = None,
    article_id: int | None = None,
) -> None:
    dados = load_index(root)
    entradas = dados["entries"]
    for entrada in entradas:
        if source_url and entrada.get("source_url") == source_url:
            entrada.update({"phash": phash or entrada.get("phash", ""),
                            "source_page": source_page or entrada.get("source_page", ""),
                            "subject": subject or entrada.get("subject", "")})
            if media_id:
                entrada["media_id"] = media_id
            _salvar(root, dados)
            return
    entradas.append({
        "phash": phash,
        "source_url": source_url,
        "source_page": source_page,
        "subject": subject,
        "media_id": media_id,
        "article_id": article_id,
        "uses": 1,
    })
    _salvar(root, dados)


def find_by_source_url(root: Path | str, source_url: str) -> dict[str, Any] | None:
    """A imagem exata já foi usada em algum post?"""
    if not source_url:
        return None
    for entrada in load_index(root)["entries"]:
        if entrada.get("source_url") == source_url:
            return entrada
    return None


def find_similar(root: Path | str, phash: str, *, threshold: int = 6) -> dict[str, Any] | None:
    """Já temos este frame (mesmo recomprimido/redimensionado)?"""
    if not phash:
        return None
    for entrada in load_index(root)["entries"]:
        candidato = str(entrada.get("phash") or "")
        if candidato and hamming(candidato, phash) <= threshold:
            return entrada
    return None


def find_by_subject(root: Path | str, subject: str) -> list[dict[str, Any]]:
    """Imagens já validadas para este subject (evita refazer a busca)."""
    alvo = (subject or "").strip().lower()
    if not alvo:
        return []
    return [
        e for e in load_index(root)["entries"]
        if str(e.get("subject") or "").strip().lower() == alvo
    ]


def count(root: Path | str) -> int:
    return len(load_index(root)["entries"])


__all__ = [
    "load_index",
    "register",
    "find_by_source_url",
    "find_similar",
    "find_by_subject",
    "hamming",
    "count",
]
