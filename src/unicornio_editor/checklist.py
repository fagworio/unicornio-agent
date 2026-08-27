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
from .media.vision_gate import VisionGateError, verify_image_subject, vision_config_ready
from .wordpress import WordPressClient

_CTA_MARKER = "Confira mais novidades em nosso Portal de"
_IMG_RE = re.compile(r"<img\b[^>]*\bsrc=\"([^\"]+)\"", re.IGNORECASE)
_IFRAME_RE = re.compile(r"<iframe\b[^>]*youtube", re.IGNORECASE)


def _required_image_count(words: int, *, title: str, content: str) -> int:
    """Minimum body images for the post.

    Plain articles follow the 2/4/6 SEO rule (by word count). Listicles
    follow their structural rule of one image per numbered item instead:
    ``max(2, item_count)`` — so a 5-item list with 5 images passes even
    though 2/4/6 would demand 6, and the image rule never conflicts with
    ``estrutura_lista``.
    """
    from .list_quality import detect_list_format

    promised = detect_list_format(title or "", content or "")
    if promised is not None:
        return max(2, promised)
    return minimum_image_count(words)


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

    # 6. Body images per content length (SEO rule: 2/4/6 minimum). The
    #    minimum ALWAYS holds — an image-less post must not be published.
    #    Listicles get their own floor (max(2, item count)) so the 2/4/6 rule
    #    never conflicts with the one-image-per-item structural rule.
    words = word_count(content)
    required = _required_image_count(
        words, title=str((post.get("title") or {}).get("raw") or ""), content=content
    )
    inline_images = _IMG_RE.findall(content)
    image_count = len(inline_images)
    check(
        "imagens_no_corpo",
        image_count >= required,
        f"{words} palavras exigem >= {required} imagens; conteudo tem {image_count}",
    )

    # 6b. Every inline image must be semantically related to the cited subject
    #     (deterministic entity-overlap gate; generic concept matches fail).
    from .media.relevance import extract_entities, image_is_relevant, iter_content_images

    content_images = iter_content_images(content)
    image_entities = extract_entities(
        title=str(editorial.get("seo", {}).get("title") or ""),
        content_html=str(editorial.get("cleaned_html") or ""),
        focus_keyword=str(editorial.get("seo", {}).get("focus_keyword") or ""),
        game_name=editorial.get("game_name"),
    )
    irrelevant_images = [
        item
        for item in content_images
        if not image_is_relevant(
            alt_text=str(item.get("alt") or ""),
            credit_text=str(item.get("caption") or ""),
            source_url=str(item.get("src") or ""),
            entities=image_entities,
        )
    ]
    if content_images:
        check(
            "relevancia_imagens",
            not irrelevant_images,
            f"{len(content_images) - len(irrelevant_images)} relevante(s) de "
            f"{len(content_images)} imagem(ns); irrelevantes: "
            f"{', '.join(item.get('alt') or item.get('src') or '?' for item in irrelevant_images) or 'nenhuma'}",
        )
    else:
        check("relevancia_imagens", True, "sem imagens para validar", skipped=True)

    # 6c. No repeated image: each inline image must be distinct. Reusing the
    #     same URL several times in a post is low-quality editorial (the exact
    #     "same key art reused throughout" failure observed in production). A
    #     single source appearing more than once blocks READY until the agent
    #     replaces the duplicates with distinct imagery of the same work.
    from collections import Counter as _Counter

    src_counts = _Counter(str(item.get("src") or "").strip() for item in content_images)
    repeated = [f"{src} (x{count})" for src, count in src_counts.items() if count > 1 and src]
    if content_images:
        check(
            "imagens_duplicadas",
            not repeated,
            (
                f"{len(content_images)} imagem(ns); repetidas: {', '.join(repeated[:3])}"
                if repeated
                else f"{len(content_images)} imagem(ns) distintas"
            ),
        )
    else:
        check("imagens_duplicadas", True, "sem imagens para validar", skipped=True)

    # 7. Featured image is mandatory before publishing.
    featured_raw = post.get("featured_media")
    featured = featured_raw if isinstance(featured_raw, int) and featured_raw > 0 else None
    featured_ok = featured is not None
    check(
        "imagem_destaque",
        featured_ok,
        f"featured_media={featured_raw or 'vazio'} — obrigatoria para publicar",
    )

    # 7b. Featured image must obey the portal ratio: exactly 1280x720.
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
        proportion_ok = width == 1280 and height == 720
        check(
            "destaque_1280x720",
            proportion_ok,
            f"dimensoes atuais: {width or 'desconhecida'}x{height or 'desconhecida'} (exigido 1280x720)",
        )
    else:
        check("destaque_1280x720", True, "sem destaque para verificar", skipped=True)

    # 7c. Featured must depict the cited subject itself (source-only gate):
    #     the agent-written alt/credit can decorate a wrong image (e.g. a
    #     Disney castle captioned as Kingdom Hearts), but the real source
    #     file/page name of a true key art carries the game/work name.
    #     The REAL featured attachment is validated when the post already has
    #     one: url + title + alt of the attachment are evidence from the
    #     actual source (a normalized/reused featured keeps the original
    #     provenance), never agent-written text. The media_plan item is only
    #     consulted as intent before any media was uploaded (standalone
    #     checklist runs, dry-run).
    featured_plan_items = [
        item
        for item in (editorial.get("media_plan") or [])
        if isinstance(item, Mapping) and bool(item.get("is_featured"))
    ]
    featured_source_relevant: bool | None = None
    if featured_ok and client is not None:
        try:
            media = client.get_media(featured)
            mtitle = str((media.get("title") or {}).get("rendered") or "")
            malt = str(media.get("alt_text") or "")
            murl = str(media.get("source_url") or "")
            featured_source_relevant = image_is_relevant(
                alt_text="",
                credit_text="",
                source_url=" ".join(part for part in (murl, mtitle, malt) if part),
                entities=image_entities,
                source_only=True,
            )
        except Exception:
            featured_source_relevant = None
    if featured_source_relevant is None and featured_plan_items:
        featured_item = featured_plan_items[0]
        featured_source_relevant = image_is_relevant(
            alt_text="",
            credit_text="",
            source_url=" ".join(
                str(featured_item.get(key) or "")
                for key in ("direct_image_url", "source_page_url")
            ),
            search_query=str(featured_item.get("search_query") or ""),
            entities=image_entities,
            source_only=True,
        )
    if featured_source_relevant is None:
        check("destaque_relevancia", True, "sem destaque para verificar", skipped=True)
    else:
        listed = ", ".join(sorted(image_entities)) or "nenhuma"
        check(
            "destaque_relevancia",
            featured_source_relevant,
            (
                "destaque retrata o assunto citado (origem com entidade: "
                f"{', '.join(sorted(image_entities)) or 'nenhuma'})"
                if featured_source_relevant
                else f"destaque SEM relacao com o assunto (origem sem entidades: {listed}); "
                "troque por key art/imagem do jogo/obra"
            ),
        )

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

    # 8b. Every inline image must declare real dimensions inside the portal
    #     content range (MIN..MAX wide) — the converter enforces it at apply
    #     time; this gate re-checks the final published content so a wrong
    #     source (tiny thumbnail, stretched art) cannot slip through.
    from .media.converter import MAX_INLINE_WIDTH, MIN_INLINE_WIDTH

    bad_dimensions: list[str] = []
    for tag in re.findall(r"<img\b[^>]*>", content, flags=re.IGNORECASE):
        width_match = re.search(r'\bwidth="(\d+)"', tag, flags=re.IGNORECASE)
        height_match = re.search(r'\bheight="(\d+)"', tag, flags=re.IGNORECASE)
        if not width_match or not height_match:
            bad_dimensions.append("sem width/height")
            continue
        width = int(width_match.group(1))
        height = int(height_match.group(1))
        if not MIN_INLINE_WIDTH <= width <= MAX_INLINE_WIDTH or height <= 0:
            bad_dimensions.append(f"{width}x{height}")
    if inline_images:
        check(
            "dimensoes_imagens",
            not bad_dimensions,
            f"{len(inline_images)} imagem(ns); fora do padrao {MIN_INLINE_WIDTH}-{MAX_INLINE_WIDTH}px: "
            f"{', '.join(bad_dimensions) or 'nenhuma'}",
        )
    else:
        check("dimensoes_imagens", True, "sem imagens para verificar", skipped=True)

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
            allowed_topics=config.site_topics,
            required_images=required,
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

    # 14. Vision gate (optional, fail-closed when enabled): a cheap vision
    #     model confirms each published image depicts its alt subject. This
    #     catches a CDN serving the wrong image under a correct slug — the
    #     one case the deterministic gates cannot see. Only posts that passed
    #     every other gate pay for vision calls.
    vision_ready, vision_msg = vision_config_ready(
        enabled=config.vision_enabled, api_key=config.vision_api_key
    )
    if not vision_ready:
        check("imagens_visao", True, vision_msg, skipped=True)
    elif any(item["status"] == "fail" for item in items):
        check(
            "imagens_visao",
            True,
            "post ja bloqueado por outro gate; verificacao de visao nao executada",
            skipped=True,
        )
    else:
        from .media.vision_cache import get_cached_decision, set_cached_decision

        # Root do projeto a partir do snapshot backups/<id>/snapshot.json.
        vision_root = (
            Path(str(backup_path)).parents[2] if backup_path else Path(".")
        )
        vision_failures: list[str] = []
        calls_low = 0
        calls_high = 0
        checked = 0

        def _verify(
            url: str, subject: str, *,
            is_featured: bool, context: str = "", category: str = "",
        ) -> None:
            nonlocal calls_low, calls_high, checked
            if not url or not subject.strip():
                return
            # Cache por imagem+entidade: evita re-analisar a mesma key art
            # em varios posts/artigos.
            cached = get_cached_decision(vision_root, url, subject)
            if cached is not None:
                checked += 1
                if cached.get("status") == "MATCH" and float(cached.get("confidence") or 0) >= 0.85:
                    return
                vision_failures.append(f"{url[:60]}: cache nao-confirma ({cached.get('status')})")
                return
            # Limites mecanicos de custo por post.
            if calls_low >= config.vision_max_low:
                vision_failures.append(f"{url[:60]}: limite de chamadas low atingido")
                return
            calls_low += 1
            try:
                ok, reason = verify_image_subject(
                    image_url=url,
                    subject=subject,
                    api_key=config.vision_api_key,
                    base_url=config.vision_base_url,
                    model=config.vision_model,
                    timeout=config.http_timeout,
                    context=context,
                    category=category,
                    alt=subject,
                    detail=config.vision_detail,
                    allow_high=is_featured,  # featured escala low->high; inline nao
                    require_key_art=is_featured,  # key art nao pode ser banner tipografico/infografico
                )
                checked += 1
                if ok:
                    set_cached_decision(
                        vision_root, url, subject,
                        {"status": "MATCH", "confidence": 1.0, "visual_type": "other"},
                    )
                    return
                # Escalonou para high na featured? Contabiliza para o limite.
                if is_featured and "high" in str(config.vision_detail):
                    calls_high += 1
                vision_failures.append(f"{url[:60]}: {reason}")
            except VisionGateError as exc:
                vision_failures.append(f"{url[:60]}: {exc}")
            except Exception as exc:  # noqa: BLE001 - report, keep gate
                vision_failures.append(f"{url[:60]}: {exc}")

        # Inline: gate determinístico ja passou; low apenas (descarta se nao confirmar).
        for item in content_images:
            _verify(
                str(item.get("src") or ""),
                str(item.get("alt") or ""),
                is_featured=False,
                category="game_artwork" if image_entities else "media",
            )
        # Featured: low -> high obrigatorio (a imagem mais importante).
        if featured_ok and client is not None:
            try:
                media = client.get_media(featured)
                featured_url = str(media.get("source_url") or "").strip()
                featured_subject = str(editorial.get("seo", {}).get("title") or "").strip()
                if featured_url and featured_subject:
                    _verify(
                        featured_url, featured_subject,
                        is_featured=True,
                        context="image destaque do artigo",
                        category="game_artwork",
                    )
            except (VisionGateError, Exception) as exc:  # noqa: BLE001 - report, keep gate
                vision_failures.append(f"destaque: {exc}")
        if vision_failures:
            check("imagens_visao", False, "; ".join(vision_failures[:3]))
        else:
            check(
                "imagens_visao",
                True,
                f"{checked} imagem(ns) confirmadas (low={calls_low}, high={calls_high})",
            )

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
