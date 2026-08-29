"""Application workflows shared by the CLI and integration tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import datetime
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from .backup import SnapshotStore
from .builder import append_canonical_footer
from .checklist import _required_image_count, run_pre_publish_checklist
from .config import Config
from .editorial_schema import validate_editorial
from .html_cleaner import _repair_orphan_media, clean_html
from .list_quality import detect_list_format
from .locking import LockError, LockManager
from .manifest import (
    META_READY_MANIFEST,
    build_ready_manifest,
    manifest_hash,
    manifest_matches,
    parse_manifest,
    serialize_manifest,
)
from .media.converter import (
    convert_to_webp,
    image_dimensions,
    image_has_transparency,
    image_is_mostly_flat,
    prepare_featured_webp,
)
from .media.downloader import download_image
from .media.inserter import append_featured_credit, insert_media
from .media.relevance import extract_entities, image_is_relevant, iter_content_images
from .media.source_verify import verify_downloaded_against_source
from .media.wordpress_media import upload_image
from .observability import append_telemetry, build_processing_markers
from .seo.rank_math import build_meta
from .state import (
    STATE_AWAITING_HUMAN,
    STATE_BLOCKED,
    STATE_NEW,
    STATE_PUBLISHED,
    STATE_READY,
    STATE_SKIPPED,
    STATE_UNCERTAIN,
    build_state_markers,
    cooldown_expired,
    read_state,
    retry_eligible,
    rework_backoff,
)
from .trailer import TrailerError, build_trailer_html, find_game_trailer
from .wordpress import WordPressClient


# Media plan: download/upload/verificacao sao I/O-bound (rede); threads
# liberam o GIL durante E/S. Falhas de item são registradas no próprio item;
# nunca reexecutamos um lote parcial, pois upload é efeito colateral.
_MEDIA_WORKERS = 4


class WorkflowError(RuntimeError):
    """Raised when a post cannot safely enter a workflow step."""


def _acquire_post_lock(root: Path, config: Config, post_id: int):
    """Serialize every mutating operation for one post.

    The WordPress status re-fetch protects against a late manual publish, but
    it cannot prevent two cron sessions from uploading the same media in
    parallel. The filesystem lock covers that expensive side effect.
    """
    try:
        return LockManager(root / "work" / "locks", ttl=config.lock_ttl).acquire(post_id)
    except LockError as exc:
        raise WorkflowError(f"post {post_id} is already being processed") from exc


def prepare_post(client: WordPressClient, root: Path, post_id: int) -> dict[str, Any]:
    post = client.get_post(post_id)
    _require_pending(post)
    backup = SnapshotStore(root).save(post_id, post)
    raw = _raw_content(post)
    return {
        "post_id": post_id,
        "status": post["status"],
        "backup": str(backup),
        "cleaned_html": clean_html(_repair_orphan_media(raw)),
        "original_link": _original_link(post),
        "wordpress_changed": False,
    }


def apply_editorial(
    client: WordPressClient,
    config: Config,
    root: Path,
    post_id: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Apply an editorial result while exclusively owning the post."""
    with _acquire_post_lock(root, config, post_id):
        return _apply_editorial_unlocked(client, config, root, post_id, payload)


