"""Pre-publication checklist: deterministic, sequential, fail-closed.

Each rule defined in the editorial policy is evaluated in order and reported
as pass / fail / skip. Publishing a post should only happen when every item
passes (``all_passed == True``).

The checklist never writes to WordPress — it is a read-only gate that runs
against the post snapshot, the validated editorial JSON and the final
content that would be published.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .config import Config
from .content_quality import ContentQualityError, minimum_image_count, validate_content_quality, word_count
from .editorial_schema import EditorialValidationError, validate_editorial
from .list_quality import ListContentError, validate_list_content
from .wordpress import WordPressClient

_CTA_MARKER = "Confira mais novidades em nosso Portal de"
_IMG_RE = re.compile(r"<img\b[^>]*\bsrc=\"([^\"]+)\"", re.IGNORECASE)
_IFRAME_RE = re.compile(r"<iframe\b[^>]*youtube", re.IGNORECASE)


def run_pre_publish_checklist(
    *,
    post: Mapping[str, Any],
    editorial: Mapping[str, Any],
    content: str,
    backup_path: str | Path | None,
    config: Config,
    client: WordPressClient | None = None,
) -> dict[str, Any]:
    """Run every policy rule in sequence and report the result per item."""
    if not isinstance(post, Mapping):
        raise TypeError("post must be a mapping")
    if not isinstance(editorial, Mapping):
        raise TypeError("editorial must be a mapping")
    if not isinstance(content, str):
        raise TypeError("content must be a string")

    items: list[dict[str, str]] = []

    def check(name: str, ok: bool, detail: str, skipped: bool = False) -> None:
        items.append(
            {
                "name": name,
                "status": "skip" if skipped else ("pass" if ok else "fail"),
                "detail": detail,
            }
        )

    # 1. Backup snapshot before any processing.
    backup_ok = bool(backup_path) and Path(str(backup_path)).exists()
    check("backup", backup_ok, f"snapshot: {backup_path}" if backup_ok else "snapshot ausente")

    # 2. Only pending posts enter the editorial pipeline.
    status = str(post.get("status") or "")
    check("status_pending", status == "pending", f"status atual: {status or 'desconhecido'}")

    # 3. Relevance classification with confidence above the threshold.
    relevance = editorial.get("site_relevance") or {}
    decision = relevance.get("decision")
    confidence = relevance.get("confidence")
    try:
        confidence = float(confidence)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        confidence = 0.0
    relevance_ok = decision == "process" and confidence >= config.min_relevance_confidence
    check(
        "relevancia",
        relevance_ok,
        f"decision={decision or 'ausente'}, confidence={confidence:.2f} "
        f"(minimo {config.min_relevance_confidence:.2f})",
    )

    # 4. Content must be non-empty and readable.
    cleaned = editorial.get("cleaned_html")
    content_ok = isinstance(cleaned, str) and bool(cleaned.strip())
    check("conteudo_nao_vazio", content_ok, "cleaned_html preenchido" if content_ok else "cleaned_html vazio")

    # 5. Source block: original_link exists -> canonical Fonte must be present.
    meta = post.get("meta") if isinstance(post.get("meta"), Mapping) else {}
    original_link = meta.get("original_link")
    original_link = original_link.strip() if isinstance(original_link, str) else None
    if original_link:
        has_fonte = "Fonte:" in content and original_link in content
        check("fonte_original_link", has_fonte, f"original_link presente; bloco Fonte {'ok' if has_fonte else 'AUSENTE no conteudo'}")
    else:
        check("fonte_original_link", True, "sem original_link; bloco Fonte nao exigido", skipped=True)

    # 6. Body images per content length (SEO rule: 2/4/6 minimum).
    words = word_count(content)
    required = minimum_image_count(words)
    inline_images = _IMG_RE.findall(content)
    image_count = len(inline_images)
    images_ok = image_count >= required
    check(
        "imagens_no_corpo",
        images_ok,
        f"{words} palavras exigem >= {required} imagens; conteudo tem {image_count}",
    )

    # 7. Featured image is mandatory before publishing.
    featured_raw = post.get("featured_media")
    featured = featured_raw if isinstance(featured_raw, int) and featured_raw > 0 else None
    featured_ok = featured is not None
    check(
        "imagem_destaque",
        featured_ok,
        f"featured_media={featured_raw or 'vazio'} — obrigatoria para publicar",
    )

    # 7b. Featured image must obey the portal ratio: exactly 1200x720.
    featured_details: dict[str, Any] = {}
    if featured_ok and client is not None:
        try:
            media = client.get_media(featured)
            details = media.get("media_details") or {}
            if isinstance(details, dict):
                featured_details = details
        except Exception:
            pass
    if featured_ok:
        width = featured_details.get("width")
        height = featured_details.get("height")
        proportion_ok = width == 1200 and height == 720
        check(
            "destaque_1200x720",
            proportion_ok,
            f"dimensoes atuais: {width or 'desconhecida'}x{height or 'desconhecida'} (exigido 1200x720)",
        )
    else:
        check("destaque_1200x720", True, "sem destaque para verificar", skipped=True)

    # 8. Every published image must be WebP.
    image_urls = list(inline_images)
    if featured_ok and client is not None:
        try:
            media = client.get_media(featured)
            source_url = (media.get("source_url") or "").strip()
            if source_url:
                image_urls.append(source_url)
        except Exception:
            pass  # media lookup failure is reported by the item below
    non_webp = [url for url in image_urls if not url.lower().endswith(".webp")]
    if image_urls:
        check("imagens_webp", not non_webp, f"{len(image_urls)} imagem(ns); nao-webp: {len(non_webp)}")
    else:
        check("imagens_webp", True, "sem imagens para verificar", skipped=True)

    # 9. Trailer: game content must carry a validated YouTube embed.
    game_name = editorial.get("game_name")
    if isinstance(game_name, str) and game_name.strip():
        has_trailer = bool(_IFRAME_RE.search(content))
        check("trailer_youtube", has_trailer, f"game_name={game_name!r}; embed {'presente' if has_trailer else 'AUSENTE'}")
    else:
        check("trailer_youtube", True, "conteudo nao e de jogo; trailer nao exigido", skipped=True)

    # 10. Canonical CTA must be present.
    check("cta_canonico", _CTA_MARKER in content, "CTA canonico presente" if _CTA_MARKER in content else "CTA AUSENTE")

    # 11. Text quality gates (keyword, dashes, AI phrasing, subheadings).
    try:
        quality = validate_content_quality(
            content,
            title=str(editorial.get("seo", {}).get("title") or ""),
            focus_keyword=str(editorial.get("seo", {}).get("focus_keyword") or ""),
            image_count=image_count,
            matched_topics=relevance.get("matched_topics") or [],
        )
        check("qualidade_texto", True, f"{quality['words']} palavras, qualidade ok")
    except ContentQualityError as exc:
        check("qualidade_texto", False, str(exc))

    # 12. Numbered-list structural validation when applicable.
    try:
        validate_list_content(
            str((post.get("title") or {}).get("raw") or editorial.get("seo", {}).get("title") or ""),
            content,
        )
        check("estrutura_lista", True, "estrutura de lista validada (ou nao aplicavel)")
    except ListContentError as exc:
        check("estrutura_lista", False, str(exc))

    # 13. Strict editorial schema validation.
    try:
        validate_editorial(editorial, min_confidence=config.min_relevance_confidence)
        check("schema_editorial", True, "JSON editorial valido no schema estrito")
    except EditorialValidationError as exc:
        check("schema_editorial", False, str(exc))

    passed = sum(1 for item in items if item["status"] == "pass")
    skipped = sum(1 for item in items if item["status"] == "skip")
    failed = sum(1 for item in items if item["status"] == "fail")
    return {
        "post_id": post.get("id"),
        "items": items,
        "passed": passed,
        "skipped": skipped,
        "failed": failed,
        "all_passed": failed == 0,
    }
