"""Cache de validacao visual por hash(imagem) + entidade + versao.

A MESMA key art aparece em varios posts (GTA 6 trailer, leak, lancamento...).
Sem cache, cada artigo pagaria uma chamada de visao para a mesma imagem. Este
cache evita re-analise: chave = sha256(url) + entidade normalizada + versao do
validador. Entidade importa: a mesma imagem pode ser valida para um artigo
(GTA 6) e invalida para outro (Redfall). Fail-soft: cache ausente/corrompido
apenas significa "nao cacheado" (nao derruba o pipeline).
"""

from __future__ import annotations

import hashlib
import json
import re
import os
import threading
from pathlib import Path
from typing import Any

VISION_CACHE_VERSION = "vision-v2"
VISUAL_COMPARISON_CACHE_VERSION = "visual-compare-v1"
_CACHE_LOCK = threading.Lock()


def vision_cache_path(root: str | Path) -> Path:
    return Path(root) / "work" / "vision_cache.json"


def visual_comparison_cache_path(root: str | Path) -> Path:
    return Path(root) / "work" / "visual_comparison_cache.json"


def _normalize_entity(entity: str) -> str:
    """Slug curto da entidade para a chave de cache (case/acento-insensivel)."""
    decomposed = re.sub(r"[̀-ͯ]", "", (entity or "").lower())
    slug = re.sub(r"[^a-z0-9]+", "-", decomposed).strip("-")
    return slug[:48] or "none"


def cache_key(image_url: str, entity: str, version: str = VISION_CACHE_VERSION) -> str:
    """Chave estavel: hash da imagem + entidade + versao do validador."""
    digest = hashlib.sha256((image_url or "").encode("utf-8")).hexdigest()[:12]
    return f"{digest}:{_normalize_entity(entity)}:{version}"


def read_vision_cache(root: str | Path) -> dict[str, Any]:
    """Le o cache de visao; tolerante a arquivo ausente/corrompido."""
    path = vision_cache_path(root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def get_cached_decision(root: str | Path, image_url: str, entity: str) -> Any | None:
    """Resultado de visao cacheado para esta imagem+entidade, ou None."""
    key = cache_key(image_url, entity)
    cache = read_vision_cache(root)
    value = cache.get(key)
    if isinstance(value, dict) and value.get("status"):
        return value
    return None


def set_cached_decision(
    root: str | Path, image_url: str, entity: str, decision: dict[str, Any]
) -> None:
    """Persiste o resultado de visao no cache (fail-soft)."""
    try:
        path = vision_cache_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        cache = read_vision_cache(root)
        cache[cache_key(image_url, entity)] = decision
        _atomic_write(path, cache)
    except OSError:
        pass


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temporary, path)


def visual_comparison_cache_key(reference_sha256: str, candidate_sha256: str) -> str:
    """Symmetric key: comparison is about pixels, never URL or editorial subject."""
    if not reference_sha256 or not candidate_sha256:
        return ""
    left, right = sorted((str(reference_sha256), str(candidate_sha256)))
    return f"{left}:{right}:{VISUAL_COMPARISON_CACHE_VERSION}"


def get_cached_visual_comparison(root: str | Path, reference_sha256: str, candidate_sha256: str) -> dict[str, Any] | None:
    key = visual_comparison_cache_key(reference_sha256, candidate_sha256)
    if not key:
        return None
    path = visual_comparison_cache_path(root)
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get(key)
    except (OSError, ValueError, AttributeError):
        return None
    return dict(value) if isinstance(value, dict) and value.get("decision") else None


def set_cached_visual_comparison(root: str | Path, reference_sha256: str, candidate_sha256: str, decision: dict[str, Any]) -> None:
    key = visual_comparison_cache_key(reference_sha256, candidate_sha256)
    if not key:
        return
    try:
        path = visual_comparison_cache_path(root)
        with _CACHE_LOCK:
            try:
                cache = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            except (OSError, ValueError):
                cache = {}
            if isinstance(cache, dict):
                cache[key] = dict(decision)
                _atomic_write(path, cache)
    except OSError:
        pass


__all__ = [
    "VISION_CACHE_VERSION", "VISUAL_COMPARISON_CACHE_VERSION", "vision_cache_path", "visual_comparison_cache_path", "cache_key",
    "read_vision_cache", "get_cached_decision", "set_cached_decision",
    "visual_comparison_cache_key", "get_cached_visual_comparison", "set_cached_visual_comparison",
]
