"""Cache de validação visual por hash dos bytes + entidade + política + versão.

A MESMA key art aparece em varios posts (GTA 6 trailer, leak, lancamento...).
Sem cache, cada artigo pagaria uma chamada de visão para a mesma imagem. Este
cache evita reanálise quando os bytes, assunto e política são idênticos.
Chamadores sem hash de conteúdo mantêm uma chave legada baseada em URL, mas
fluxos de featured usam obrigatoriamente o SHA-256 materializado. Fail-soft:
cache ausente/corrompido apenas significa "não cacheado".
"""

from __future__ import annotations

import hashlib
import json
import re
import os
import threading
import fcntl
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

VISION_CACHE_VERSION = "vision-v4-content-policy-model-context"
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


def vision_model_identity(base_url: str, model: str) -> str:
    """Return a credential-free provider/model identity suitable for cache keys."""
    parsed = urlsplit(str(base_url or ""))
    host = (parsed.hostname or "").casefold()
    try:
        parsed_port = parsed.port
    except ValueError:
        parsed_port = None
    port = f":{parsed_port}" if parsed_port else ""
    endpoint = f"{parsed.scheme.casefold()}://{host}{port}" if host else "unknown-provider"
    return f"{endpoint}/{str(model or '').strip()}"


def cache_key(
    image_url: str,
    entity: str,
    version: str = VISION_CACHE_VERSION,
    *,
    image_sha256: str = "",
    policy: str = "default",
    model_identity: str = "",
    context_key: str = "",
) -> str:
    """Key by actual image bytes when known, otherwise by URL (legacy callers)."""
    source_identity = str(image_sha256 or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{64}", source_identity):
        source_identity = "url:" + hashlib.sha256((image_url or "").encode("utf-8")).hexdigest()
    digest = hashlib.sha256(source_identity.encode("utf-8")).hexdigest()[:12]
    # Hash full model/context strings rather than truncating them into slugs:
    # distinct providers, model revisions, or editorial categories must not
    # accidentally collide in a shared cache.
    scope = hashlib.sha256(json.dumps(
        [str(model_identity or "").strip(), str(context_key or "").strip()],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()[:16]
    return f"{digest}:{_normalize_entity(entity)}:{_normalize_entity(policy)}:{scope}:{version}"


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


def get_cached_decision(
    root: str | Path,
    image_url: str,
    entity: str,
    *,
    image_sha256: str = "",
    policy: str = "default",
    model_identity: str = "",
    context_key: str = "",
) -> Any | None:
    """Return the cached image/entity decision; byte hashes take precedence."""
    key = cache_key(
        image_url, entity, image_sha256=image_sha256, policy=policy,
        model_identity=model_identity, context_key=context_key,
    )
    cache = read_vision_cache(root)
    value = cache.get(key)
    if isinstance(value, dict) and value.get("status"):
        return value
    return None


def set_cached_decision(
    root: str | Path,
    image_url: str,
    entity: str,
    decision: dict[str, Any],
    *,
    image_sha256: str = "",
    policy: str = "default",
    model_identity: str = "",
    context_key: str = "",
) -> None:
    """Persiste o resultado de visao no cache (fail-soft)."""
    try:
        path = vision_cache_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Re-read under both a process-local mutex and an OS file lock so
        # concurrent workers merge entries instead of overwriting each other.
        with _CACHE_LOCK, path.with_suffix(path.suffix + ".lock").open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            cache = read_vision_cache(root)
            cache[cache_key(
                image_url, entity, image_sha256=image_sha256, policy=policy,
                model_identity=model_identity, context_key=context_key,
            )] = dict(decision)
            _atomic_write(path, cache)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
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
    "read_vision_cache", "get_cached_decision", "set_cached_decision", "vision_model_identity",
    "visual_comparison_cache_key", "get_cached_visual_comparison", "set_cached_visual_comparison",
]