def _apply_editorial_unlocked(
    client: WordPressClient,
    config: Config,
    root: Path,
    post_id: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Preflight completo: valida, resolve, executa mídia, monta conteúdo e
    roda o checklist INTEIRO antes de gravar qualquer coisa no WordPress.

    Somente um apply com checklist 100% (``checklist.failed == 0``) escreve o
    conteúdo e marca o post ``READY`` (meta ``_hermes_state``) com o Ready
    Manifest (hash SHA-256). Qualquer falha -> ``needs_rework`` + estado
    ``blocked`` (com contagem de tentativas e ``next_retry_at`` — backoff
    30m/2h, 3ª falha vira AWAITING_HUMAN). Nenhum post quebrado chega ao
    publish: o publish-ready apenas confirma o hash.
    """
    post = client.get_post(post_id)
    _require_pending(post)
    backup = SnapshotStore(root).save(post_id, post)
    editorial = validate_editorial(payload, min_confidence=config.min_relevance_confidence)
    decision = editorial["site_relevance"]["decision"]
    confidence = float(editorial["site_relevance"].get("confidence") or 0.0)
    if decision == "skip" and confidence < config.min_skip_confidence:
        # Conservative skip (token + accuracy policy): a low-confidence skip is
        # NOT final — record it as uncertain so the post stays pending (out of
        # the processing queue, visible for review) instead of being dropped
        # forever via editorial.latest.json.
        _save_uncertain(root, post_id, editorial)
        _backoff_u = rework_backoff(
            read_state(post)["attempts"] + 1,
            cooldown_minutes=config.rework_cooldown_minutes,
            max_attempts=config.max_rework_attempts,
        )
        _write_state_markers(
            client,
            config,
            post_id,
            STATE_UNCERTAIN,
            attempts=_backoff_u["attempts"],
            next_retry_at=_backoff_u["next_retry_at"],
            last_error=editorial["site_relevance"]["reason"],
        )
        append_telemetry(
            root, "apply_uncertain",
            post_id=post_id, reason=editorial["site_relevance"]["reason"],
        )
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "dry_run": config.dry_run,
            "status": "uncertain",
            "state": STATE_UNCERTAIN,
            "skip_reason": editorial["site_relevance"]["reason"],
            "confidence": confidence,
            "backup": str(backup),
        }
    if decision == "process":
        editorial = resolve_editorial_defaults(editorial, post)
    _save_editorial_latest(root, post_id, editorial)
    if decision == "skip":
        _write_state_markers(
            client,
            config,
            post_id,
            STATE_SKIPPED,
            last_error=editorial["site_relevance"]["reason"],
        )
        append_telemetry(
            root, "apply_skipped",
            post_id=post_id, reason=editorial["site_relevance"]["reason"],
        )
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "dry_run": config.dry_run,
            "status": "skipped",
            "state": STATE_SKIPPED,
            "skip_reason": editorial["site_relevance"]["reason"],
            "backup": str(backup),
        }

    # Draft persistido ANTES da execução pesada: mesmo que download/upload/
    # checklist falhem, o trabalho editorial fica salvo e o rework corrige
    # somente o componente com problema (nunca reescreve o texto nem re-gera
    # SEO do zero).
    _save_draft(root, post_id, editorial)

    media_results, featured_id, featured_credit = _execute_media_plan(editorial, config, client)
    if featured_id is None and not config.dry_run:
        featured_id = _normalize_existing_featured(client, config, post, editorial)
    html = editorial["cleaned_html"]
    if media_results and not config.dry_run:
        plan = [
            {
                "paragraph_index": result["paragraph_index"],
                "media_url": result["media_url"],
                "alt_text": result["alt_text"],
                "credit_text": result["credit_text"],
                "width": result.get("width"),
                "height": result.get("height"),
            }
            for result in media_results
            if result.get("media_url") and not result.get("featured")
        ]
        if plan:
            is_list = bool(
                detect_list_format(
                    _post_title(post) or editorial["seo"]["title"], html
                )
            )
            html = insert_media(html, plan, listicle=is_list)
    editorial_with_media = {**editorial, "cleaned_html": html}
    content, trailer = compose_final_content(editorial_with_media, config, original_link_of(post))
    if featured_credit and not config.dry_run:
        content = append_featured_credit(content, featured_credit)
    inline_normalization: list[dict[str, Any]] = []
    image_entities = extract_entities(
        title=str(editorial["seo"].get("title") or ""),
        content_html=html,
        focus_keyword=str(editorial["seo"].get("focus_keyword") or ""),
        game_name=editorial.get("game_name"),
    )
    if not config.dry_run:
        # Normalização técnica sem LLM (Fase 5.2): imagens inline relevantes
        # em formato errado (JPEG/PNG) viram WebP local automaticamente —
        # problema técnico não volta ao modelo. Irrelevantes ficam como estão:
        # o gate relevancia_imagens bloqueia e o agente corrige.
        content, inline_normalization = _normalize_inline_images(client, config, content, image_entities)
        editorial_with_media = {**editorial_with_media, "cleaned_html": content}
    # Tentativas anteriores (antes desta): base do teto deterministico de
    # buscas de imagem. Cada apply falho = 1 busca completa esgotada.
    attempts_before = read_state(post)["attempts"]
    checklist = run_pre_publish_checklist(
        post={**post, "featured_media": featured_id or post.get("featured_media")},
        editorial=editorial_with_media,
        content=content,
        backup_path=backup,
        config=config,
        client=client,
        attempts=attempts_before,
    )
    # GATE COMPLETO (politica verificar -> corrigir -> publicar): qualquer
    # item do checklist com falha impede READY — o apply NUNCA grava um post
    # que o publish-ready bloquearia depois. O editorial fica arquivado em
    # editorial.blocked.json (rascunho preservado em editorial.draft.json) e
    # o post volta à fila de rework com backoff (30m/2h -> AWAITING_HUMAN).
    if not config.dry_run:
        failed_items = [
            item
            for item in (checklist.get("items") or [])
            if item.get("status") in ("fail", "error") and item.get("name")
        ]
        if failed_items:
            state_info = read_state(post)
            attempts = state_info["attempts"] + 1
            # Listicle com busca de imagens esgotada -> AWAITING_HUMAN direto
            # (revisao manual), sem loop de rework que so queimaria token.
            media_exhausted = bool(editorial.get("media_exhausted"))
            deterministic_exhausted = attempts >= config.max_media_search_attempts
            if (media_exhausted or deterministic_exhausted) and detect_list_format(
                _post_title(post) or editorial["seo"]["title"], content
            ) is not None:
                backoff = {
                    "state": STATE_AWAITING_HUMAN,
                    "attempts": attempts,
                    "next_retry_at": "",
                }
            else:
                backoff = rework_backoff(
                    attempts,
                    cooldown_minutes=config.rework_cooldown_minutes,
                    max_attempts=config.max_rework_attempts,
                )
            last_error = "; ".join(
                f"{item['name']}: {str(item.get('detail') or '')[:120]}"
                for item in failed_items[:5]
            )
            _save_blocked(root, post_id, editorial, checklist)
            _write_state_markers(
                client,
                config,
                post_id,
                backoff["state"],
                attempts=backoff["attempts"],
                next_retry_at=backoff["next_retry_at"],
                last_error=last_error,
            )
            images_summary = _images_summary(
                content, _post_title(post) or editorial["seo"]["title"], image_entities
            )
            append_telemetry(
                root, "apply_blocked",
                post_id=post_id,
                attempts=backoff["attempts"],
                reason=", ".join(item["name"] for item in failed_items),
                missing_images=images_summary.get("missing", 0),
                valid_images=images_summary.get("valid", 0),
                blocked_detail=(
                    "; ".join(str(item.get("detail") or "")[:120] for item in failed_items[:3])
                ),
            )
            return {
                "post_id": post_id,
                "wordpress_changed": False,
                "dry_run": False,
                "status": "needs_rework",
                "state": backoff["state"],
                "attempts": backoff["attempts"],
                "next_retry_at": backoff["next_retry_at"],
                "backup": str(backup),
                "checklist": checklist,
                "media_plan_results": media_results,
                "inline_normalization": inline_normalization,
                "blocked_reasons": [item["name"] for item in failed_items],
                "blocked_detail": "; ".join(
                    str(item.get("detail") or "")[:200] for item in failed_items[:3]
                ),
                "images": images_summary,
            }
    if config.dry_run:
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "dry_run": True,
            "backup": str(backup),
            "content_preview": content,
            "trailer": trailer,
            "media_plan_results": media_results,
            "checklist": checklist,
            "images": _images_summary(content, _post_title(post) or editorial["seo"]["title"], image_entities),
        }

    latest = client.get_post(post_id)
    _require_pending(latest)
    manifest = build_ready_manifest(
        post_id=post_id,
        content=content,
        featured_media=featured_id or latest.get("featured_media"),
        seo=editorial["seo"],
        original_link=original_link_of(post),
        editorial=editorial_with_media,
        policy_version=config.policy_version,
    )
    update_payload: dict[str, Any] = {
        "content": {"raw": content},
        "meta": {
            **build_meta(editorial["seo"], latest.get("meta", {})),
            **build_processing_markers(
                editorial["site_relevance"]["decision"],
                editorial["site_relevance"]["confidence"],
            ),
            **build_state_markers(
                STATE_READY,
                ready_hash=manifest_hash(manifest),
                policy_version=config.policy_version,
            ),
            META_READY_MANIFEST: serialize_manifest(manifest),
        },
    }
    if featured_id:
        update_payload["featured_media"] = featured_id
    result = client.update_post(post_id, update_payload)
    # O post saiu do estado de rework: limpa os marcadores para o queue nao
    # continuar listando blocked/uncertain (senao o monitor acordaria o agente
    # em loop para "corrigir" um post ja corrigido).
    _clear_processing_markers(root, post_id)
    append_telemetry(root, "apply_ready", post_id=post_id)
    return {
        "post_id": post_id,
        "wordpress_changed": True,
        "dry_run": False,
        "status": "ready",
        "state": STATE_READY,
        "ready_hash": manifest_hash(manifest),
        "backup": str(backup),
        "status_after": result.get("status"),
        "trailer": trailer,
        "media_plan_results": media_results,
        "inline_normalization": inline_normalization,
        "featured_media": result.get("featured_media"),
        "checklist": checklist,
        "images": _images_summary(content, _post_title(post) or editorial["seo"]["title"], image_entities),
    }


def _clear_processing_markers(root: Path, post_id: int) -> None:
    """Remove the blocked/uncertain markers after a successful apply.

    The post is no longer reopened-for-rework nor uncertain; leaving the
    markers would keep it in the blocked/rework queue forever and re-wake the
    editorial cron to "fix" an already-fixed post (token waste + stuck loop).
    """
    try:
        directory = root / "backups" / str(post_id)
        for name in ("editorial.blocked.json", "uncertain.json"):
            marker = directory / name
            if marker.is_file():
                marker.unlink()
    except OSError:
        pass


def _write_state_markers(
    client: WordPressClient,
    config: Config,
    post_id: int,
    state: str,
    *,
    attempts: int = 0,
    next_retry_at: str = "",
    last_error: str = "",
    ready_hash: str = "",
) -> None:
    """Persiste o estado operacional ``_hermes_*`` no WordPress (write mode).

    Telemetria de estado: falha aqui não derruba o fluxo — o pior caso é o
    post ficar sem estado e o publish-ready revalidar pelo checklist (mais
    caro, nunca inseguro).
    """
    if config.dry_run:
        return
    try:
        client.update_post(
            post_id,
            {
                "meta": build_state_markers(
                    state,
                    attempts=attempts,
                    next_retry_at=next_retry_at,
                    last_error=last_error,
                    ready_hash=ready_hash,
                    policy_version=config.policy_version,
                )
            },
        )
    except Exception:  # noqa: BLE001 - telemetria nunca bloqueia o fluxo
        pass


def _save_draft(root: Path, post_id: int, editorial: dict[str, Any]) -> None:
    """Persiste o rascunho editorial resolvido ANTES da execução pesada.

    ``editorial.draft.json`` é a base do rework: o agente carrega o rascunho,
    corrige SOMENTE o componente com problema (media_plan, seo, texto) e
    re-aplica — o trabalho editorial caro nunca é refeito do zero.
    """
    try:
        directory = root / "backups" / str(post_id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "editorial.draft.json").write_text(
            json.dumps(editorial, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def _inline_filename_from_source(source_url: str, width: int, height: int) -> str:
    """Nome de arquivo com proveniência para imagens inline normalizadas.

    Mantém o slug da fonte original no nome (evidência para o gate
    determinístico de relevância) e anota as dimensões reais.
    """
    stem = Path(source_url.split("?", 1)[0]).stem or ""
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    if len(slug) < 5:
        return f"inline-{width}x{height}.webp"
    return f"{slug[:80].strip('-')}-{width}x{height}.webp"


def _normalize_inline_images(
    client: WordPressClient,
    config: Config,
    html: str,
    entities: set[str],
) -> tuple[str, list[dict[str, Any]]]:
    """Re-upload de imagens inline não-WebP (relevantes) como WebP local.

    Problema técnico (formato/dimensão) não volta ao modelo: imagem já
    relevante e com crédito é baixada, convertida (transparência achatada,
    largura limitada a 1280px), re-upload como NOVO attachment preservando
    alt/credit, e a URL trocada no conteúdo — o WebP publicado nunca é
    transparente (política). Imagens irrelevantes ou cujo download falha
    ficam como estão: o gate relevancia_imagens/imagens_webp bloqueia o
    apply e o agente decide (substituir/remover) com o delta do card.
    """
    if not entities:
        return html, []
    images = {str(item.get("src") or ""): item for item in iter_content_images(html)}
    if not images:
        return html, []
    results: list[dict[str, Any]] = []

    def _replace(match: re.Match[str]) -> str:
        tag = match.group(0)
        src_match = re.search(r'\bsrc="([^"]+)"', tag, flags=re.IGNORECASE)
        if not src_match:
            return tag
        src = src_match.group(1)
        if src.lower().split("?", 1)[0].endswith(".webp"):
            return tag
        info = images.get(src) or {}
        alt = str(info.get("alt") or "")
        caption = str(info.get("caption") or "")
        if not image_is_relevant(
            alt_text=alt,
            credit_text=caption,
            source_url=src,
            entities=entities,
        ):
            results.append(
                {
                    "src": src[:80],
                    "status": "irrelevant",
                    "detail": "sem relacao com o conteudo; deixada como esta (gate relevancia bloqueia)",
                }
            )
            return tag
        try:
            with tempfile.TemporaryDirectory(prefix="unicornio-inline-") as directory:
                tmp = Path(directory)
                suffix = Path(src.split("?", 1)[0]).suffix or ".jpg"
                source = download_image(
                    src,
                    tmp / f"inline_source{suffix}",
                    max_attempts=config.max_source_retries + 1,
                )
                webp = convert_to_webp(source, tmp / "inline.webp")
                width, height = image_dimensions(webp)
                filename = _inline_filename_from_source(src, width, height)
                media = client.upload_media(
                    str(webp),
                    filename=filename,
                    alt_text=alt,
                    title=caption or alt,
                    caption=caption,
                )
                media_url = str(media.get("source_url") or "").strip()
                if not media_url:
                    raise WorkflowError("inline normalization upload returned no source_url")
        except Exception as exc:  # noqa: BLE001 - download/convert/upload: reporta e segue
            results.append({"src": src[:80], "status": "error", "detail": str(exc)[:140]})
            return tag
        tag = re.sub(r'\bsrc="[^"]*"', f'src="{media_url}"', tag, count=1, flags=re.IGNORECASE)
        tag = re.sub(r'\bwidth="[^"]*"', f'width="{width}"', tag, count=1, flags=re.IGNORECASE)
        tag = re.sub(r'\bheight="[^"]*"', f'height="{height}"', tag, count=1, flags=re.IGNORECASE)
        if not re.search(r"\bwidth=", tag, flags=re.IGNORECASE):
            stripped = tag.rstrip()
            if stripped.endswith("/>"):
                tag = stripped[:-2] + f' width="{width}" height="{height}" />'
            elif stripped.endswith(">"):
                tag = stripped[:-1] + f' width="{width}" height="{height}">'
        results.append(
            {
                "src": src[:80],
                "status": "normalized",
                "media_url": media_url,
                "width": width,
                "height": height,
            }
        )
        return tag

    normalized = re.sub(r"<img\b[^>]*>", _replace, html, flags=re.IGNORECASE)
    return normalized, results


def _images_summary(content: str, title: str, entities: set[str] | None = None) -> dict[str, int]:
    """Delta de imagens determinístico: quanto o conteúdo TEM vs PRECISA.

    ``required`` segue a política 2/4/6 (listicle = max(2, itens));
    ``valid`` conta as inline relevantes; ``missing`` é o que falta para
    READY; ``irrelevant``/``non_webp`` são os problemas técnicos que o
    código resolve (non_webp relevante é normalizado automaticamente).
    """
    from .content_quality import word_count

    words = word_count(content)
    required = _required_image_count(words, title=title or "", content=content)
    images = iter_content_images(content)
    relevant = [
        item
        for item in images
        if image_is_relevant(
            alt_text=str(item.get("alt") or ""),
            credit_text=str(item.get("caption") or ""),
            source_url=str(item.get("src") or ""),
            entities=entities or set(),
        )
    ]
    non_webp = sum(
        1
        for item in images
        if not str(item.get("src") or "").lower().split("?", 1)[0].endswith(".webp")
    )
    from collections import Counter as _Counter

    src_counts = _Counter(str(item.get("src") or "").strip() for item in images)
    duplicates = sum(count - 1 for src, count in src_counts.items() if count > 1 and src)
    return {
        "required": required,
        "valid": len(relevant),
        "missing": max(0, required - len(relevant)),
        "irrelevant": len(images) - len(relevant),
        "non_webp": non_webp,
        "duplicates": duplicates,
    }


def _media_item_rejection(
    item: dict[str, Any],
    entities: set[str],
    client: WordPressClient,
    attachment_cache: dict[int, dict[str, Any]],
) -> str | None:
    """Motivo de rejeicao de um item do media_plan, ou None se valido.

    Compartilhada pelo ``_execute_media_plan`` (apply) e pelo
    ``validate_media_plan`` (media-validate, 1 chamada antes do apply):
    reuso da Media Library exige credito visivel no attachment; featured deve
    retratar o assunto citado pela evidencia real (arquivo/pagina de origem);
    inline deve referenciar entidade distintiva do post.
    """
    is_featured = bool(item.get("is_featured"))
    media_id = item.get("media_library_id")
    attachment = None
    if media_id:
        if media_id not in attachment_cache:
            attachment_cache[media_id] = client.get_media(media_id)
        attachment = attachment_cache[media_id]
    if attachment is not None:
        title = str((attachment.get("title") or {}).get("rendered") or "")
        alt = str(attachment.get("alt_text") or "")
        caption = str((attachment.get("caption") or {}).get("rendered") or "")
        url = str(attachment.get("source_url") or "")
        credit = title or caption
        if "crédito da imagem" not in credit.lower():
            return (
                "reuso da midia library exige credito visivel no attachment original "
                "(title/caption sem 'Crédito da imagem'); nao usar como fonte"
            )
        source = " ".join(part for part in (url, title, alt, caption) if part)
        if is_featured:
            if not image_is_relevant(
                alt_text="", credit_text="", source_url=source,
                search_query=str(item.get("search_query") or ""),
                entities=entities, source_only=True,
            ):
                listed = ", ".join(sorted(entities)) or "nenhuma"
                return (
                    "featured reusada deve retratar o assunto citado "
                    f"(attachment sem as entidades: {listed}); escolha key art/imagem do jogo/obra"
                )
            return None
        if not image_is_relevant(
            alt_text=str(item.get("alt_text") or ""),
            credit_text=str(item.get("credit_text") or ""),
            source_url=source,
            search_query=str(item.get("search_query") or ""),
            entities=entities,
        ):
            listed = ", ".join(sorted(entities)) or "nenhuma"
            return f"imagem sem relacao com o conteudo (entidades distintas: {listed})"
        return None
    if is_featured:
        # Featured must depict the cited subject itself: only the real
        # source file/page name counts as evidence. The agent-written
        # alt/credit can decorate a wrong image (e.g. a Disney castle
        # captioned "presente em Kingdom Hearts" for a game post), but a
        # true key art file name carries the game/work name.
        if not image_is_relevant(
            alt_text="",
            credit_text="",
            source_url=" ".join(
                str(item.get(key) or "") for key in ("direct_image_url", "source_page_url")
            ),
            search_query=str(item.get("search_query") or ""),
            entities=entities,
            source_only=True,
        ):
            listed = ", ".join(sorted(entities)) or "nenhuma"
            return (
                "featured deve retratar o assunto citado (arquivo/pagina de origem "
                f"sem as entidades: {listed}); escolha key art/imagem do jogo/obra"
            )
        return None
    if not image_is_relevant(
        alt_text=str(item.get("alt_text") or ""),
        credit_text=str(item.get("credit_text") or ""),
        source_url=" ".join(
            str(item.get(key) or "") for key in ("direct_image_url", "source_page_url")
        ),
        search_query=str(item.get("search_query") or ""),
        entities=entities,
    ):
        listed = ", ".join(sorted(entities)) or "nenhuma"
        return f"imagem sem relacao com o conteudo (entidades distintas: {listed})"
    return None


def _plan_source_key(item: dict[str, Any]) -> str:
    """Chave de fonte de um item do media_plan para deteccao de duplicatas.

    Reuso da Media Library -> attachment id; novo -> URL direta. O mesmo
    conteudo visual nao pode entrar duas vezes (politica anti-repeticao).
    """
    media_id = item.get("media_library_id")
    if media_id:
        return f"lib:{media_id}"
    return f"url:{str(item.get('direct_image_url') or '').strip()}"


def _duplicate_source_reason(plan: list[dict[str, Any]], index: int, seen: set[str]) -> str | None:
    """Motivo de rejeicao quando a fonte ja aparece em item anterior do plano."""
    key = _plan_source_key(plan[index])
    if not key or key.endswith(":"):
        return None
    if key in seen:
        return (
            "imagem repetida no media_plan (mesma fonte ja usada em outro item); "
            "cada imagem do post deve ser distinta — troque por outra captura/ângulo da obra"
        )
    seen.add(key)
    return None


def validate_media_plan(
    client: WordPressClient,
    editorial: dict[str, Any],
) -> dict[str, Any]:
    """Valida o media_plan de um editorial SEM executar download/upload.

    Retorna ``{valid, rejected: [{index, reason}]}`` — o agente corrige o
    plano antes do apply (1 chamada compacta em vez de aplicar e ver itens
    rejeitados no resultado). Deterministico e somente leitura.
    """
    plan = editorial.get("media_plan") or []
    if not plan:
        return {"valid": 0, "rejected": []}
    entities = extract_entities(
        title=str((editorial.get("seo") or {}).get("title") or ""),
        content_html=str(editorial.get("cleaned_html") or ""),
        focus_keyword=str((editorial.get("seo") or {}).get("focus_keyword") or ""),
        game_name=editorial.get("game_name"),
    )
    cache: dict[int, dict[str, Any]] = {}
    valid = 0
    rejected: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    for index, item in enumerate(plan):
        reason = _media_item_rejection(item, entities, client, cache)
        if reason is None:
            reason = _duplicate_source_reason(plan, index, seen_sources)
        if reason:
            rejected.append({"index": index, "reason": reason})
        else:
            valid += 1
    return {"valid": valid, "rejected": rejected}


def get_cleaned_content(
    client: WordPressClient,
    root: Path,
    post_id: int,
) -> dict[str, Any]:
    """Conteudo limpo do post (somente leitura; sob demanda para reescrita).

    Nao cria snapshot (o apply salva): comando ``content POST_ID`` — o agente
    le o cleaned_html UMA vez quando realmente vai reescrever o texto, em vez
    de abrir o prepared.json inteiro.
    """
    from .content_quality import word_count

    post = client.get_post(post_id)
    _require_pending(post)
    raw = _raw_content(post)
    cleaned = clean_html(_repair_orphan_media(raw))
    return {
        "post_id": post_id,
        "status": post["status"],
        "cleaned_html": cleaned,
        "original_link": _original_link(post),
        "word_count": word_count(cleaned),
    }


def _execute_media_plan(
    editorial: dict[str, Any],
    config: Config,
    client: WordPressClient,
) -> tuple[list[dict[str, Any]], int | None, str | None]:
    """Download, convert to WebP, upload and report the editorial media plan.

    Featured candidates are prepared at exactly 1200x720. In dry-run the plan
    is reported but never executed (uploads are write operations).

    Relevance gate: every candidate must reference a distinctive entity of the
    post (title/keyword/game name). Generic concept matches (e.g. a real bat
    for a game vampire) are rejected before any download/insert — the
    editorial rule is "no image beats a wrong image".
    """
    plan = editorial.get("media_plan") or []
    if not plan:
        return [], None, None
    entities = extract_entities(
        title=str((editorial.get("seo") or {}).get("title") or ""),
        content_html=str(editorial.get("cleaned_html") or ""),
        focus_keyword=str((editorial.get("seo") or {}).get("focus_keyword") or ""),
        game_name=editorial.get("game_name"),
    )

    attachment_cache: dict[int, dict[str, Any]] = {}

    def _attachment_evidence(item: dict[str, Any]) -> dict[str, Any] | None:
        """Resolve a Media Library attachment referenced by ``media_library_id``.

        Reused images are validated against the REAL attachment metadata
        (title/alt/caption/url), never against agent-written text, and are
        re-uploaded as a NEW attachment so the original descriptions are
        never overwritten. Returns None when the item is not a reuse.
        """
        media_id = item.get("media_library_id")
        if not media_id:
            return None
        if media_id not in attachment_cache:
            attachment_cache[media_id] = client.get_media(media_id)
        return attachment_cache[media_id]

    def _rejection_reason(item: dict[str, Any]) -> str | None:
        return _media_item_rejection(item, entities, client, attachment_cache)

    if config.dry_run:
        results: list[dict[str, Any]] = []
        seen_sources: set[str] = set()
        for index, item in enumerate(plan):
            reason = _rejection_reason(item)
            if reason is None:
                reason = _duplicate_source_reason(plan, index, seen_sources)
            results.append(
                {
                    "paragraph_index": item.get("paragraph_index"),
                    "status": "rejected" if reason else "blocked",
                    "detail": reason or "dry-run nao executa download/upload de midia",
                }
            )
        return results, None, None
    outcomes: dict[int, dict[str, Any]] = {}
    # Compartilhado entre threads: a verificacao de origem faz read-modify-write
    # ("se ausente, busca e grava"); sob GIL o pior caso e uma busca duplicada
    # da mesma pagina (idempotente), sem corromper o cache.
    page_cache: dict[str, list[str] | None] = {}
    seen_sources: set[str] = set()

    # Pre-passe SERIAL: rejeicao (relevancia/reuso) + deteccao de duplicatas.
    # Duplicata depende da ORDEM (primeira ocorrencia vence) e o
    # attachment_cache e populado aqui (get_media), por isso fica fora do
    # paralelismo.
    pending: list[tuple[int, dict[str, Any]]] = []
    for position, item in enumerate(plan):
        reason = _rejection_reason(item)
        if reason is None:
            reason = _duplicate_source_reason(plan, position, seen_sources)
        if reason:
            outcomes[position] = {
                "paragraph_index": item.get("paragraph_index"),
                "status": "rejected",
                "detail": reason,
            }
            continue
        pending.append((position, item))

    if pending:
        with tempfile.TemporaryDirectory(prefix="unicornio-media-") as directory:
            tmp = Path(directory)

            def _process_item(pair: tuple[int, dict[str, Any]]) -> tuple[int, dict[str, Any]]:
                position, item = pair
                evidence = {
                    name: item[name]
                    for name in (
                        "source_page_url",
                        "direct_image_url",
                        "author",
                        "license",
                        "license_url",
                        "captured_at",
                        "credit_text",
                        "alt_text",
                    )
                }
                suffix = Path(item["direct_image_url"].split("?", 1)[0]).suffix or ".jpg"
                attachment = _attachment_evidence(item)
                download_url = (
                    attachment.get("source_url") if attachment is not None else item["direct_image_url"]
                )
                source = download_image(
                    str(download_url),
                    tmp / f"source_{position}{suffix}",
                    max_attempts=config.max_source_retries + 1,
                )
                # Verificacao de conteudo: a imagem baixada deve estar listada
                # na pagina de origem (fail-closed).
                ok, verify_reason = verify_downloaded_against_source(
                    source_page_url=str(item.get("source_page_url") or ""),
                    downloaded=source,
                    direct_image_url=str(download_url),
                    cache=page_cache,
                )
                if not ok:
                    return position, {
                        "paragraph_index": item.get("paragraph_index"),
                        "status": "rejected",
                        "detail": f"verificacao de origem: {verify_reason}",
                    }
                is_featured = bool(item.get("is_featured"))
                transparency = "flattened" if image_has_transparency(source) else "none"
                if is_featured:
                    webp = prepare_featured_webp(source, tmp / f"featured_{position}.webp")
                else:
                    webp = convert_to_webp(source, tmp / f"inline_{position}.webp")
                # Featured "so texto" nao e key art: rejeita.
                if is_featured and image_is_mostly_flat(webp):
                    return position, {
                        "paragraph_index": item.get("paragraph_index"),
                        "status": "rejected",
                        "detail": "featured aparenta ser so texto/arte plana sem conteudo visual; "
                        "escolha uma key art/imagem real da obra",
                    }
                width, height = image_dimensions(webp)
                media = upload_image(client, webp, evidence)
                media_id = media.get("id")
                media_url = media.get("source_url")
                if not media_id or not media_url:
                    raise WorkflowError(f"media upload returned no id/source_url (item {position})")
                return position, {
                    "paragraph_index": item["paragraph_index"],
                    "media_id": media_id,
                    "media_url": media_url,
                    "alt_text": item["alt_text"],
                    "credit_text": item["credit_text"],
                    "featured": is_featured,
                    "width": width,
                    "height": height,
                    "transparency": transparency,
                }

            # Fase paralela (I/O-bound). Somente falha ao CRIAR o executor cai
            # para serial: nessa altura ainda não há download nem upload. Uma
            # falha dentro de worker vira rejeição daquele item; reexecutar o
            # lote inteiro duplicaria anexos que já foram enviados.
            try:
                pool = ThreadPoolExecutor(max_workers=_MEDIA_WORKERS)
            except Exception:
                processed = [_process_item(pair) for pair in pending]
            else:
                with pool:
                    futures = [(pair, pool.submit(_process_item, pair)) for pair in pending]
                    processed = []
                    for pair, future in futures:
                        position, item = pair
                        try:
                            processed.append(future.result())
                        except Exception as exc:  # noqa: BLE001 - report one failed item
                            processed.append(
                                (
                                    position,
                                    {
                                        "paragraph_index": item.get("paragraph_index"),
                                        "status": "error",
                                        "detail": f"processamento de midia: {str(exc)[:160]}",
                                    },
                                )
                            )

            for position, result in processed:
                outcomes[position] = result

    # Reconstrói results na ordem do plano (paragrafos), rejeitados e
    # processados intercalados como antes.
    results = [outcomes[position] for position in sorted(outcomes)]
    featured_id: int | None = None
    featured_credit: str | None = None
    for result in results:
        if result.get("featured"):
            featured_id = result.get("media_id")
            featured_credit = result.get("credit_text")
    return results, featured_id, featured_credit



def _normalize_existing_featured(
    client: WordPressClient,
    config: Config,
    post: dict[str, Any],
    editorial: dict[str, Any] | None = None,
) -> int | None:
    """Re-prepare an existing featured image at exactly 1280x720 WebP.

    Posts imported with a featured image may carry any size/format; the
    portal rule requires 1280x720 WebP, so the source is re-downloaded and
    re-uploaded through the same conversion path when it does not comply.
    The new attachment KEEPS the original provenance: its file name is
    derived from the source file name and the title/alt are copied as-is
    (fixed bug: the title was serialized as ``str(dict)``), so the
    deterministic featured-relevance gate can still match the work from
    real evidence instead of a generic ``featured-1280x720.webp`` name.

    When ``editorial`` is provided, the existing featured is ONLY reused
    when its real evidence (url/title/alt) references a cited work of the
    post — a generic article header/wordmark (e.g. a "5 classic animes..."
    banner image) is NOT the subject and is not reused, leaving the post
    without a featured so the editorial flow must supply a real key art.

    Returns the (new) attachment id, or None when there is nothing to do.
    """
    featured = post.get("featured_media")
    if not isinstance(featured, int) or featured <= 0:
        return None
    try:
        media = client.get_media(featured)
    except Exception:
        return None
    details = media.get("media_details") or {}
    width, height = details.get("width"), details.get("height")
    source_url = (media.get("source_url") or "").strip()
    if editorial is not None:
        entities = extract_entities(
            title=str((editorial.get("seo") or {}).get("title") or ""),
            content_html=str(editorial.get("cleaned_html") or ""),
            focus_keyword=str((editorial.get("seo") or {}).get("focus_keyword") or ""),
            game_name=editorial.get("game_name"),
        )
        if entities:
            evidence = " ".join(
                part
                for part in (
                    source_url,
                    str((media.get("title") or {}).get("rendered") or ""),
                    str(media.get("alt_text") or ""),
                )
                if part
            )
            if not image_is_relevant(
                alt_text="",
                credit_text="",
                source_url=evidence,
                entities=entities,
                source_only=True,
            ):
                return None
    if width == 1280 and height == 720 and source_url.lower().endswith(".webp"):
        return featured
    if not source_url:
        return None
    from .media.text import plain_text

    title = plain_text(
        str((media.get("title") or {}).get("rendered") or "")
    ) or "Imagem de destaque"
    alt = plain_text(str(media.get("alt_text") or ""))
    caption = plain_text(str((media.get("caption") or {}).get("rendered") or ""))
    filename = _featured_filename_from_source(source_url)
    try:
        with tempfile.TemporaryDirectory(prefix="unicornio-featured-") as directory:
            tmp = Path(directory)
            source = download_image(
                source_url,
                tmp / "featured_source.jpg",
                max_attempts=config.max_source_retries + 1,
            )
            webp = prepare_featured_webp(source, tmp / "featured_1280x720.webp")
            new_media = client.upload_media(
                str(webp),
                filename=filename,
                alt_text=alt,
                title=title,
                caption=caption,
            )
    except Exception:
        return None
    new_id = new_media.get("id")
    return new_id if isinstance(new_id, int) else None


def _featured_filename_from_source(source_url: str) -> str:
    """Derive a provenance-carrying filename from the original source URL.

    The re-uploaded featured image must keep the source's evidence in its
    own file name (e.g. ``remothered-red-nuns-legacy-...-1280x720.webp``)
    so the deterministic relevance gate can still match the work. Falls
    back to the generic ``featured-1280x720.webp`` when the source name is
    not usable (non-ascii-only or too short).
    """
    stem = Path(source_url.split("?", 1)[0]).stem or ""
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    if len(slug) < 5:
        return "featured-1280x720.webp"
    return f"{slug[:80].strip('-')}-1280x720.webp"


def resolve_editorial_defaults(editorial: dict[str, Any], post: dict[str, Any]) -> dict[str, Any]:
    """Fill optional editorial fields (seo, cleaned_html) from the post.

    Token-economy defaults: the model must not re-emit content/SEO the post
    already has. ``seo`` is inherited from a valid existing Rank Math meta,
    or derived deterministically from the post when no valid meta exists;
    ``cleaned_html`` reuses the deterministic cleaned content (no-rewrite).
    Raises EditorialValidationError only when even the deterministic SEO
    derivation fails — the model must provide seo in that rare case.
    """
    resolved = dict(editorial)
    if resolved.get("seo") is None:
        resolved["seo"] = _resolve_seo_from_post(
            post, game_name=editorial.get("game_name")
        )
    if resolved.get("cleaned_html") is None:
        resolved["cleaned_html"] = clean_html(_repair_orphan_media(_raw_content(post)))
    return resolved


def _seo_description(text: str, limit: int = 155) -> str:
    """First sentence of the text, trimmed to ~``limit`` chars at a word boundary."""
    clean = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()
    if not clean:
        return "Notícia do UnicornioHater."
    for sep in (". ", "! ", "? ", "\n"):
        head = clean.split(sep, 1)[0]
        if head and len(head) >= 120:
            clean = head
            break
    if len(clean) <= limit:
        return clean
    cut = clean[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(".,;:") + "..."


def _seo_keyword_candidates(title: str, game_name: str | None) -> list[str]:
    """Deterministic focus-keyword candidates, most specific first."""
    from .content_quality import _keyword_in_text

    candidates: list[str] = []
    if game_name and game_name.strip():
        candidates.append(game_name.strip())
    title = (title or "").strip()
    if title:
        candidates.append(title)
        words = re.findall(r"[\wÀ-ÿ]+", title)
        if len(words) >= 3:
            candidates.append(" ".join(words[:3]))
            candidates.append(" ".join(words[-3:]))
    return candidates


def _resolve_seo_from_post(
    post: dict[str, Any], *, game_name: str | None = None
) -> dict[str, Any]:
    from .content_quality import _keyword_in_text
    from .editorial_schema import EditorialValidationError

    meta = post.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}
    title = meta.get("rank_math_title")
    description = meta.get("rank_math_description")
    keyword = meta.get("rank_math_focus_keyword")
    if (
        isinstance(title, str)
        and title.strip()
        and 0 < len(title.strip()) <= 65
        and isinstance(description, str)
        and 120 <= len(description.strip()) <= 160
        and isinstance(keyword, str)
        and keyword.strip()
    ):
        return {
            "title": title.strip(),
            "meta_description": description.strip(),
            "focus_keyword": keyword.strip(),
        }
    # No valid Rank Math meta: derive SEO deterministically (token economy —
    # the model must not generate what the code can). The keyword must occur
    # naturally in BOTH the title and the body (the quality gate enforces it).
    post_title = _post_title(post) or ""
    body = clean_html(_raw_content(post))
    body_text = re.sub(r"<[^>]+>", " ", body)
    derived_title = post_title.strip()[:65] or "Notícia"
    derived_description = _seo_description(body_text)
    for candidate in _seo_keyword_candidates(post_title, game_name):
        if _keyword_in_text(candidate, post_title) and _keyword_in_text(candidate, body_text):
            return {
                "title": derived_title,
                "meta_description": derived_description,
                "focus_keyword": candidate,
            }
    raise EditorialValidationError(
        "seo ausente no JSON e nao foi possivel deriva-lo deterministicamente "
        "(nenhuma frase do titulo ocorre no corpo) — o modelo deve fornecer seo "
        "(title <= 65, meta_description 120-160, focus_keyword)"
    )


def _save_uncertain(root: Path, post_id: int, editorial: dict[str, Any]) -> None:
    """Record a non-final skip: the post stays pending, out of the queue."""
    try:
        directory = root / "backups" / str(post_id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "uncertain.json").write_text(
            json.dumps(
                {
                    "post_id": post_id,
                    "status": "uncertain",
                    "site_relevance": editorial.get("site_relevance"),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def _save_editorial_latest(root: Path, post_id: int, editorial: dict[str, Any]) -> None:
    """Persist the validated editorial so the publish flow can re-check it."""
    try:
        directory = root / "backups" / str(post_id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "editorial.latest.json").write_text(
            json.dumps(editorial, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def _save_blocked(root: Path, post_id: int, editorial: dict[str, Any], checklist: dict[str, Any]) -> None:
    """Archive an editorial the apply refused to write (checklist failed).

    Keeps ``editorial.blocked.json`` as the audit trail. ``editorial.latest.json``
    is NOT removed: the post keeps its publish candidacy (its WordPress content
    may already carry good images from a previous successful apply — removing
    the latest would orphan the post and the publish gate would never try it).
    The agent sees the blocked marker in the cards, fixes the failing items and
    re-applies; the next publish window decides with the real content.
    """
    try:
        directory = root / "backups" / str(post_id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "editorial.blocked.json").write_text(
            json.dumps(
                {**editorial, "blocked_checklist": checklist},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def _record_blocked(root: Path, post_id: int, checklist: dict[str, Any]) -> None:
    """The publish gate blocked a post: record the failure in
    ``editorial.blocked.json`` WITHOUT removing ``editorial.latest.json``.

    The post stays a publish candidate for the next windows (its content may
    already be good on WordPress — removing the latest would orphan it and the
    publish gate would never try it again). The agent sees the blocked marker
    in the cards, fixes the failing items (re-apply), and the next window
    publishes once the checklist passes.
    """
    try:
        directory = root / "backups" / str(post_id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "editorial.blocked.json").write_text(
            json.dumps(
                {
                    "post_id": post_id,
                    "status": "blocked",
                    "reopened_at": datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(timespec="seconds"),
                    "blocked_checklist": checklist,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def publish_post(
    client: WordPressClient,
    config: Config,
    root: Path,
    post_id: int,
) -> dict[str, Any]:
    """Publish one post while excluding a concurrent editorial mutation."""
    with _acquire_post_lock(root, config, post_id):
        return _publish_post_unlocked(client, config, root, post_id)


def _publish_post_unlocked(
    client: WordPressClient,
    config: Config,
    root: Path,
    post_id: int,
) -> dict[str, Any]:
    """Publish de UM post, somente a partir do estado READY.

    Caminho barato (determinístico): post READY cujo Ready Manifest (hash
    SHA-256) ainda bate com o WordPress agora -> conteúdo idêntico ao do
    preflight -> publica SEM re-executar o checklist caro (nada mudou).

    Caminho de revalidação: STALE (hash mudou) ou legado (sem estado) ->
    checklist completo -> publica se 100%, senão BLOCKED (fora da fila até o
    agente re-aplicar). Estados blocked/awaiting_human/uncertain/skipped
    nunca são tocados aqui — pertencem à fila de rework/do agente.
    """
    post = client.get_post(post_id)
    if post.get("status") != "pending":
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "status": "skipped",
            "reason": f"post status is {post.get('status')}, expected pending",
        }
    state_info = read_state(post)
    state = state_info["state"]
    if state not in (None, STATE_READY):
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "status": "skipped",
            "reason": f"estado {state} (fora da fila de publicacao; rework/agente)",
            "state": state,
        }
    if state == STATE_READY:
        raw_meta = post.get("meta")
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        stored = parse_manifest(meta.get(META_READY_MANIFEST))
        if manifest_matches(post, stored, state_info["ready_hash"], policy_version=config.policy_version):
            return _publish_now(client, config, post_id, integrity="manifest_match")
        # STALE: algo mudou desde o preflight -> revalida com o checklist.
    editorial_path = root / "backups" / str(post_id) / "editorial.latest.json"
    if not editorial_path.is_file():
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "status": "skipped",
            "reason": "sem editorial.latest.json (post ainda nao passou pelo pipeline)",
            "state": state,
        }
    try:
        editorial = validate_editorial(
            json.loads(editorial_path.read_text(encoding="utf-8")),
            min_confidence=config.min_relevance_confidence,
        )
    except (ValueError, OSError) as exc:
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "status": "skipped",
            "reason": f"editorial.latest.json invalido: {exc}",
            "state": state,
        }
    if editorial["site_relevance"]["decision"] != "process":
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "status": "skipped",
            "reason": editorial["site_relevance"]["reason"],
            "state": state,
        }
    backup = SnapshotStore(root).save(post_id, post)
    checklist = run_pre_publish_checklist(
        post=post,
        editorial=editorial,
        content=_raw_content(post),
        backup_path=backup,
        config=config,
        client=client,
    )
    if checklist["failed"]:
        # Registra o bloqueio SEM remover editorial.latest.json: o post
        # continua candidato nas proximas janelas (o conteudo no WP pode ja
        # estar bom — remover o latest orfana o post e o publish nunca mais o
        # tenta). O agente ve o editorial.blocked.json nos cards, corrige
        # (re-apply) e a proxima janela publica.
        _record_blocked(root, post_id, checklist)
        _write_state_markers(
            client,
            config,
            post_id,
            STATE_BLOCKED,
            last_error="checklist pre-publicacao com falhas",
        )
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "status": "blocked",
            "reason": "checklist pre-publicacao com falhas (STALE/legado revalidado)",
            "checklist": checklist,
            "reopened_for_rework": True,
            "state": STATE_BLOCKED,
        }
    if not config.publish_enabled:
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "status": "blocked",
            "reason": "PUBLISH_ENABLED=false (gate de publicacao desligado)",
            "checklist": checklist,
            "state": state,
        }
    return _publish_now(client, config, post_id, integrity="revalidated")


def _publish_now(
    client: WordPressClient,
    config: Config,
    post_id: int,
    *,
    integrity: str,
) -> dict[str, Any]:
    """Publica de fato e marca PUBLISHED (gate PUBLISH_ENABLED já verificado)."""
    if not config.publish_enabled:
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "status": "blocked",
            "reason": "PUBLISH_ENABLED=false (gate de publicacao desligado)",
        }
    published_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    result = client.publish(
        post_id,
        meta={
            "_ai_editor_published_at": published_at,
            **build_state_markers(STATE_PUBLISHED, policy_version=config.policy_version),
        },
        # Política do dono: post antigo em pending publica como data corrente
        # (não fica enterrado no passado do site).
        date_gmt=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
    )
    return {
        "post_id": post_id,
        "wordpress_changed": True,
        "status": "published",
        "status_after": result.get("status"),
        "link": result.get("link"),
        "published_at": published_at,
        "integrity": integrity,
        "state": STATE_PUBLISHED,
    }


def publish_ready_posts(
    client: WordPressClient,
    config: Config,
    root: Path,
    limit: int = 0,
) -> list[dict[str, Any]]:
    """Publish dos pending prontos, até a cota da janela (PUBLISH_LIMIT).

    Fase 10: percorre apenas trabalho elegível — posts READY (caminho barato
    via manifest) e legado sem estado (revalidação). Posts blocked/
    awaiting_human/uncertain/skipped são ignorados sem custo (sem checklist).
    ``limit`` conta somente publicados; ``limit=0`` = sem cota.
    """
    outcomes: list[dict[str, Any]] = []
    # Paginacao completa: a fila pode ter mais que 100 pending, e um post READY
    # alem da pagina 1 nao pode ficar invisivel na janela de publicacao.
    posts: list[dict[str, Any]] = []
    page = 1
    while True:
        chunk = client.list_pending(page=page, per_page=100)
        posts.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
        if page > 100:
            break  # limite de seguranca (paginas sao baratas, mas nao infinitas)
    for candidate in posts:
        candidate_id = candidate.get("id")
        if not isinstance(candidate_id, int):
            continue
        state_info = read_state(candidate)
        if state_info["state"] not in (None, STATE_READY):
            continue  # fora da fila de publicacao — sem chamadas caras
        if state_info["state"] is None and (
            root / "backups" / str(candidate_id) / "editorial.blocked.json"
        ).is_file():
            # Legado ja sinalizado como rework: nao revalida a cada janela —
            # o agente corrige (re-apply) e o estado vira READY.
            continue
        published = sum(1 for outcome in outcomes if outcome.get("wordpress_changed"))
        if limit and published >= limit:
            break
        try:
            outcome = publish_post(client, config, root, candidate_id)
        except Exception as exc:  # noqa: BLE001 - report per post, keep the loop alive
            outcome = {
                "post_id": candidate_id,
                "wordpress_changed": False,
                "status": "error",
                "reason": str(exc),
            }
        outcomes.append(outcome)
    return outcomes


def _discover_trailer(editorial: dict[str, Any], config: Config) -> dict[str, str] | None:
    """Discover a YouTube trailer for game content; fail-closed to None."""
    game_name = editorial.get("game_name")
    if not isinstance(game_name, str) or not game_name.strip():
        return None
    try:
        return find_game_trailer(game_name, timeout=config.http_timeout)
    except TrailerError:
        return None


def compose_final_content(
    editorial: dict[str, Any],
    config: Config,
    original_link: str | None,
) -> tuple[str, dict[str, str] | None]:
    """Build the final content: cleaned HTML + optional trailer embed + canonical footer.

    Returns ``(content, trailer)`` so callers can report what was embedded.

    Internal category links are added deterministically (no LLM) to the body
    HTML BEFORE the trailer/footer are appended, so the CTA/Fonte blocks and
    the trailer embed are never linked. The enrichment runs on the same final
    content the checklist validates and the manifest hashes.
    """
    html = editorial["cleaned_html"]
    if config.internal_links_enabled:
        from .internal_links import add_internal_links

        html = add_internal_links(html)
    # Remove figuras orfas de credito duplicado (figcaption repetido sem <img>).
    from .media.text import dedupe_credit_figures

    html = dedupe_credit_figures(html)
    trailer = _discover_trailer(editorial, config)
    if trailer is not None:
        html = html.rstrip() + "\n\n" + build_trailer_html(trailer)
    return append_canonical_footer(html, original_link), trailer


def original_link_of(post: dict[str, Any]) -> str | None:
    """Read the ``original_link`` custom field from the post meta (REST edit context)."""
    return _original_link(post)


def _require_pending(post: dict[str, Any]) -> None:
    if post.get("status") != "pending":
        raise WorkflowError("post is no longer pending; refusing to process")


def _raw_content(post: dict[str, Any]) -> str:
    content = post.get("content")
    if not isinstance(content, dict) or not isinstance(content.get("raw"), str):
        raise WorkflowError("post content.raw is missing")
    return content["raw"]


def _post_title(post: dict[str, Any]) -> str | None:
    title = post.get("title")
    if isinstance(title, dict) and isinstance(title.get("raw"), str):
        return title["raw"].strip() or None
    if isinstance(title, str):
        return title.strip() or None
    return None


def build_queue_report(
    client: WordPressClient,
    root: Path,
    *,
    per_page: int = 50,
    recent_days: int = 7,
) -> dict[str, Any]:
    """Estado determinístico da fila, orientado pela meta ``_hermes_state``.

    Read-only. A meta do WordPress é a fonte de verdade; marcadores de
    filesystem (editorial.latest.json / editorial.blocked.json / uncertain.json)
    são o fallback para posts legado (sem estado). ``edited`` significa
    estado READY (apto à publicação — o publish-ready confirma o hash).
    ``blocked`` = rework (apply recusou ou publish reabriu); o monitor só
    considera elegíveis os BLOCKED cujo ``next_retry_at`` venceu (cooldown
    respeitado — um post não reaparece na agenda enquanto estiver em
    cooldown). ``awaiting_human``/``uncertain``/``skipped`` saem da fila.
    ``unprocessed_ids`` + ``eligible_rework_ids`` formam a linha
    estável que o monitor hasheia (token economy: sem LLM no idle).
    """
    from .content_quality import word_count

    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=recent_days)
    # A fila editorial não pode parar na primeira página: um backlog com mais
    # de ``per_page`` pending deixava posts invisíveis ao monitor para sempre.
    # O teto protege contra paginação defeituosa no WordPress.
    def _all_with_status(status: str) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        for page in range(1, 101):
            try:
                chunk = client.list_pending(page=page, per_page=per_page, status=status)
            except TypeError:
                # Clientes legados/test doubles não aceitavam ``status``;
                # para pending, preservamos a API antiga. Outros status não
                # podem ser consultados com segurança nesse cliente.
                if status != "pending":
                    return collected
                chunk = client.list_pending(page=page, per_page=per_page)
            collected.extend(chunk)
            if len(chunk) < per_page:
                break
        return collected

    posts = _all_with_status("pending")
    # Posts movidos para o status WP "awaiting_human" (decisão humana): saem
    # de pending e deixariam de aparecer no relatório. O relatório continua
    # listando-os como awaiting_human (o monitor/publish nunca os tocam — só
    # o retry humano os devolve ao fluxo). Client sem o param (testes antigos)
    # apenas não busca o status extra.
    try:
        awaiting_wp = _all_with_status("awaiting_human")
    except TypeError:
        awaiting_wp = []
    _pending_ids = {p.get("id") for p in posts if isinstance(p.get("id"), int)}
    for p in awaiting_wp:
        if isinstance(p.get("id"), int) and p["id"] not in _pending_ids:
            p["_wp_awaiting_human"] = True
            posts.append(p)
    rows: list[dict[str, Any]] = []
    unprocessed: list[int] = []
    recent_unprocessed: list[int] = []
    blocked_ids: list[int] = []
    recent_blocked: list[int] = []
    eligible_rework: list[int] = []
    ready_ids: list[int] = []
    awaiting_human_ids: list[int] = []
    uncertain_ids: list[int] = []
    skipped_ids: list[int] = []
    for post in posts:
        post_id = post.get("id")
        if not isinstance(post_id, int):
            continue
        backups_dir = root / "backups" / str(post_id)
        state_info = read_state(post)
        state = state_info["state"]
        latest_file = (backups_dir / "editorial.latest.json").is_file()
        blocked_file = (backups_dir / "editorial.blocked.json").is_file()
        uncertain_file = (backups_dir / "uncertain.json").is_file()
        if state is None:
            # Legado (sem meta de estado): marcadores de filesystem decidem.
            if uncertain_file:
                state = STATE_UNCERTAIN
            elif blocked_file:
                state = STATE_BLOCKED
            elif latest_file:
                state = STATE_READY  # legado: latest sem bloqueio == pronto
            else:
                state = STATE_NEW
        # Elegibilidade usa o estado RESOLVIDO (legado incluido): blocked sem
        # next_retry_at (ou com ele vencido) volta a agenda do monitor.
        effective_state = {**state_info, "state": state}
        if state == STATE_UNCERTAIN:
            uncertain_ids.append(post_id)
            # Política do dono: uncertain volta ao trabalho quando o cooldown
            # expira (ou imediatamente se nunca teve cooldown — legado). Antes
            # ficava preso para sempre: posts pending antigos nunca eram
            # trilhados. O cooldown evita loop infinito de re-trilhagem.
            if cooldown_expired(state_info.get("next_retry_at") or ""):
                eligible_rework.append(post_id)
        elif state == STATE_AWAITING_HUMAN or post.get("_wp_awaiting_human"):
            awaiting_human_ids.append(post_id)
        elif state == STATE_SKIPPED:
            skipped_ids.append(post_id)
        elif state == STATE_READY:
            ready_ids.append(post_id)
        elif state == STATE_BLOCKED:
            blocked_ids.append(post_id)
            if retry_eligible(effective_state):
                eligible_rework.append(post_id)
            if _is_recent(post, cutoff):
                recent_blocked.append(post_id)
        else:  # NEW / PROCESSING / desconhecido
            unprocessed.append(post_id)
            if _is_recent(post, cutoff):
                recent_unprocessed.append(post_id)
        title = (post.get("title") or {}).get("raw") or (post.get("title") or {}).get("rendered")
        rows.append(
            {
                "id": post_id,
                "date": post.get("date"),
                "date_gmt": post.get("date_gmt"),
                "word_count": word_count((post.get("content") or {}).get("rendered") or ""),
                "state": state,
                "attempts": state_info["attempts"],
                "next_retry_at": state_info["next_retry_at"],
                "last_error": state_info["last_error"][:160],
                "prepared": (backups_dir / "prepared.json").is_file(),
                "edited": state == STATE_READY,
                "blocked": state == STATE_BLOCKED,
                "uncertain": state == STATE_UNCERTAIN,
                "awaiting_human": state == STATE_AWAITING_HUMAN or post.get("_wp_awaiting_human"),
                "skipped": state == STATE_SKIPPED,
                "title": title,
            }
        )
    rows.sort(key=lambda row: int(row["id"] or 0))
    unprocessed.sort()
    recent_unprocessed.sort()
    blocked_ids.sort()
    recent_blocked.sort()
    eligible_rework.sort()
    ready_ids.sort()
    awaiting_human_ids.sort()
    uncertain_ids.sort()
    skipped_ids.sort()
    return {
        "pending": len(rows),
        "edited": len(ready_ids),
        "blocked": len(blocked_ids),
        "uncertain": len(uncertain_ids),
        "awaiting_human": len(awaiting_human_ids),
        "skipped": len(skipped_ids),
        "unprocessed_ids": unprocessed,
        "recent_unprocessed_ids": recent_unprocessed,
        "blocked_ids": blocked_ids,
        "recent_blocked_ids": recent_blocked,
        "eligible_rework_ids": eligible_rework,
        "ready_ids": ready_ids,
        "awaiting_human_ids": awaiting_human_ids,
        "uncertain_ids": uncertain_ids,
        "skipped_ids": skipped_ids,
        "recent_days": recent_days,
        "posts": rows,
    }


_GAME_HINT_WORDS = frozenset(
    {
        "jogo", "jogos", "game", "games", "gameplay", "demo", "remake",
        "remaster", "dlc", "expansao", "expansão", "expansion", "console",
        "playstation", "ps5", "ps4", "ps3", "xbox", "switch", "nintendo",
        "steam", "gaming", "gameboy", "game boy", "emulador", "emuladores",
        "plataforma", "plataformas", "videogame", "gamepass", "game pass",
    }
)


def _game_hint(title: str) -> bool:
    """Cheap deterministic hint that the post is about a game (LLM confirms)."""
    from .media.relevance import normalize

    tokens = set(re.findall(r"[a-z0-9]+", normalize(title or "")))
    return bool(tokens & _GAME_HINT_WORDS)


def _rework_ids(root: Path) -> list[int]:
    """IDs com ``editorial.blocked.json`` (rework), sem os uncertain.

    Leitura direta do filesystem (token economy + CPU): nao busca 100 posts
    no WordPress so para descobrir quais estao bloqueados. ``uncertain.json``
    vence (o agente ja decidiu que nao ha como processar).
    """
    backups = root / "backups"
    if not backups.is_dir():
        return []
    ids: list[int] = []
    for entry in backups.iterdir():
        if not entry.is_dir():
            continue
        if (entry / "editorial.blocked.json").is_file() and not (
            entry / "uncertain.json"
        ).is_file():
            try:
                ids.append(int(entry.name))
            except ValueError:
                continue
    return sorted(ids)


def build_cards(
    client: WordPressClient,
    config: Config,
    root: Path,
    *,
    per_page: int | None = None,
) -> dict[str, Any]:
    """Cartões compactos por post para o agente (economia de tokens: UMA chamada).

    Cada card carrega o DELTA exato (Fase 4): quantas imagens são exigidas,
    quantas válidas existem, quantas faltam, quantas são irrelevantes/não-WebP,
    diagnóstico da featured (existe? relevante? WebP? dimensões? ação) e, para
    posts bloqueados, o plano ``fix`` — o agente sabe o que corrigir SÓ pelo
    card, sem abrir blocked.json/checklist/logs/source. Rework vem PRIMEIRO
    (FIFO por id); posts fora da fila (uncertain/awaiting_human/skipped/ready)
    não geram card.
    """
    from .content_quality import word_count
    from .html_cleaner import clean_html
    from .media.relevance import extract_entities

    per_page = per_page or config.max_posts_per_run
    rework_ids = _rework_ids(root)
    posts: list[dict[str, Any]] = []
    if rework_ids:
        posts.extend(
            client.list_pending(include=rework_ids[:per_page], per_page=len(rework_ids[:per_page]))
        )
    remaining = per_page - len(posts)
    if remaining > 0:
        # Fetch amplo e filtra: muitos pending podem ser ready/out-of-queue.
        posts.extend(client.list_pending(per_page=max(remaining * 5, 20)))
    seen: set[int] = set()
    ordered: list[dict[str, Any]] = []
    for post in posts:
        post_id = post.get("id")
        if not isinstance(post_id, int) or post_id in seen:
            continue
        seen.add(post_id)
        ordered.append(post)
    cards: list[dict[str, Any]] = []
    for post in ordered:
        post_id = post.get("id")
        if not isinstance(post_id, int):
            continue
        backups_dir = root / "backups" / str(post_id)
        state_info = read_state(post)
        state = state_info["state"]
        blocked_file = (backups_dir / "editorial.blocked.json").is_file()
        uncertain_file = (backups_dir / "uncertain.json").is_file()
        latest_file = (backups_dir / "editorial.latest.json").is_file()
        if state is None:
            # Legado: marcadores de filesystem decidem.
            if uncertain_file:
                state = STATE_UNCERTAIN
            elif blocked_file:
                state = STATE_BLOCKED
            elif latest_file:
                state = STATE_READY
            else:
                state = STATE_NEW
        if state == STATE_UNCERTAIN:
            # Política do dono (ba91a43): uncertain volta ao trabalho quando o
            # cooldown expira (legado sem cooldown = elegível imediatamente).
            # Espelha o eligible_rework do build_queue_report — senão o monitor
            # acorda o agente para posts que o cards nunca mostra (loop de
            # tokens). Em cooldown, continua fora da fila.
            if not cooldown_expired(state_info.get("next_retry_at") or ""):
                continue
        elif state in (STATE_AWAITING_HUMAN, STATE_SKIPPED, STATE_READY):
            continue  # fora da fila de trabalho do agente
        blocked = state == STATE_BLOCKED
        title = (post.get("title") or {}).get("raw") or (post.get("title") or {}).get("rendered") or ""
        raw = (post.get("content") or {}).get("raw") or ""
        rendered = (post.get("content") or {}).get("rendered") or ""
        meta = post.get("meta") or {}
        if not isinstance(meta, dict):
            meta = {}
        entities = extract_entities(title=title, content_html=raw)
        # Delta sobre o conteudo LIMPO (o que o apply realmente produz): o
        # clean_html preserva figuras com credito completo — sem isso o card
        # contaria imagens que o apply descartaria (loop de rework invisivel).
        cleaned = clean_html(raw)
        images = _images_summary(cleaned, title, entities)
        featured = _featured_diagnosis(client, post, entities)
        fix = _fix_plan(backups_dir, images, featured, blocked) if blocked else None
        cards.append(
            {
                "id": post_id,
                "date": post.get("date"),
                "title": title,
                "word_count": word_count(rendered or raw),
                "entities": sorted(entities),
                "original_link": meta.get("original_link"),
                "seo_exists": _seo_is_valid(meta),
                "images": images,
                "featured": featured,
                "game_hint": _game_hint(title),
                "state": state,
                "attempts": state_info["attempts"],
                "next_retry_at": state_info["next_retry_at"],
                "last_error": state_info["last_error"][:160],
                "blocked": blocked,
                "blocked_reason": _blocked_reason(backups_dir) if blocked else None,
                "fix": fix,
                "draft": (
                    str(backups_dir / "editorial.draft.json")
                    if (backups_dir / "editorial.draft.json").is_file()
                    else None
                ),
                "prepared": (backups_dir / "prepared.json").is_file(),
            }
        )
    # Rework first, FIFO por id (os mais antigos primeiro): posts reabertos
    # pelo publish gate sao corrigidos antes de posts novos — e o lote
    # rotaciona, em vez de os mesmos 10 blocked monopolizarem o topo.
    cards.sort(key=lambda card: (not card.get("blocked", False), int(card.get("id") or 0)))
    return {"count": len(cards[:per_page]), "cards": cards[:per_page]}


def _featured_diagnosis(
    client: WordPressClient,
    post: dict[str, Any],
    entities: set[str],
) -> dict[str, Any]:
    """Diagnóstico determinístico da featured atual (Fase 4.2).

    ``exists``/``relevant``/``webp``/``dimensions``/``valid`` + ``action``:
    - ``normalize`` — semanticamente correta mas formato/dimensão errados:
      o apply normaliza automaticamente (o agente não busca nada).
    - ``replace``   — irrelevante (não retrata o assunto): o agente deve
      buscar key art nova.
    - ``provide``   — não existe: o agente deve incluir ``is_featured`` no
      media_plan.
    - ``ok``        — já válida; nada a fazer.
    """
    featured_raw = post.get("featured_media")
    if not isinstance(featured_raw, int) or featured_raw <= 0:
        return {
            "exists": False,
            "relevant": None,
            "webp": None,
            "dimensions": None,
            "valid": False,
            "action": "provide",
        }
    featured_id = featured_raw
    webp: bool | None = None
    dimensions: str | None = None
    relevant: bool | None = None
    try:
        media = client.get_media(featured_id)
        details = media.get("media_details") or {}
        width, height = details.get("width"), details.get("height")
        dimensions = f"{width or '?'}x{height or '?'}"
        source_url = (media.get("source_url") or "").strip()
        webp = source_url.lower().split("?", 1)[0].endswith(".webp")
        if entities:
            evidence = " ".join(
                part
                for part in (
                    source_url,
                    str((media.get("title") or {}).get("rendered") or ""),
                    str(media.get("alt_text") or ""),
                )
                if part
            )
            relevant = image_is_relevant(
                alt_text="", credit_text="", source_url=evidence, entities=entities, source_only=True
            )
    except Exception:  # noqa: BLE001 - media lookup failure: diagnostico conservador
        pass
    valid = bool(relevant and webp and dimensions == "1280x720")
    if valid:
        action = "ok"
    elif relevant is False:
        action = "replace"
    else:
        action = "normalize"
    return {
        "exists": True,
        "relevant": relevant,
        "webp": webp,
        "dimensions": dimensions,
        "valid": valid,
        "action": action,
    }


def _fix_plan(
    backups_dir: Path,
    images: dict[str, int],
    featured: dict[str, Any],
    blocked: bool,
) -> dict[str, Any] | None:
    """Plano de correção derivado do checklist bloqueado (Fase 4.3).

    O agente lê APENAS o card e sabe: quantas imagens buscar, se a featured
    será normalizada pelo código (nada a fazer), se precisa substituí-la,
    se a lista/estrutura/texto precisam de ajuste.
    """
    if not blocked:
        return None
    names: set[str] = set()
    try:
        data = json.loads((backups_dir / "editorial.blocked.json").read_text(encoding="utf-8"))
        checklist = data.get("blocked_checklist")
        if isinstance(checklist, dict):
            names = {
                str(item.get("name"))
                for item in (checklist.get("items") or [])
                if item.get("status") in ("fail", "error") and item.get("name")
            }
    except (OSError, ValueError):
        pass
    # find_inline_images = o delta real (missing) — vale para blocked legado
    # (sem blocked_checklist) e para qualquer gate que deixe imagens faltando.
    return {
        "find_inline_images": images["missing"],
        "normalize_featured": featured.get("action") == "normalize",
        "replace_featured": featured.get("action") == "replace",
        "provide_featured": featured.get("action") == "provide",
        "normalize_inline": "imagens_webp" in names,
        "remove_irrelevant_images": "relevancia_imagens" in names,
        "fix_list_structure": "estrutura_lista" in names,
        "rewrite_text": "qualidade_texto" in names,
        "provide_trailer": "trailer_youtube" in names,
        "fix_dimensions": "dimensoes_imagens" in names,
        "remove_duplicate_images": "imagens_duplicadas" in names,
    }


def _blocked_reason(backups_dir: Path) -> str | None:
    """Compact failure summary from ``editorial.blocked.json``.

    Token economy: the card tells the agent WHAT the publish gate rejected
    (failing checklist item names, or the reopen reason) so it can fix the
    post without extra file reads. Tolerant: any read/parse error -> None.
    """
    try:
        data = json.loads((backups_dir / "editorial.blocked.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    checklist = data.get("blocked_checklist")
    if isinstance(checklist, dict):
        failed = [
            item.get("name")
            for item in (checklist.get("items") or [])
            if item.get("status") in ("fail", "error") and isinstance(item.get("name"), str)
        ]
        if failed:
            return "checklist: " + ", ".join(failed)
    reason = data.get("reason")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    return "blocked"


def _seo_is_valid(meta: dict[str, Any]) -> bool:
    title = meta.get("rank_math_title")
    description = meta.get("rank_math_description")
    keyword = meta.get("rank_math_focus_keyword")
    return bool(
        isinstance(title, str)
        and title.strip()
        and len(title.strip()) <= 65
        and isinstance(description, str)
        and 120 <= len(description.strip()) <= 160
        and isinstance(keyword, str)
        and keyword.strip()
    )


def _is_recent(post: dict[str, Any], cutoff: datetime.datetime) -> bool:
    value = post.get("date_gmt") or post.get("date")
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed >= cutoff


def _original_link(post: dict[str, Any]) -> str | None:
    meta = post.get("meta")
    if not isinstance(meta, dict):
        return None
    value = meta.get("original_link")
    return value.strip() if isinstance(value, str) and value.strip() else None


def load_draft(root: Path, post_id: int) -> dict[str, Any]:
    """Rascunho editorial persistido (base do rework incremental).

    Lê ``backups/<id>/editorial.draft.json``; sem draft, cai para o
    ``editorial.latest.json`` (legado). O agente carrega o rascunho, corrige
    SOMENTE o componente apontado pelo ``fix`` do card e re-aplica.
    """
    directory = root / "backups" / str(post_id)
    draft = directory / "editorial.draft.json"
    source = draft if draft.is_file() else directory / "editorial.latest.json"
    if not source.is_file():
        raise WorkflowError(f"sem editorial.draft.json nem editorial.latest.json para o post {post_id}")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkflowError(f"draft ilegivel ({source.name}): {exc}") from exc
    if not isinstance(value, dict):
        raise WorkflowError("draft invalido: conteudo nao e um objeto JSON")
    return value


def retry_post(
    client: WordPressClient,
    config: Config,
    root: Path,
    post_id: int,
) -> dict[str, Any]:
    """Reabre um post AWAITING_HUMAN/BLOCKED para nova tentativa automática.

    Operação explícita de revisão humana: zera as tentativas e o cooldown
    (``next_retry_at`` vazio = elegível imediatamente), mantendo o estado
    BLOCKED para o agente ver o card e corrigir. Nunca força READY.
    """
    post = client.get_post(post_id)
    if post.get("status") != "pending":
        raise WorkflowError(f"post {post_id} nao esta pending ({post.get('status')})")
    if config.dry_run:
        raise WorkflowError("retry e uma operacao de escrita: exige write mode (EDITOR_DRY_RUN=false)")
    _write_state_markers(
        client,
        config,
        post_id,
        STATE_BLOCKED,
        attempts=0,
        last_error="reaberto por revisao humana (retry)",
    )
    return {
        "post_id": post_id,
        "status": "retried",
        "state": STATE_BLOCKED,
        "attempts": 0,
        "wordpress_changed": True,
    }


def discard_post(
    client: WordPressClient,
    config: Config,
    root: Path,
    post_id: int,
    reason: str = "",
) -> dict[str, Any]:
    """Descarta um post da fila editorial (decisão humana ou do agente).

    Grava ``uncertain.json`` (escape já existente do pipeline) e o estado
    UNCERTAIN — o post sai da agenda do monitor e nunca publica.
    """
    post = client.get_post(post_id)
    if post.get("status") != "pending":
        raise WorkflowError(f"post {post_id} nao esta pending ({post.get('status')})")
    if config.dry_run:
        raise WorkflowError("discard e uma operacao de escrita: exige write mode (EDITOR_DRY_RUN=false)")
    editorial = {"site_relevance": {"decision": "skip", "confidence": 1.0, "reason": reason or "descartado"}}
    _save_uncertain(root, post_id, editorial)
    _write_state_markers(
        client,
        config,
        post_id,
        STATE_UNCERTAIN,
        last_error=reason or "descartado",
    )
    return {
        "post_id": post_id,
        "status": "discarded",
        "state": STATE_UNCERTAIN,
        "wordpress_changed": True,
    }


def mark_uncertain(
    client: WordPressClient,
    config: Config,
    root: Path,
    post_id: int,
    reason: str,
) -> dict[str, Any]:
    """Registra a decisão do agente de não processar o post agora.

    O agente usava ``uncertain.json`` direto no filesystem; este comando
    valida e persiste também o estado no WordPress (fonte de verdade única).
    """
    if not reason or not reason.strip():
        raise WorkflowError("motivo obrigatorio para marcar uncertain")
    return discard_post(client, config, root, post_id, reason=reason.strip())
