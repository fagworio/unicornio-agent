"""Ready Manifest: fingerprint determinístico do estado pronto para publicar.

Quando o preflight (apply) passa, o pipeline grava no WordPress:

- ``_hermes_ready_manifest`` — JSON canônico compacto com o que foi escrito
- ``_hermes_ready_hash``     — SHA-256 desse manifest

No ``publish-ready`` o hash atual é recalculado a partir do que está no
WordPress agora (content.raw + featured_media + meta SEO + original_link).
Se o hash bater, nada mudou desde o apply → publica SEM re-executar o
checklist caro (especialmente o vision gate). Se mudou → STALE → revalida
com o checklist completo antes de decidir.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .state import canonical_json

META_READY_MANIFEST = "_hermes_ready_manifest"

_POLICY_AGNOSTIC = ("post_id", "content_hash", "featured_media", "seo_hash", "original_link")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_ready_manifest(
    *,
    post_id: int,
    content: str,
    featured_media: int | None,
    seo: dict[str, Any] | None,
    original_link: str | None,
    editorial: dict[str, Any] | None,
    policy_version: int,
) -> dict[str, Any]:
    """Manifest determinístico do estado pronto (gravado no READY).

    ``content`` é o conteúdo final exato que foi gravado no WordPress.
    ``editorial`` entra como hash (nunca o corpo inteiro na meta).
    """
    seo = dict(seo or {})
    seo_hash = _sha256(canonical_json({k: seo.get(k) for k in ("title", "meta_description", "focus_keyword")}))
    editorial_hash = _sha256(canonical_json(editorial or {})) if editorial is not None else ""
    return {
        "post_id": post_id,
        "content_hash": _sha256(content or ""),
        "featured_media": featured_media if isinstance(featured_media, int) and featured_media > 0 else None,
        "seo_hash": seo_hash,
        "original_link": original_link or "",
        "editorial_hash": editorial_hash,
        "policy_version": policy_version,
    }


def manifest_hash(manifest: dict[str, Any]) -> str:
    """SHA-256 do manifest canônico."""
    return _sha256(canonical_json(manifest))


def current_manifest_hash(
    post: dict[str, Any],
    stored_manifest: dict[str, Any] | None,
    *,
    policy_version: int,
) -> str:
    """Hash do estado ATUAL do WordPress, comparável com ``_hermes_ready_hash``.

    Recalcula os campos derivados do post (content_hash, seo_hash,
    featured_media, original_link) e mantém os campos de política do manifest
    armazenado (editorial_hash, policy_version) — o editorial não vive no
    WordPress, e se o conteúdo/SEO/destaque não mudou, nada mudou.
    """
    meta = post.get("meta") if isinstance(post.get("meta"), dict) else {}
    content = post.get("content") or {}
    raw = content.get("raw") if isinstance(content, dict) else None
    seo = {
        "title": meta.get("rank_math_title") or "",
        "meta_description": meta.get("rank_math_description") or "",
        "focus_keyword": meta.get("rank_math_focus_keyword") or "",
    }
    original_link = meta.get("original_link") or ""
    stored = stored_manifest if isinstance(stored_manifest, dict) else {}
    candidate = {
        "post_id": post.get("id"),
        "content_hash": _sha256(raw if isinstance(raw, str) else ""),
        "featured_media": post.get("featured_media") if isinstance(post.get("featured_media"), int) else None,
        "seo_hash": _sha256(canonical_json(seo)),
        "original_link": original_link if isinstance(original_link, str) else "",
        "editorial_hash": stored.get("editorial_hash", ""),
        "policy_version": stored.get("policy_version", policy_version),
    }
    return manifest_hash(candidate)


def manifest_matches(
    post: dict[str, Any],
    stored_manifest: dict[str, Any] | None,
    ready_hash: str,
    *,
    policy_version: int,
) -> bool:
    """True quando o WordPress ainda está exatamente como o apply deixou."""
    if not ready_hash or not stored_manifest:
        return False
    if stored_manifest.get("post_id") != post.get("id"):
        return False
    return current_manifest_hash(post, stored_manifest, policy_version=policy_version) == ready_hash


def serialize_manifest(manifest: dict[str, Any]) -> str:
    return json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_manifest(raw: Any) -> dict[str, Any] | None:
    """Tolerante: meta inválida/corrompida -> None (força revalidação)."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None
