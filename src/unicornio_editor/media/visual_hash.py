"""Perceptual image similarity (pHash) for the pre-publication gate.

Fonte de imagem diferente != imagem diferente: o MESMO frame (screenshot,
key art) replicado por varios veiculos chega com URL e bytes diferentes
(recompressao WebP), mas e a MESMA imagem para o leitor. O fingerprint de URL
nao pega isso; o pHash pega.

Fail-soft por design: se o download/hash de uma imagem falhar, ela e ignorada
(o gate nao pode travar o pipeline por rede).

Uso no checklist (opcao A): os hashes sao calculados UMA vez por apply e
reusados para (a) bloquear frames repetidos e (b) dimensionar o minimo de
imagens pela disponibilidade real de frames distintos.
"""

from __future__ import annotations

import io
import urllib.request
from typing import Any

# Distancia de Hamming no pHash: 0 = identicas, <=6 = mesmo frame/screenshot
# (observado em producao: o mesmo print de fontes diferentes da 0-2).
DEFAULT_THRESHOLD = 6

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"


def _phash(url: str, *, timeout: float = 20.0):
    from PIL import Image
    import imagehash

    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(8_000_000)
    return imagehash.phash(Image.open(io.BytesIO(data)))


def image_hashes(urls: list[str], *, timeout: float = 20.0) -> dict[str, Any]:
    """url -> pHash (fail-soft: imagens inacessiveis ficam de fora)."""
    out: dict[str, Any] = {}
    for url in urls:
        if not url or url in out:
            continue
        try:
            out[url] = _phash(url, timeout=timeout)
        except Exception:  # noqa: BLE001 - rede/formato: ignora a imagem
            continue
    return out


def similar_image_pairs(
    urls: list[str],
    *,
    threshold: int = DEFAULT_THRESHOLD,
    hashes: dict[str, Any] | None = None,
) -> list[tuple[str, str, int]]:
    """Pares de URLs cujas imagens sao visualmente a mesma (dist <= threshold)."""
    if hashes is None:
        hashes = image_hashes(urls)
    items = list(hashes.items())
    out: list[tuple[str, str, int]] = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            u1, h1 = items[i]
            u2, h2 = items[j]
            dist = int(h1 - h2)
            if dist <= threshold:
                out.append((u1, u2, dist))
    return out


def distinct_image_count(
    urls: list[str],
    *,
    threshold: int = DEFAULT_THRESHOLD,
    hashes: dict[str, Any] | None = None,
) -> int | None:
    """Numero de frames visualmente distintos (union-find dos pares similares).

    Retorna None quando nao ha hashes suficientes (fail-soft: o chamador mantem
    a politica cheia). Com 0-1 imagens hasheadas, retorna len(hashes).
    """
    if hashes is None:
        hashes = image_hashes(urls)
    if not hashes:
        return None
    keys = list(hashes.keys())
    parent = {k: k for k in keys}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            if int(hashes[keys[i]] - hashes[keys[j]]) <= threshold:
                ri, rj = find(keys[i]), find(keys[j])
                if ri != rj:
                    parent[rj] = ri
    return len({find(k) for k in keys})
