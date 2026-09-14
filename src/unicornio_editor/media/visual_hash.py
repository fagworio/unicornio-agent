"""Perceptual image similarity (pHash) for the pre-publication gate.

Fonte de imagem diferente != imagem diferente: o MESMO frame (screenshot,
key art) replicado por varios veiculos chega com URL e bytes diferentes
(recompressao WebP), mas e a MESMA imagem para o leitor. O fingerprint de URL
nao pega isso; o pHash pega.

Fail-soft por design: se o download/hash de uma imagem falhar, ela e ignorada
(o gate nao pode travar o pipeline por rede).
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


def similar_image_pairs(
    urls: list[str],
    *,
    threshold: int = DEFAULT_THRESHOLD,
) -> list[tuple[str, str, int]]:
    """Pares de URLs cujas imagens sao visualmente a mesma (dist <= threshold)."""
    hashes: list[tuple[str, Any]] = []
    for url in urls:
        if not url:
            continue
        try:
            hashes.append((url, _phash(url)))
        except Exception:  # noqa: BLE001 - rede/formato: ignora a imagem
            continue
    out: list[tuple[str, str, int]] = []
    for i in range(len(hashes)):
        for j in range(i + 1, len(hashes)):
            u1, h1 = hashes[i]
            u2, h2 = hashes[j]
            dist = h1 - h2
            if dist <= threshold:
                out.append((u1, u2, int(dist)))
    return out
