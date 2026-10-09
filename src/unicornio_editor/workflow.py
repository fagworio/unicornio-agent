"""Application workflows shared by the CLI and integration tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Lock

import datetime
import json
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .backup import SnapshotStore, atomic_write_text
from .builder import BuilderError, append_canonical_footer
from .checklist import _required_image_count, run_pre_publish_checklist
from .config import Config
from .editorial_schema import validate_editorial
from .html_cleaner import _repair_orphan_media, clean_html
from .language import editorial_language_report
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
from .media.text import sanitize_title
from .media.source_verify import verify_downloaded_against_source
from .media.vision_cache import get_cached_decision, set_cached_decision
from .media.vision_gate import VisionGateError, verify_image_subject, vision_config_ready
from .media.vision_policy import (
    featured_vision_category,
    featured_vision_subject,
    trusted_featured_evidence,
    vision_cache_subject,
)
from .media.wordpress_media import upload_image
from .observability import append_telemetry, build_processing_markers
from .seo.rank_math import build_meta
from .state import (
    STATE_AWAITING_HUMAN,
    STATE_BLOCKED,
    STATE_PARTIAL,
    STATE_NEW,
    STATE_PUBLISHED,
    STATE_READY,
    STATE_SKIPPED,
    STATE_UNCERTAIN,
    META_READY_HASH,
    build_state_markers,
    cooldown_expired,
    read_state,
    retry_eligible,
    rework_backoff,
    uncertain_second_pass_eligible,
)
from .trailer import (
    TrailerError, build_trailer_html, find_cached_game_trailer_with_status,
    find_game_trailer_with_status,
)
from .wordpress import WordPressClient


# Media plan: download/upload/verificacao sao I/O-bound (rede); threads
# liberam o GIL durante E/S. Falhas de item são registradas no próprio item;
# nunca reexecutamos um lote parcial, pois upload é efeito colateral.
_MEDIA_WORKERS = 4


class WorkflowError(RuntimeError):
    """Raised when a post cannot safely enter a workflow step."""


class MediaFunnelInvariantError(WorkflowError):
    """Raised when media candidates lose their discovery identity."""


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
        "cleaned_html": clean_html(_repair_orphan_media(raw), post_title=_post_title(post) or ""),
        "original_link": _original_link(post),
        "wordpress_changed": False,
    }


def _partial_manifest_path(root: Path, post_id: int) -> Path:
    return root / "backups" / str(post_id) / "editorial.partial.json"


def _load_partial_manifest(root: Path, post_id: int, *, required: bool = False) -> dict[str, Any]:
    path = _partial_manifest_path(root, post_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        if required:
            raise WorkflowError(f"partial_manifest_invalid: post {post_id}: {exc}") from exc
        return {}
    if not isinstance(value, dict):
        if required:
            raise WorkflowError(f"partial_manifest_invalid: post {post_id}: objeto invalido")
        return {}
    accepted = value.get("accepted_media")
    if required and (
        value.get("state") != "partial"
        or value.get("kind") != "media"
        or not isinstance(accepted, list)
        or not isinstance(value.get("required"), int)
        or not isinstance(value.get("completed"), int)
        or not isinstance(value.get("missing"), int)
    ):
        raise WorkflowError(f"partial_manifest_invalid: post {post_id}: campos inconsistentes")
    if required:
        accepted_items = accepted if isinstance(accepted, list) else []
        completed_items = [item for item in accepted_items if isinstance(item, dict) and not item.get("featured")]
        if (
            any(not isinstance(item, dict) or not item.get("media_id") or not item.get("media_url") for item in accepted_items)
            or value["required"] < 0
            or value["completed"] != len(completed_items)
            or value["missing"] != max(0, value["required"] - value["completed"])
        ):
            raise WorkflowError(f"partial_manifest_invalid: post {post_id}: progresso inconsistente")
    return value


def _save_partial_manifest(root: Path, post_id: int, manifest: dict[str, Any]) -> None:
    path = _partial_manifest_path(root, post_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def _partial_media_records(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in (manifest.get("accepted_media") or [])
            if isinstance(item, dict) and item.get("media_url") and not item.get("featured")]


def _partial_retry(
    no_progress_attempts: int,
    *,
    cooldown_minutes: int,
    max_no_progress_attempts: int,
    now: datetime.datetime | None = None,
) -> dict[str, Any]:
    """Cooldown exclusivo de PARTIAL; progresso nunca consome passes."""
    if no_progress_attempts >= max_no_progress_attempts:
        return {"state": STATE_AWAITING_HUMAN, "next_retry_at": ""}
    multiplier = 4 ** max(0, no_progress_attempts)
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    retry_at = moment + datetime.timedelta(minutes=cooldown_minutes * multiplier)
    return {"state": STATE_PARTIAL, "next_retry_at": retry_at.isoformat(timespec="seconds")}


def _effective_partial_kind(state_info: dict[str, Any]) -> str | None:
    """Map legacy media markers to the current featured-vision retry kind."""
    kind = state_info.get("partial_kind")
    last_error = str(state_info.get("last_error") or "").casefold()
    if (
        kind == "media"
        and int(state_info.get("partial_missing") or 0) == 0
        and any(token in last_error for token in ("imagens_visao", "imagens visão", "featured vision"))
    ):
        return "featured_vision"
    return kind


def _recover_partial_featured(
    client: WordPressClient,
    post_id: int,
    manifest: dict[str, Any],
) -> tuple[int | None, str | None]:
    featured = manifest.get("featured") or {}
    if featured.get("status") != "valid":
        return None, None
    media_id = featured.get("media_id")
    if not isinstance(media_id, int) or media_id <= 0:
        raise WorkflowError(f"partial_manifest_invalid: featured sem media_id no post {post_id}")
    try:
        client.get_media(media_id)
    except Exception as exc:  # noqa: BLE001 - manifesto fail-closed
        raise WorkflowError(
            f"partial_manifest_invalid: featured {media_id} ausente no post {post_id}"
        ) from exc
    return media_id, str(featured.get("credit_text") or "") or None
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
    started_at = time.monotonic()
    post = client.get_post(post_id)
    _require_pending(post)
    state_before = read_state(post)
    partial_path = _partial_manifest_path(root, post_id)
    if state_before.get("state") == STATE_PARTIAL:
        partial_manifest = _load_partial_manifest(root, post_id, required=True)
    else:
        partial_manifest = {}
        if partial_path.is_file():
            append_telemetry(root, "partial_manifest_orphaned", post_id=post_id)
    prior_media = _partial_media_records(partial_manifest)
    attempts_before = state_before["attempts"]
    append_telemetry(
        root, "apply_started", post_id=post_id,
        attempt=attempts_before + 1, first_pass=attempts_before == 0,
    )
    backup = SnapshotStore(root).save(post_id, post)
    editorial = validate_editorial(payload, min_confidence=config.min_relevance_confidence)
    decision = editorial["site_relevance"]["decision"]
    confidence = float(editorial["site_relevance"].get("confidence") or 0.0)
    if decision == "skip" and confidence < config.min_skip_confidence:
        # Conservative skip (token + accuracy policy): a low-confidence skip is
        # NOT final — record it as uncertain so the post stays pending (out of
        # the processing queue, visible for review) instead of being dropped
        # forever via editorial.latest.json.
        # UNCERTAIN tambem nao altera conteudo: a duvida e sobre pertencer ou
        # nao ao portal, entao mexer no WordPress seria prematuro.
        baseline_changed = False
        _save_uncertain(root, post_id, editorial)
        attempts_after = attempts_before + 1
        # Um UNCERTAIN volta ao ciclo somente UMA vez. Se a segunda passagem
        # também não consegue decidir com segurança, a automação encerra a
        # tentativa e entrega o post explicitamente ao humano.
        uncertain_state = (
            STATE_AWAITING_HUMAN if attempts_before >= 1 else STATE_UNCERTAIN
        )
        _backoff_u = rework_backoff(
            attempts_after,
            cooldown_minutes=config.rework_cooldown_minutes,
            max_attempts=config.max_rework_attempts,
        )
        uncertain_retry = (
            _backoff_u["next_retry_at"] if uncertain_state == STATE_UNCERTAIN else ""
        )
        if not _write_state_markers(
            client,
            config,
            post_id,
            uncertain_state,
            root=root,
            attempts=attempts_after,
            next_retry_at=uncertain_retry,
            last_error=editorial["site_relevance"]["reason"],
        ):
            raise WorkflowError(
                f"estado {uncertain_state.upper()} nao persistiu no WordPress "
                f"(post {post_id}); sem estado o post reaparece na fila"
            )
        if uncertain_state == STATE_AWAITING_HUMAN and not config.dry_run:
            client.move_to_status(post_id, "awaiting_human")
        append_telemetry(
            root, "apply_uncertain" if uncertain_state == STATE_UNCERTAIN
            else "apply_uncertain_escalated",
            post_id=post_id, reason=editorial["site_relevance"]["reason"],
            attempts=attempts_after, state=uncertain_state,
        )
        return {
            "post_id": post_id,
            "wordpress_changed": baseline_changed,
            "dry_run": config.dry_run,
            "status": "uncertain" if uncertain_state == STATE_UNCERTAIN else "awaiting_human",
            "state": uncertain_state,
            "baseline_enriched": baseline_changed,
            "skip_reason": editorial["site_relevance"]["reason"],
            "confidence": confidence,
            "attempts": attempts_after,
            "backup": str(backup),
        }
    if decision == "process":
        editorial = resolve_editorial_defaults(editorial, post)
    _save_editorial_latest(root, post_id, editorial)
    if decision == "skip":
        # SKIPPED = zero alteracao de conteudo no WordPress (o motivo do skip
        # pode ser justamente o conteudo nao pertencer ao portal). Persistir
        # CTA/fonte/links aqui contradizia o contrato de seguranca do README.
        baseline_changed = False
        # Fase 17 (fail-closed): um SKIPPED nao persistido volta a ser
        # processado na proxima janela (o editorial.latest.json ja foi gravado).
        if not _write_state_markers(
            client,
            config,
            post_id,
            STATE_SKIPPED,
            root=root,
            last_error=editorial["site_relevance"]["reason"],
        ):
            raise WorkflowError(
                f"estado SKIPPED nao persistiu no WordPress (post {post_id}); "
                "o post voltaria a fila como se nunca tivesse sido avaliado"
            )
        append_telemetry(
            root, "apply_skipped",
            post_id=post_id, reason=editorial["site_relevance"]["reason"],
        )
        return {
            "post_id": post_id,
            "wordpress_changed": baseline_changed,
            "dry_run": config.dry_run,
            "status": "skipped",
            "state": STATE_SKIPPED,
            "baseline_enriched": baseline_changed,
            "skip_reason": editorial["site_relevance"]["reason"],
            "backup": str(backup),
        }

    # Draft persistido ANTES da execução pesada: mesmo que download/upload/
    # checklist falhem, o trabalho editorial fica salvo e o rework corrige
    # somente o componente com problema (nunca reescreve o texto nem re-gera
    # SEO do zero).
    _save_draft(root, post_id, editorial)

    partial_slots = {
        item.get("paragraph_index")
        for item in prior_media
        if isinstance(item.get("paragraph_index"), int)
    }
    effective_kind = _effective_partial_kind(state_before)
    partial_featured = partial_manifest.get("featured") or {}
    legacy_featured_vision = (
        effective_kind == "featured_vision"
        and partial_featured.get("status") == "valid"
    )
    if legacy_featured_vision:
        partial_featured = {
            **partial_featured,
            "status": "vision_rejected",
            "reason": str(state_before.get("last_error") or "featured vision rejected"),
        }
        partial_manifest = {**partial_manifest, "featured": partial_featured}
    partial_featured_valid = partial_featured.get("status") == "valid"
    partial_featured_rejected = partial_featured.get("status") == "vision_rejected"
    if partial_manifest:
        remaining_plan = []
        for item in (editorial.get("media_plan") or []):
            if isinstance(item, dict) and item.get("is_featured") and partial_featured_valid:
                continue
            if isinstance(item, dict) and item.get("paragraph_index") in partial_slots:
                continue
            remaining_plan.append(item)
        editorial = {**editorial, "media_plan": remaining_plan}
    media_preflight = validate_media_plan(
        client,
        editorial,
        config=config,
        root=root,
        post_title=_post_title(post),
        post_id=post_id,
        existing_featured_id=(
            None
            if partial_featured_rejected
            else int(post.get("featured_media") or 0) or None
        ),
    )
    media_results, featured_id, featured_credit = _execute_media_plan(
        editorial, config, client, root, preflight=media_preflight, post_id=post_id
    )
    combined_media_results = prior_media + [
        item for item in media_results
        if item.get("media_url") and not item.get("featured")
    ]
    if featured_id is None and partial_featured_valid:
        featured_id, featured_credit = _recover_partial_featured(
            client, post_id, partial_manifest
        )
    if featured_id is None and not config.dry_run and not partial_featured_rejected:
        featured_id = _normalize_existing_featured(client, config, post, editorial, root=root)
    html = editorial["cleaned_html"]
    if combined_media_results and not config.dry_run:
        plan = [
            {
                "paragraph_index": result["paragraph_index"],
                "media_url": result["media_url"],
                "alt_text": result["alt_text"],
                "credit_text": result["credit_text"],
                "width": result.get("width"),
                "height": result.get("height"),
            }
            for result in combined_media_results
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
    content, trailer, trailer_status = compose_final_content(
        editorial_with_media, config, original_link_of(post), root=root
    )
    editorial_with_media = attach_trailer_audit(
        editorial_with_media, trailer, search_status=trailer_status
    )
    # Atualiza o artefato durável com o resultado determinístico da busca. Em
    # uma revalidação STALE, o checklist consegue auditar por que não há embed.
    _save_editorial_latest(root, post_id, editorial_with_media)
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
    checklist = run_pre_publish_checklist(
        post={
            **post,
            "featured_media": (
                featured_id
                if featured_id is not None
                else (None if partial_featured_rejected else post.get("featured_media"))
            ),
        },
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
            media_only = all(
                any(token in str(item.get("name") or "").lower() for token in ("imagem", "imagens", "image", "media", "destaque"))
                for item in failed_items
            )
            current_accepted = [
                item for item in media_results
                if item.get("media_url") and item.get("media_id") and not item.get("featured")
            ]
            current_featured = next(
                (item for item in media_results if item.get("featured") and item.get("media_id")),
                None,
            )
            if media_only:
                accepted: list[dict[str, Any]] = []
                seen_media: set[int] = set()
                for item in prior_media + current_accepted:
                    media_id = int(item.get("media_id") or 0)
                    if media_id and media_id not in seen_media:
                        accepted.append(item)
                        seen_media.add(media_id)
                featured_before = partial_manifest.get("featured") or {}
                featured_manifest = dict(featured_before) if isinstance(featured_before, dict) else {"status": "missing"}
                featured_vision_failure = next(
                    (
                        row for row in (media_preflight.get("featured_vision") or [])
                        if isinstance(row, dict) and row.get("status") == "rejected"
                    ),
                    None,
                )
                checklist_featured_failure = next(
                    (
                        item for item in failed_items
                        if any(token in str(item.get(key) or "").casefold()
                               for key in ("name", "detail")
                               for token in ("featured", "imagens_visao", "imagens visão", "vision"))
                    ),
                    None,
                )
                if not featured_vision_failure and checklist_featured_failure:
                    featured_vision_failure = {
                        "reason": str(checklist_featured_failure.get("detail") or checklist_featured_failure.get("name") or "featured vision rejected"),
                        "index": None,
                    }
                if featured_vision_failure:
                    failed_index = featured_vision_failure.get("index")
                    candidate = (
                        (editorial.get("media_plan") or [])[failed_index]
                        if isinstance(failed_index, int)
                        and 0 <= failed_index < len(editorial.get("media_plan") or [])
                        else {}
                    )
                    rejected_id = candidate.get("media_library_id") or featured_id or featured_manifest.get("media_id")
                    rejected_url = str(
                        candidate.get("direct_image_url")
                        or (current_featured or {}).get("media_url")
                        or featured_manifest.get("media_url")
                        or ""
                    )
                    if isinstance(rejected_id, int) and rejected_id > 0:
                        try:
                            rejected_url = str(client.get_media(rejected_id).get("source_url") or rejected_url)
                        except Exception:  # noqa: BLE001 - preserve the candidate evidence
                            pass
                    featured_manifest.update({
                        "status": "vision_rejected",
                        "media_id": rejected_id,
                        "media_url": rejected_url,
                        "reason": str(
                            featured_vision_failure.get("reason")
                            or featured_manifest.get("reason")
                            or "featured vision rejected"
                        ),
                    })
                elif featured_id:
                    # Upload existence is not a gate result. A featured is
                    # valid only after the checklist has been evaluated and no
                    # featured vision gate failed.
                    featured_manifest["status"] = "valid"
                    featured_manifest["media_id"] = featured_id
                    featured_manifest["media_url"] = (current_featured or {}).get("media_url") or featured_manifest.get("media_url", "")
                    featured_manifest["credit_text"] = featured_credit or featured_manifest.get("credit_text", "")
                images_summary, media_drift = _reconcile_partial_media(
                    content,
                    _post_title(post) or editorial["seo"]["title"],
                    image_entities,
                    accepted,
                    stored={
                        "required": partial_manifest.get("required", state_before.get("partial_required", 0)),
                        "completed": partial_manifest.get("completed", state_before.get("partial_completed", 0)),
                        "missing": partial_manifest.get("missing", state_before.get("partial_missing", 0)),
                    },
                )
                required = images_summary["required"]
                completed = images_summary["valid"]
                missing = images_summary["missing"]
                if media_drift:
                    append_telemetry(root, "partial_media_drift", post_id=post_id, **media_drift)
                featured_status = str(featured_manifest.get("status") or "missing")
                if missing and featured_status not in {"missing", "valid"}:
                    partial_kind = "mixed_media"
                elif missing:
                    partial_kind = "inline_missing"
                elif featured_status in {"rejected", "vision", "failed", "vision_rejected"}:
                    partial_kind = "featured_vision"
                elif featured_status != "valid":
                    partial_kind = "featured_missing"
                else:
                    partial_kind = "inline_missing"
                previous_completed = len(prior_media)
                featured_before_valid = (
                    (partial_manifest.get("featured") or {}).get("status") == "valid"
                )
                featured_progress = bool(featured_id) and not featured_before_valid
                progress = completed > previous_completed or featured_progress
                state_info = read_state(post)
                passes = state_info.get("processing_passes", 0) + 1
                no_progress = 0 if progress else state_info.get("no_progress_attempts", 0) + 1
                partial_state = _partial_retry(
                    no_progress,
                    cooldown_minutes=config.rework_cooldown_minutes,
                    max_no_progress_attempts=config.max_partial_no_progress_attempts,
                )["state"]
                partial_retry = _partial_retry(
                    no_progress,
                    cooldown_minutes=config.rework_cooldown_minutes,
                    max_no_progress_attempts=config.max_partial_no_progress_attempts,
                )
                manifest = {
                    "state": "partial",
                    "kind": "media",
                    "required": required,
                    "completed": completed,
                    "missing": missing,
                    "partial_kind": partial_kind,
                    "accepted_media": accepted,
                    "featured": featured_manifest,
                    "processing_passes": passes,
                    "no_progress_attempts": no_progress,
                }
                _save_partial_manifest(root, post_id, manifest)
                next_retry = partial_retry["next_retry_at"]
                if not _write_state_markers(
                    client, config, post_id, partial_state, root=root,
                    attempts=state_info["attempts"], next_retry_at=next_retry,
                    last_error="; ".join(str(item.get("detail") or "")[:160] for item in failed_items[:5]),
                    partial_kind=partial_kind, partial_required=required,
                    partial_completed=completed, partial_missing=missing,
                    processing_passes=passes, no_progress_attempts=no_progress,
                ):
                    raise WorkflowError(f"estado {partial_state} nao persistiu no WordPress (post {post_id})")
                if partial_state == STATE_AWAITING_HUMAN and not config.dry_run:
                    client.move_to_status(post_id, "awaiting_human")
                append_telemetry(
                    root, "partial_started" if not partial_manifest else "partial_resumed",
                    post_id=post_id, required=required, completed=completed, missing=missing,
                )
                if partial_state == STATE_AWAITING_HUMAN:
                    append_telemetry(root, "partial_escalated", post_id=post_id, no_progress_attempts=no_progress)
                append_telemetry(
                    root, "partial_progress" if progress else "partial_no_progress",
                    post_id=post_id, required=required, completed=completed,
                    missing=missing, processing_passes=passes,
                    no_progress_attempts=no_progress,
                )
                return {
                    "post_id": post_id, "wordpress_changed": False, "dry_run": False,
                    "status": "partial" if partial_state == STATE_PARTIAL else "awaiting_human",
                    "state": partial_state, "attempts": state_info["attempts"],
                    "processing_passes": passes, "no_progress_attempts": no_progress,
                    "partial": manifest, "checklist": checklist,
                    "media_plan_results": media_results,
                }
            # O gate continua impedindo READY/publicacao; falhas não relacionadas
            # aprimoramentos mecanicos e seguros do post: CTA, Fonte e links
            # internos persistem no conteudo original antes de ele entrar em
            # rework ou AWAITING_HUMAN. O editorial incompleto (texto/midia)
            # fica somente no draft, para nunca gravar um post que falhou no
            # checklist.
            baseline_changed = _persist_baseline_enrichment(client, config, post_id, post)
            state_info = read_state(post)
            attempts = state_info["attempts"] + 1
            media_failure = any(
                "imagem" in str(item.get("name") or "").lower()
                or "media" in str(item.get("name") or "").lower()
                or "destaque" in str(item.get("name") or "").lower()
                for item in failed_items
            )
            media_search_attempts = state_info.get("media_search_attempts", 0)
            if media_failure:
                media_search_attempts += 1
            # Falhas de SEO, texto ou trailer não contam como tentativas de
            # mídia. O contador de apply é independente do contador de buscas.
            deterministic_exhausted = (
                media_failure
                and media_search_attempts >= config.max_media_search_attempts
            )
            if deterministic_exhausted and detect_list_format(
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
            # Fase 17 (fail-closed) nos estados terminais do rework; BLOCKED
            # tolera falha de escrita (o post continua na fila de qualquer
            # forma), mas devolve `state_persisted` no relatorio.
            _state_ok = _write_state_markers(
                client,
                config,
                post_id,
                backoff["state"],
                root=root,
                attempts=backoff["attempts"],
                next_retry_at=backoff["next_retry_at"],
                last_error=last_error,
                media_search_attempts=media_search_attempts,
            )
            if not _state_ok and backoff["state"] in (STATE_AWAITING_HUMAN, STATE_UNCERTAIN):
                raise WorkflowError(
                    f"estado {backoff['state']} nao persistiu no WordPress "
                    f"(post {post_id}); sem estado o post nao entra na fila humana "
                    "de forma confiavel — verifique a conexao e reaplique"
                )
            featured_normalized = False
            # AWAITING_HUMAN sai da fila de trilhagem: move o status WP para o
            # filtro "Awaiting Human" (visivel para decisao humana). Falha de
            # status nao derruba o apply — a meta _hermes_state ja marca.
            if backoff["state"] == STATE_AWAITING_HUMAN:
                # A imagem de destaque já pode ter sido normalizada durante o
                # preflight, mas um apply bloqueado não chega ao payload READY
                # que a associa ao post. Preserve essa correção técnica agora;
                # ela não torna o post publicável nem altera seu estado humano.
                # Se a featured era irrelevante ao editorial, ainda assim
                # convertemos a imagem existente para WebP: a revisão humana
                # decide depois se ela permanece ou é substituída.
                awaiting_featured_id = featured_id or _normalize_existing_featured(
                    client, config, post, root=root
                )
                if (
                    isinstance(awaiting_featured_id, int)
                    and awaiting_featured_id > 0
                    and awaiting_featured_id != int(post.get("featured_media") or 0)
                ):
                    try:
                        client.update_post(post_id, {"featured_media": awaiting_featured_id})
                        featured_normalized = True
                    except Exception:  # noqa: BLE001 - best-effort tecnico
                        pass
                try:
                    client.move_to_status(post_id, "awaiting_human")
                    # Verificacao POS-ESCRITA: o operador precisa achar o post no
                    # filtro "Awaiting Human" do WP. Se o status nao mudou, a
                    # meta diz awaiting_human e o WP continua pending — o humano
                    # abre a fila e o post nao esta la (divergencia silenciosa).
                    _conf = client.get_post(post_id)
                    _st_wp = str(_conf.get("status") or "")
                    if _st_wp != "awaiting_human":
                        append_telemetry(
                            root, "awaiting_human_status_mismatch",
                            post_id=post_id, status_wp=_st_wp,
                            state_meta=str((_conf.get("meta") or {}).get("_hermes_state") or ""),
                        )
                except Exception as exc:  # noqa: BLE001 - best-effort
                    try:
                        append_telemetry(
                            root, "awaiting_human_move_failed",
                            post_id=post_id, error=str(exc)[:200],
                        )
                    except Exception:  # noqa: BLE001
                        pass
            images_summary = _images_summary(
                content, _post_title(post) or editorial["seo"]["title"], image_entities
            )
            append_telemetry(
                root, "apply_blocked",
                post_id=post_id,
                attempts=backoff["attempts"],
                # O plano manda na atribuição: cada imagem carrega o
                # decision_id que a escolheu (item 3 auto != item 4 choose).
                **_decision_fields(root, post_id, payload.get("media_plan")),
                reason=", ".join(item["name"] for item in failed_items),
                missing_images=images_summary.get("missing", 0),
                valid_images=images_summary.get("valid", 0),
                blocked_detail=(
                    "; ".join(str(item.get("detail") or "")[:120] for item in failed_items[:3])
                ),
                failure_reasons=[item["name"] for item in failed_items],
                first_pass=attempts_before == 0,
                duration_ms=round((time.monotonic() - started_at) * 1000),
            )
            for item in failed_items:
                append_telemetry(
                    root,
                    "checklist_block",
                    post_id=post_id,
                    attempt=backoff["attempts"],
                    reason=item["name"],
                    detail=str(item.get("detail") or "")[:200],
                )
            return {
                "post_id": post_id,
                "wordpress_changed": baseline_changed or featured_normalized,
                "dry_run": False,
                "status": "needs_rework",
                "state": backoff["state"],
                "baseline_enriched": baseline_changed,
                "attempts": backoff["attempts"],
                "next_retry_at": backoff["next_retry_at"],
                "backup": str(backup),
                "checklist": checklist,
                "media_plan_results": media_results,
                "inline_normalization": inline_normalization,
                "featured_normalized": featured_normalized,
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
                media_search_attempts=0,
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
    if partial_manifest:
        append_telemetry(root, "partial_completed", post_id=post_id)
    append_telemetry(
        root,
        "apply_ready",
        post_id=post_id,
        attempts=attempts_before + 1,
        **_decision_fields(root, post_id, payload.get("media_plan")),
        first_pass=attempts_before == 0,
        duration_ms=round((time.monotonic() - started_at) * 1000),
    )
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
        for name in ("editorial.blocked.json", "uncertain.json", "editorial.partial.json"):
            marker = directory / name
            if marker.is_file():
                marker.unlink()
    except OSError:
        pass


def _baseline_content(post: dict[str, Any], config: Config) -> str:
    """Build deterministic improvements safe for a non-READY post.

    This deliberately starts from the original WordPress content, not from an
    editorial draft that failed the checklist. It preserves CTA, Fonte and
    internal links without persisting incomplete text or media.
    """
    html = clean_html(_repair_orphan_media(_raw_content(post)), post_title=_post_title(post) or "")
    if config.internal_links_enabled:
        from .internal_links import add_internal_links

        html = add_internal_links(html)
    from .media.text import dedupe_credit_figures

    html = dedupe_credit_figures(html)
    try:
        return append_canonical_footer(html, original_link_of(post))
    except BuilderError:
        # Uma origem legado invalida nao pode impedir o CTA e os links
        # internos; Fonte so e omitida quando nao ha URL segura para exibir.
        return append_canonical_footer(html, None)


def _persist_baseline_enrichment(
    client: WordPressClient, config: Config, post_id: int, post: dict[str, Any]
) -> bool:
    """Persist only safe baseline enrichment for skipped or blocked posts."""
    if config.dry_run:
        return False
    try:
        content = _baseline_content(post, config)
        if content == _raw_content(post):
            return False
        client.update_post(post_id, {"content": {"raw": content}})
        return True
    except Exception:  # noqa: BLE001 - state handling must remain fail-safe
        return False


# Estados em que perder a persistência é operacionalmente crítico: o post fica
# fora de sincronia com a fila (reaparece, gera retry e custo de LLM de novo).
_STATE_MARKER_CRITICAL = frozenset({
    STATE_READY, STATE_SKIPPED, STATE_UNCERTAIN, STATE_BLOCKED, STATE_PARTIAL, STATE_AWAITING_HUMAN,
})


def _write_state_markers(
    client: WordPressClient,
    config: Config,
    post_id: int,
    state: str,
    *,
    root: Path | None = None,
    attempts: int = 0,
    next_retry_at: str = "",
    last_error: str = "",
    ready_hash: str = "",
    media_search_attempts: int | None = None,
    partial_kind: str = "",
    partial_required: int | None = None,
    partial_completed: int | None = None,
    partial_missing: int | None = None,
    processing_passes: int | None = None,
    no_progress_attempts: int | None = None,
) -> bool:
    """Persiste o estado operacional ``_hermes_*`` no WordPress (write mode).

    Falha aqui NÃO derruba o fluxo — o pior caso é o post ficar sem estado e o
    publish-ready revalidar pelo checklist (mais caro, nunca inseguro).

    Mas também não é mais silenciosa: uma falha de escrita deixa o estado
    DISTRIBUÍDO inconsistente (filesystem/draft avança, WordPress não), o que
    faz o post reaparecer na fila, gerar retry desnecessário (custo de LLM de
    novo) e divergir dos relatórios. Retorna True quando confirmado e registra
    telemetria ``state_persist_failed`` (crítica nos estados terminais).
    """
    if config.dry_run:
        return True
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
                    media_search_attempts=media_search_attempts,
                    partial_kind=partial_kind,
                    partial_required=partial_required,
                    partial_completed=partial_completed,
                    partial_missing=partial_missing,
                    processing_passes=processing_passes,
                    no_progress_attempts=no_progress_attempts,
                    policy_version=config.policy_version,
                )
            },
        )
        if state == STATE_PARTIAL:
            persisted = client.get_post(post_id)
            meta = persisted.get("meta") if isinstance(persisted, dict) else {}
            expected = {
                "_hermes_partial_kind": partial_kind,
                "_hermes_media_required": str(partial_required),
                "_hermes_media_completed": str(partial_completed),
                "_hermes_media_missing": str(partial_missing),
                "_hermes_processing_passes": str(processing_passes),
                "_hermes_no_progress_attempts": str(no_progress_attempts),
            }
            if not isinstance(meta, dict) or any(meta.get(key) != value for key, value in expected.items()):
                raise WorkflowError(f"round-trip das metas PARTIAL falhou no post {post_id}")

    except Exception as exc:  # noqa: BLE001 - telemetria nunca bloqueia o fluxo
        critical = state in _STATE_MARKER_CRITICAL
        if root is not None:
            try:
                append_telemetry(
                    root, "state_persist_failed",
                    post_id=post_id, state=state,
                    error=str(exc)[:200], critical=critical,
                )
            except Exception:  # noqa: BLE001 - telemetria jamais derruba o fluxo
                pass
        return False
    return True


def _save_draft(root: Path, post_id: int, editorial: dict[str, Any]) -> None:
    """Persiste o rascunho editorial resolvido ANTES da execução pesada.

    ``editorial.draft.json`` é a base do rework: o agente carrega o rascunho,
    corrige SOMENTE o componente com problema (media_plan, seo, texto) e
    re-aplica — o trabalho editorial caro nunca é refeito do zero.
    """
    try:
        directory = root / "backups" / str(post_id)
        directory.mkdir(parents=True, exist_ok=True)
        atomic_write_text(directory / "editorial.draft.json", json.dumps(editorial, ensure_ascii=False, indent=2))
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
                    url_policy=config.remote_url_policy,
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


def _reconcile_partial_media(
    content: str,
    title: str,
    entities: set[str] | None,
    accepted_media: list[dict[str, Any]] | None,
    *,
    stored: dict[str, Any] | None = None,
) -> tuple[dict[str, int], dict[str, Any] | None]:
    """Reconcile real HTML coverage with assets recorded by a PARTIAL run."""
    summary = _images_summary(content, title, entities)
    content_urls: set[str] = set()
    for item in iter_content_images(content):
        src = str(item.get("src") or "").strip()
        if src and image_is_relevant(
            alt_text=str(item.get("alt") or ""),
            credit_text=str(item.get("caption") or ""),
            source_url=src,
            entities=entities or set(),
        ):
            content_urls.add(src)
    accepted_urls = {
        str(item.get("media_url") or "").strip()
        for item in (accepted_media or [])
        if isinstance(item, dict) and not item.get("featured") and item.get("media_url")
    }
    required = summary["required"]
    effective_valid = min(required, len(content_urls | accepted_urls))
    reconciled = {**summary, "required": required, "valid": effective_valid, "missing": max(0, required - effective_valid)}
    drift = None
    if stored is not None:
        stored_value = {
            "required": int(stored.get("required") or 0),
            "completed": int(stored.get("completed") or 0),
            "missing": int(stored.get("missing") or 0),
        }
        derived_value = {
            "required": reconciled["required"],
            "completed": reconciled["valid"],
            "missing": reconciled["missing"],
        }
        if stored_value != derived_value:
            drift = {"stored": stored_value, "derived": derived_value}
    return reconciled, drift



def _expected_subject(item: dict[str, Any], editorial: dict[str, Any] | None) -> str:
    """Subject ESPERADO da seção do item (P1 da auditoria).

    O `subject` do media_plan é dado do AGENTE: um plano inconsistente
    (seção = Bleach, subject declarado = Naruto) passava pela validação. A fonte
    de verdade é a SEÇÃO — o H2 do listicle que contém o parágrafo do item.
    Devolve "" quando não dá para determinar (artigo normal sem H2 numerado).
    """
    if not isinstance(editorial, dict):
        return ""
    html = str(editorial.get("cleaned_html") or "")
    if not html:
        return ""
    h2s = list(
        re.finditer(r"<h2\b[^>]*>(.*?)</h2>", html, re.IGNORECASE | re.DOTALL)
    )
    if not h2s:
        return ""
    numerados: list[tuple[int, str, int]] = []
    for achado in h2s:
        limpo = " ".join(re.sub(r"<[^>]+>", " ", achado.group(1)).split())
        numero = re.match(r"^\s*(\d+)\s*[.)]\s*(.+)$", limpo)
        if numero:
            numerados.append((int(numero.group(1)), numero.group(2).strip(), achado.start()))
    if len(numerados) < 2:
        return ""
    # Qual item da seção? O paragraph_index aponta para o parágrafo; o item é o
    # último H2 numerado ANTES dele no HTML.
    indice_paragrafo = item.get("paragraph_index")
    limite = -1
    if isinstance(indice_paragrafo, int) and indice_paragrafo >= 0:
        paragrafos = list(re.finditer(r"<p\b[^>]*>", html, re.IGNORECASE))
        if indice_paragrafo < len(paragrafos):
            limite = paragrafos[indice_paragrafo].start()
    if limite < 0:
        return numerados[0][1] if numerados else ""
    candidatos = [texto for _n, texto, pos in numerados if pos <= limite]
    return candidatos[-1] if candidatos else numerados[0][1]


def _item_entities(
    item: dict[str, Any],
    entities: set[str],
    editorial: dict[str, Any] | None = None,
) -> set[str]:
    """Entidades que valem para ESTE item (P1 da auditoria).

    ATENÇÃO — estado ATUAL: usa o `subject` DECLARADO no media_plan (dado do
    agente) e, sem ele, as entidades do artigo. O subject derivado da SEÇÃO
    (`_expected_subject`) ainda NÃO substitui o declarado: o mapeamento
    H2<->paragraph_index precisa de mais cuidado (ativar antes disso rejeitou
    itens legítimos de listicle nos testes). Ou seja, a proteção contra um plano
    inconsistente (seção = Bleach, subject = Naruto) ainda NÃO existe.
    """
    declarado = " ".join(str(item.get("subject") or "").split()).strip().lower()
    # O subject esperado da SEÇÃO fica disponível (auditoria/futuro), mas ainda
    # NÃO substitui o declarado: o mapeamento seção<->item precisa de mais
    # cuidado (um H2 numerado nem sempre corresponde ao paragraph_index).
    if declarado:
        locais = {declarado}
        locais.update(e for e in entities if e and e.lower() in declarado)
        extras = item.get("subjects") or ()
        if isinstance(extras, str):
            extras = (extras,)
        locais.update(
            " ".join(str(value).split()).casefold()
            for value in extras
            if str(value or "").strip()
        )
        return locais
    return set(entities)


def _item_evidence_relevant(
    item: dict[str, Any],
    entities: set[str],
    editorial: dict[str, Any] | None = None,
) -> bool:
    """Relevância por EVIDÊNCIA DE ORIGEM (nunca alt/credit/search_query).

    Compartilhada entre ``_execute_media_plan`` (apply) e ``validate_media_plan``
    (media-validate): uma única política de imagem para o pipeline inteiro.
    """
    # A evidência é o que a ORIGEM diz (arquivo + página). O subject NÃO entra
    # aqui: ele é o alvo da checagem (entra como entities), e incluí-lo na
    # própria evidência o faria casar consigo mesmo — qualquer item passaria.
    evidencia = " ".join(
        str(item.get(key) or "")
        for key in ("direct_image_url", "source_page_url")
    )
    return bool(
        image_is_relevant(
            alt_text="",
            credit_text="",
            source_url=evidencia,
            search_query="",
            entities=_item_entities(item, entities, editorial),
        )
    )


def _media_item_rejection(
    item: dict[str, Any],
    entities: set[str],
    client: WordPressClient,
    attachment_cache: dict[int, dict[str, Any]],
    editorial: dict[str, Any] | None = None,
    root: Path | None = None,
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
    from .media.page_assets import is_noise_image_url

    candidate_url = str(
        (attachment or {}).get("source_url") if attachment is not None
        else item.get("direct_image_url") or ""
    )
    if is_noise_image_url(candidate_url):
        return "imagem de avatar/perfil nao e midia editorial; escolha uma imagem da noticia ou da obra"
    if attachment is not None:
        title = str((attachment.get("title") or {}).get("rendered") or "")
        alt = str(attachment.get("alt_text") or "")
        caption = str((attachment.get("caption") or {}).get("rendered") or "")
        url = str(attachment.get("source_url") or "")
        credit = " ".join(part for part in (title, caption) if part)
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
                entities=_item_entities(item, entities, editorial), source_only=True,
            ):
                listed = ", ".join(sorted(entities)) or "nenhuma"
                return (
                    "featured reusada deve retratar o assunto citado "
                    f"(attachment sem as entidades: {listed}); escolha key art/imagem do jogo/obra"
                )
            return None
        # Reuso: aqui alt/título/caption vem do ATTACHMENT ORIGINAL (não do
        # agente), então contam como evidência legítima do que a imagem é.
        if not image_is_relevant(
            alt_text="", credit_text="", source_url=source,
            search_query="", entities=_item_entities(item, entities, editorial),
        ):
            listed = ", ".join(sorted(_item_entities(item, entities, editorial))) or "nenhuma"
            return f"imagem sem relacao com o conteudo (entidades distintas: {listed})"
        return None
    if is_featured:
        # Featured must depict the cited subject itself: only the real
        # source file/page name counts as evidence. The agent-written
        # alt/credit can decorate a wrong image (e.g. a Disney castle
        # captioned "presente em Kingdom Hearts" for a game post), but a
        # true key art file name carries the game/work name.
        # NOTA (auditoria): a featured AINDA considera o `search_query` do
        # agente. A unificação (featured passando pelo mesmo evidence_score, sem
        # texto do agente como prova) é o item "Vision unificada" da auditoria —
        # mexer aqui sem unificar a vision criaria DUAS políticas piores.
        if not image_is_relevant(
            alt_text="",
            credit_text="",
            source_url=" ".join(
                str(item.get(key) or "") for key in ("direct_image_url", "source_page_url")
            ),
            search_query=str(item.get("search_query") or ""),
            entities=_item_entities(item, entities, editorial),
            source_only=True,
        ):
            listed = ", ".join(sorted(_item_entities(item, entities, editorial))) or "nenhuma"
            return (
                "featured deve retratar o assunto citado (arquivo/pagina de origem "
                f"sem as entidades: {listed}); escolha key art/imagem do jogo/obra"
            )
        return None
    if root is not None and not is_featured:
        from .media.vision_cache import get_cached_decision
        cached = get_cached_decision(root, str(item.get("direct_image_url") or ""), str(item.get("subject") or editorial.get("game_name") if isinstance(editorial, dict) else ""))
        if cached and cached.get("status") == "MATCH" and float(cached.get("confidence") or 0) >= 0.80:
            return None
    # P1 (auditoria): alt/credit/search_query escritos pelo AGENTE não são
    # evidência — uma imagem errada com alt "Bleach anime" passava no apply
    # embora a descoberta já a tivesse rejeitado (o agente provava a si mesmo).
    # Aplicado aqui: só a evidência de ORIGEM (arquivo + página) e o subject que
    # a descoberta carimbou no item (entidades LOCAIS, não o conjunto do artigo).
    if not _item_evidence_relevant(item, entities, editorial):
        listed = ", ".join(sorted(_item_entities(item, entities, editorial))) or "nenhuma"
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
    *,
    config: Config | None = None,
    root: Path | None = None,
    post_title: str = "",
    existing_featured_id: int | None = None,
    post_id: int | None = None,
) -> dict[str, Any]:
    """Valida o media_plan de um editorial SEM executar download/upload.

    Retorna ``{valid, rejected: [{index, reason}]}`` — o agente corrige o
    plano antes do apply (1 chamada compacta em vez de aplicar e ver itens
    rejeitados no resultado). Deterministico e somente leitura.
    """
    plan = editorial.get("media_plan") or []
    entities = extract_entities(
        title=str((editorial.get("seo") or {}).get("title") or ""),
        content_html=str(editorial.get("cleaned_html") or ""),
        focus_keyword=str((editorial.get("seo") or {}).get("focus_keyword") or ""),
        game_name=editorial.get("game_name"),
    )
    cache: dict[int, dict[str, Any]] = {}
    valid = 0
    rejected: list[dict[str, Any]] = []
    featured_vision: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    for index, item in enumerate(plan):
        if root is not None:
            append_telemetry(
                root,
                "media_funnel",
                stage="candidate",
                status="seen",
                item_index=index,
                featured=bool(item.get("is_featured")),
                post_id=post_id,
                query=str(item.get("search_query") or item.get("query") or ""),
                subject=str(item.get("subject") or ""),
                role=str(item.get("role") or ("featured" if item.get("is_featured") else "inline")),
                candidate_id=str(item.get("candidate_id") or ""),
                source_domain=(
                    urlparse(str(item.get("direct_image_url") or "")).hostname or ""
                ).lower(),
            )
        reason = _media_item_rejection(item, entities, client, cache, editorial, root=root)
        if reason is None:
            reason = _duplicate_source_reason(plan, index, seen_sources)
        if reason is None and bool(item.get("is_featured")):
            vision = _validate_featured_candidate_vision(
                item, editorial, client, cache, config=config, root=root
            )
            featured_vision.append({
                "index": index,
                "subject": featured_vision_subject(editorial),
                "category": featured_vision_category(editorial),
                "cache_hit": bool(vision.get("cached")),
                **vision,
            })
            if root is not None:
                append_telemetry(
                    root,
                    "media_funnel",
                    stage="featured_vision",
                    status=vision["status"],
                    item_index=index,
                    featured=True,
                    post_id=post_id,
                    query=str(item.get("search_query") or item.get("query") or ""),
                    subject=str(item.get("subject") or ""),
                    role="featured",
                    candidate_id=str(item.get("candidate_id") or ""),
                    source_domain=(
                        urlparse(str(item.get("direct_image_url") or "")).hostname or ""
                    ).lower(),
                    detail=str(vision.get("reason") or "")[:160],
                )
            if vision["status"] in {"rejected", "error"}:
                reason = str(vision["reason"])
        if reason:
            rejected.append({"index": index, "reason": reason})
        else:
            valid += 1
        if root is not None:
            append_telemetry(
                root,
                "media_funnel",
                stage="preflight",
                status="rejected" if reason else "passed",
                item_index=index,
                featured=bool(item.get("is_featured")),
                post_id=post_id,
                query=str(item.get("search_query") or item.get("query") or ""),
                subject=str(item.get("subject") or ""),
                role=str(item.get("role") or ("featured" if item.get("is_featured") else "inline")),
                candidate_id=str(item.get("candidate_id") or ""),
                source_domain=(
                    urlparse(str(item.get("direct_image_url") or "")).hostname or ""
                ).lower(),
                detail=str(reason or "")[:160],
            )
    if existing_featured_id and not any(bool(item.get("is_featured")) for item in plan):
        existing_item = {
            "media_library_id": existing_featured_id,
            "is_featured": True,
            "alt_text": "",
        }
        vision = _validate_featured_candidate_vision(
            existing_item, editorial, client, cache, config=config, root=root
        )
        featured_vision.append(
            {"index": None, "existing_featured_id": existing_featured_id, **vision}
        )
        if root is not None:
            attachment = cache.get(existing_featured_id) or {}
            append_telemetry(
                root,
                "media_funnel",
                stage="featured_vision",
                status=vision["status"],
                item_index=-1,
                featured=True,
                existing=True,
                post_id=post_id,
                query="",
                subject=str(editorial.get("game_name") or ""),
                role="featured",
                candidate_id="",
                source_domain=(
                    urlparse(str(attachment.get("source_url") or "")).hostname or ""
                ).lower(),
                detail=str(vision.get("reason") or "")[:160],
            )
    rejected_indexes = {row["index"] for row in rejected}
    accepted = {index for index in range(len(plan)) if index not in rejected_indexes}
    listicle = _listicle_media_capacity(
        editorial, entities, accepted, post_title=post_title
    )
    return {
        "valid": valid,
        "rejected": rejected,
        "listicle": listicle,
        "featured_vision": featured_vision,
    }


def _validate_featured_candidate_vision(
    item: dict[str, Any],
    editorial: dict[str, Any],
    client: WordPressClient,
    attachment_cache: dict[int, dict[str, Any]],
    *,
    config: Config | None,
    root: Path | None,
) -> dict[str, Any]:
    """Run the expensive featured pixel gate during ``media-validate``.

    The decision is cached before upload, so share cards, logos and unrelated
    banners are rejected while the media plan is still cheap to replace.
    """
    if config is None:
        return {"status": "skipped", "reason": "configuracao de visao nao fornecida"}
    subject = featured_vision_subject(editorial)
    cache_subject = subject
    media_id = item.get("media_library_id")
    attachment: dict[str, Any] | None = None
    if media_id:
        attachment = attachment_cache.get(media_id)
        if attachment is None:
            attachment = client.get_media(media_id)
            attachment_cache[media_id] = attachment
        image_url = str(attachment.get("source_url") or "").strip()
    else:
        image_url = str(item.get("direct_image_url") or "").strip()
    if not image_url or not subject:
        return {"status": "rejected", "reason": "featured sem URL ou assunto para validar por visao"}

    if config.vision_mode == "ambiguous" and not media_id:
        deterministic_reason = trusted_featured_evidence(
            image_url=image_url,
            source_page_url=str(item.get("source_page_url") or ""),
            subject=subject,
            search_query=str(item.get("search_query") or ""),
        )
        if deterministic_reason:
            return {
                "status": "passed",
                "reason": f"visao dispensada: {deterministic_reason}",
                "cached": False,
                "deterministic": True,
            }

    ready, message = vision_config_ready(
        enabled=config.vision_enabled, api_key=config.vision_api_key
    )
    if not ready:
        return {"status": "skipped", "reason": message}

    cache_root = root or Path(".")
    cached = get_cached_decision(cache_root, image_url, cache_subject)
    if cached is not None:
        ok = cached.get("status") == "MATCH" and float(cached.get("confidence") or 0) >= 0.85
        return {
            "status": "passed" if ok else "rejected",
            "reason": "decisao visual reutilizada do cache",
            "cached": True,
        }
    try:
        ok, reason = verify_image_subject(
            image_url=image_url,
            subject=subject,
            api_key=config.vision_api_key,
            base_url=config.vision_base_url,
            model=config.vision_model,
            timeout=config.http_timeout,
            context="preflight da imagem de destaque do artigo",
            category=featured_vision_category(editorial),
            alt=str(item.get("alt_text") or subject),
            detail=config.vision_detail,
            allow_high=True,
            require_key_art=True,
            root=root,  # uma requisicao HTTP = um evento vision_api_request
        )
    except VisionGateError as exc:
        return {
            "status": "error",
            "technical": True,
            "reason": f"visao da featured falhou: {exc}",
            "cached": False,
        }
    except Exception as exc:  # noqa: BLE001 - fail closed
        return {
            "status": "error",
            "technical": True,
            "reason": f"visao da featured falhou: {exc}",
            "cached": False,
        }
    if ok:
        set_cached_decision(
            cache_root,
            image_url,
            cache_subject,
            {"status": "MATCH", "confidence": 1.0, "visual_type": "other"},
        )
    return {"status": "passed" if ok else "rejected", "reason": reason, "cached": False}


def _listicle_media_capacity(
    editorial: dict[str, Any],
    entities: set[str],
    accepted_plan_indexes: set[int] | None = None,
    *,
    post_title: str = "",
) -> dict[str, Any]:
    """Return the verified inline-image capacity before listicle authoring.

    A listicle may promise only as many items as it can cover with distinct,
    relevant inline images. Featured media is deliberately excluded.
    """
    html = str(editorial.get("cleaned_html") or "")
    title = post_title or str((editorial.get("seo") or {}).get("title") or "")
    promised = detect_list_format(title, html)
    if promised is None:
        return {"applicable": False}

    sources: set[str] = set()
    for image in iter_content_images(html):
        source = str(image.get("src") or "").strip()
        if source and image_is_relevant(
            alt_text=str(image.get("alt") or ""),
            credit_text=str(image.get("caption") or ""),
            source_url=source,
            entities=entities,
        ):
            sources.add(f"content:{source}")

    plan = editorial.get("media_plan") or []
    for index, item in enumerate(plan):
        if bool(item.get("is_featured")):
            continue
        if accepted_plan_indexes is not None and index not in accepted_plan_indexes:
            continue
        key = _plan_source_key(item)
        if key and not key.endswith(":"):
            sources.add(f"plan:{key}")

    available = len(sources)
    return {
        "applicable": True,
        "promised_items": promised,
        "verified_inline_capacity": available,
        "missing": max(0, promised - available),
        "feasible": available >= promised,
        "featured_counted": False,
    }


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
    # P0 (auditoria): se o conteúdo JÁ é um envelope operacional (o acidente do
    # post 114180 publicou o JSON do comando), desembrulha o HTML real antes de
    # limpar — e registra, porque significa que algo gravou saída de CLI no corpo.
    from .content_quality import looks_like_operational_envelope, unwrap_operational_envelope

    if looks_like_operational_envelope(raw):
        try:
            from .observability import append_telemetry as _telemetria

            _telemetria(root, "content_envelope_unwrapped", post_id=post_id)
        except Exception:  # noqa: BLE001
            pass
        raw = unwrap_operational_envelope(raw)
    cleaned = clean_html(_repair_orphan_media(raw), post_title=_post_title(post) or "")
    return {
        "post_id": post_id,
        "status": post["status"],
        "cleaned_html": cleaned,
        "original_link": _original_link(post),
        "word_count": word_count(cleaned),
    }


def _is_final_phash_duplicate(final_phash: str, baseline_phashes: list[str] | tuple[str, ...], *, threshold: int = 6) -> bool:
    """Return whether a final WebP pHash repeats an accepted frame."""
    if not final_phash:
        return False
    try:
        import imagehash
        candidate = imagehash.hex_to_hash(str(final_phash))
        return any(
            int(candidate - imagehash.hex_to_hash(str(previous))) <= threshold
            for previous in baseline_phashes
            if str(previous).strip()
        )
    except (ImportError, TypeError, ValueError):
        return False


def _execute_media_plan(
    editorial: dict[str, Any],
    config: Config,
    client: WordPressClient,
    root: Path,
    *,
    preflight: dict[str, Any] | None = None,
    post_id: int | None = None,
    previous_inline_phashes: list[str] | tuple[str, ...] = (),
    previous_inline_visual_assets: list[dict[str, Any]] | None = None,
    featured_visual_asset: dict[str, Any] | None = None,
    visual_comparison_budget: list[int] | None = None,
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
    planning = (preflight or {}).get("listicle") or {}
    if planning.get("applicable") and not planning.get("feasible"):
        detail = (
            f"planejamento media-first: listicle promete {planning.get('promised_items')} itens, "
            f"mas possui capacidade validada para {planning.get('verified_inline_capacity')} "
            "imagem(ns) inline distinta(s); reduza itens ou encontre uma imagem por item"
        )
        return [
            {
                "paragraph_index": item.get("paragraph_index"),
                "status": "rejected",
                "detail": detail,
            }
            for item in plan
        ], None, None
    entities = extract_entities(
        title=str((editorial.get("seo") or {}).get("title") or ""),
        content_html=str(editorial.get("cleaned_html") or ""),
        focus_keyword=str((editorial.get("seo") or {}).get("focus_keyword") or ""),
        game_name=editorial.get("game_name"),
    )
    # P1 (auditoria de contexto): o reuso da Media Library e casado por SUBJECT.
    # Quando o media_plan nao declara `subject`, usa o MESMO subject que o
    # `media-search-web` deriva do post (entidade principal do titulo) — antes a
    # entrada ia para o indice com subject vazio e o reuso nunca achava nada.
    subject_idx = ""
    try:
        from .media.evidence import post_subjects

        _subs = post_subjects(
            title=str((editorial.get("seo") or {}).get("title") or ""),
            content_html=str(editorial.get("cleaned_html") or ""),
            focus_keyword=str((editorial.get("seo") or {}).get("focus_keyword") or ""),
            game_name=editorial.get("game_name"),
        )
        if _subs:
            subject_idx = str(_subs[0].get("subject") or "")
    except Exception:  # noqa: BLE001 - registro do indice e otimizacao
        subject_idx = ""

    attachment_cache: dict[int, dict[str, Any]] = {}

    def _funnel(
        stage: str,
        status: str,
        item: dict[str, Any],
        position: int,
        detail: str = "",
    ) -> None:
        source = str(item.get("direct_image_url") or "")
        append_telemetry(
            root,
            "media_funnel",
            stage=stage,
            status=status,
            item_index=position,
            featured=bool(item.get("is_featured")),
            post_id=post_id,
            query=str(item.get("search_query") or item.get("query") or ""),
            subject=str(item.get("subject") or ""),
            role=str(item.get("role") or ("featured" if item.get("is_featured") else "inline")),
            candidate_id=str(item.get("candidate_id") or ""),
            source_domain=(urlparse(source).hostname or "").lower(),
            detail=detail[:160],
        )
        if stage == "source_verify":
            normalized_detail = detail.casefold()
            if status == "passed":
                verification = "source_verified"
            elif any(token in normalized_detail for token in ("pagina de origem", "source_page", "fonte instavel")):
                verification = "missing_source_page"
            elif any(token in normalized_detail for token in ("divergente", "mismatch", "bytes diferentes")):
                verification = "source_mismatch"
            else:
                verification = "source_rejected"
            append_telemetry(
                root,
                "source_verification",
                verification=verification,
                item_index=position,
                post_id=post_id,
                query=str(item.get("search_query") or item.get("query") or ""),
                subject=str(item.get("subject") or ""),
                role=str(item.get("role") or ("featured" if item.get("is_featured") else "inline")),
                candidate_id=str(item.get("candidate_id") or ""),
                source_domain=(urlparse(source).hostname or "").lower(),
                detail=detail[:160],
            )

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
        return _media_item_rejection(item, entities, client, attachment_cache, editorial, root=root)

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
    # Compartilhado entre threads; o lock elimina buscas duplicadas da mesma
    # pagina de origem e conserva a verificacao byte-a-byte por imagem.
    page_cache: dict[str, list[str] | None] = {}
    page_cache_lock = Lock()
    seen_sources: set[str] = set()
    accepted_final_phashes = {str(value) for value in previous_inline_phashes if str(value).strip()}
    accepted_final_phashes_lock = Lock()
    # Pre-passe SERIAL: rejeicao (relevancia/reuso) + deteccao de duplicatas.
    # Duplicata depende da ORDEM (primeira ocorrencia vence) e o
    # attachment_cache e populado aqui (get_media), por isso fica fora do
    # paralelismo.
    pending: list[tuple[int, dict[str, Any]]] = []
    preflight_rejections = {
        int(row["index"]): str(row.get("reason") or "media-validate rejeitou o item")
        for row in ((preflight or {}).get("rejected") or [])
        if isinstance(row, dict) and isinstance(row.get("index"), int)
    }
    preflight_vision = {
        int(row["index"]): row
        for row in ((preflight or {}).get("featured_vision") or [])
        if isinstance(row, dict) and isinstance(row.get("index"), int)
    }
    for position, item in enumerate(plan):
        reason = preflight_rejections.get(position) or _rejection_reason(item)
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
            # Materialise existing accepted media once.  The dictionaries are
            # intentionally updated in place so the caller can persist legacy
            # baseline fingerprints in the V2 state after this run.
            visual_baseline: list[dict[str, Any]] = []
            visual_baseline_unavailable = False
            visual_comparison_budget = visual_comparison_budget if visual_comparison_budget is not None else [0]
            for asset in [*(previous_inline_visual_assets or []), *([featured_visual_asset] if featured_visual_asset else [])]:
                if not isinstance(asset, dict):
                    continue
                row = asset
                # A missing/invalid featured is not a visual baseline.  It
                # must not make the first legitimate acquisition fail closed.
                if not row.get("media_id") or not str(row.get("media_url") or "").strip():
                    continue
                try:
                    if not row.get("sha256") or not row.get("phash"):
                        url = str(row.get("media_url") or "")
                        if not url:
                            raise ValueError("missing media URL")
                        baseline_file = download_image(url, tmp / f"baseline_{row.get('media_id') or len(visual_baseline)}.img", url_policy=config.remote_url_policy)
                        from .media.visual_identity import fingerprint_path
                        identity = fingerprint_path(baseline_file)
                        row.update(identity.to_dict())
                        row["local_path"] = str(baseline_file)
                    elif row.get("local_path") and Path(str(row["local_path"])).is_file():
                        pass
                    else:
                        baseline_file = download_image(str(row.get("media_url") or ""), tmp / f"baseline_{row.get('media_id') or len(visual_baseline)}.img", url_policy=config.remote_url_policy)
                        row["local_path"] = str(baseline_file)
                    visual_baseline.append(row)
                    if row.get("phash"):
                        accepted_final_phashes.add(str(row["phash"]))
                except Exception as exc:  # fail closed for an identity baseline
                    visual_baseline_unavailable = True
                    append_telemetry(root, "visual_identity_baseline_unavailable", post_id=post_id, media_id=row.get("media_id"), error=type(exc).__name__)

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
                browser_path = Path(str(item.get("local_image_path") or ""))
                if browser_path.is_file() and browser_path.stat().st_size <= 8 * 1024 * 1024:
                    source = tmp / f"source_{position}{suffix}"
                    shutil.copyfile(browser_path, source)
                    _funnel("download", "passed", item, position, "google_browser_local_bytes")
                else:
                    source = download_image(
                        str(download_url),
                        tmp / f"source_{position}{suffix}",
                        max_attempts=config.max_source_retries + 1,
                        url_policy=config.remote_url_policy,
                        audit=lambda finding: append_telemetry(
                            root, "remote_url_audit", url=finding.url, reason=finding.reason
                        ),
                    )
                    _funnel("download", "passed", item, position)
                # Verificacao de conteudo: a imagem baixada deve estar listada
                # na pagina de origem (fail-closed).
                if (
                    str(item.get("origin_type") or item.get("source_origin_type") or "")
                    == "article_source"
                    and bool(item.get("provenance_verified") or item.get("source_verified"))
                ):
                    # The candidate was extracted from this exact article
                    # document. Re-fetching the source page here reintroduces
                    # the old resolver gate (and can fail on a CDN/hotlink),
                    # while the remaining download, format, relevance,
                    # dimension, WebP and pHash gates still run normally.
                    ok, verify_reason = True, "article_source_extracted"
                else:
                    ok, verify_reason = verify_downloaded_against_source(
                        source_page_url=str(item.get("source_page_url") or ""),
                        downloaded=source,
                        direct_image_url=str(download_url),
                        cache=page_cache,
                        cache_lock=page_cache_lock,
                        audit=lambda finding: append_telemetry(
                            root, "remote_url_audit", url=finding.url, reason=finding.reason
                        ),
                    )
                if not ok:
                    _funnel("source_verify", "rejected", item, position, verify_reason)
                    return position, {
                        "paragraph_index": item.get("paragraph_index"),
                        "status": "rejected",
                        "detail": f"verificacao de origem: {verify_reason}",
                    }
                _funnel("source_verify", "passed", item, position, verify_reason)
                is_featured = bool(item.get("is_featured"))
                transparency = "flattened" if image_has_transparency(source) else "none"
                if is_featured:
                    webp = prepare_featured_webp(source, tmp / f"featured_{position}.webp")
                else:
                    webp = convert_to_webp(source, tmp / f"inline_{position}.webp")
                from .media.visual_hash import phash_from_path

                final_phash = phash_from_path(str(webp)) or str(item.get("phash") or "")
                # Featured "so texto" nao e key art: rejeita.
                if is_featured and image_is_mostly_flat(webp):
                    _funnel("conversion", "rejected", item, position, "featured plana")
                    return position, {
                        "paragraph_index": item.get("paragraph_index"),
                        "status": "rejected",
                        "detail": "featured aparenta ser so texto/arte plana sem conteudo visual; "
                        "escolha uma key art/imagem real da obra",
                    }
                width, height = image_dimensions(webp)
                _funnel("conversion", "passed", item, position, f"{width}x{height}")
                with accepted_final_phashes_lock:
                    if _is_final_phash_duplicate(final_phash, tuple(accepted_final_phashes)):
                        _funnel("preflight", "rejected", item, position, "duplicate_existing_frame")
                        return position, {
                            "paragraph_index": item.get("paragraph_index"),
                            "status": "rejected",
                            "detail": "duplicate_existing_frame",
                        }
                    from .media.visual_identity import VisualIdentity, VisualIdentityDecision, verify_candidate_identity
                    try:
                        identity, visual = verify_candidate_identity(
                            webp, candidate_id=str(item.get("candidate_id") or position),
                            baseline=visual_baseline, config=config, root=root,
                            comparison_budget=visual_comparison_budget,
                        )
                    except Exception as exc:
                        # A first asset has no duplicate baseline.  Keep the
                        # historical upload contract resilient to a transient
                        # local fingerprinting failure, but never use that
                        # escape when a baseline needs comparison.
                        if visual_baseline or visual_baseline_unavailable:
                            return position, {"paragraph_index": item.get("paragraph_index"), "status": "rejected", "detail": f"visual_identity_unverified:{type(exc).__name__}"}
                        identity = VisualIdentity("", str(final_phash or ""), "")
                        visual = VisualIdentityDecision("INITIAL", True, reason="initial_identity_pending")
                    if visual_baseline_unavailable:
                        visual = type(visual)("UNVERIFIED", False, reason="baseline_identity_unavailable")
                    if not visual.verified:
                        _funnel("visual_identity", "rejected", item, position, visual.reason)
                        return position, {"paragraph_index": item.get("paragraph_index"), "status": "rejected", "detail": f"visual_identity_unverified:{visual.reason}"}
                    if visual.decision in {"SAME_IMAGE", "SAME_ART_CROP"}:
                        _funnel("visual_identity", "rejected", item, position, visual.reason)
                        return position, {"paragraph_index": item.get("paragraph_index"), "status": "rejected", "detail": "duplicate_existing_frame"}
                media = upload_image(client, webp, evidence)
                media_id = media.get("id")
                media_url = media.get("source_url")
                if not media_id or not media_url:
                    raise WorkflowError(f"media upload returned no id/source_url (item {position})")
                # Fase 13: persiste o fingerprint/proveniencia da midia. Assim a
                # proxima busca do mesmo subject (ou do mesmo frame recomprimido)
                # reusa a imagem em vez de baixar e subir de novo.
                from .media.library_index import register as _registrar_midia

                # Erro de PROGRAMAÇÃO aqui tem de estourar (o swallow total
                # escondia um NameError e o índice simplesmente nunca era
                # gravado); só falha de I/O é tolerada, e mesmo assim fica
                # registrada.
                try:
                    _registrar_midia(
                        root,
                        phash=str(final_phash or ""),
                        source_url=str(item.get("direct_image_url") or ""),
                        source_page=str(item.get("source_page_url") or ""),
                        subject=str(item.get("subject") or "") or subject_idx,
                        media_id=int(media_id),
                        article_id=int(post_id) if post_id else None,
                        sha256=identity.sha256,
                        visual_group_id=identity.visual_group_id,
                    )
                except (OSError, ValueError) as exc:
                    try:
                        from .observability import append_telemetry

                        append_telemetry(root, "media_index_write_failed",
                                         post_id=post_id, error=str(exc)[:200])
                    except Exception:  # noqa: BLE001
                        pass
                # A visão cara já ocorreu no media-validate. Transfere a
                # aprovação para a URL hospedada no WordPress; o checklist
                # final continua fail-closed, mas consome o cache em vez de
                # criar uma segunda chamada tardia.
                vision = preflight_vision.get(position)
                if is_featured and vision and vision.get("status") == "passed":
                    subject = vision_cache_subject(editorial)
                    set_cached_decision(
                        root,
                        str(media_url),
                        subject,
                        {"status": "MATCH", "confidence": 1.0, "visual_type": "other"},
                    )
                _funnel("upload", "passed", item, position)
                visual_record = {"decision": visual.decision, "reason": visual.reason, "comparisons": list(visual.comparisons)}
                if identity.sha256:
                    visual_baseline.append({**identity.to_dict(), "media_id": int(media_id), "media_url": str(media_url), "local_path": str(webp)})
                if final_phash:
                    accepted_final_phashes.add(final_phash)
                return position, {
                    "paragraph_index": item["paragraph_index"],
                    "media_id": media_id,
                    "media_url": media_url,
                    "alt_text": item["alt_text"],
                    "credit_text": item["credit_text"],
                    "subject": item.get("subject"),
                    "item_number": item.get("item_number"),
                    "section_heading": item.get("section_heading"),
                    "section_slot": item.get("section_slot"),
                    "featured": is_featured,
                    "width": width,
                    "height": height,
                    "transparency": transparency,
                    "phash": final_phash,
                    "sha256": identity.sha256,
                    "visual_group_id": identity.visual_group_id,
                    "visual_verification": visual_record,
                }

            # Visual identity has a mutable accepted baseline.  Process serially:
            # this is stronger than an in-memory reservation and prevents two
            # workers accepting the same artwork before either upload completes.
            # Fase paralela (I/O-bound). Somente falha ao CRIAR o executor cai
            # para serial: nessa altura ainda não há download nem upload. Uma
            # falha dentro de worker vira rejeição daquele item; reexecutar o
            # lote inteiro duplicaria anexos que já foram enviados.
            processed = []
            for pair in pending:
                position, item = pair
                try:
                    processed.append(_process_item(pair))
                except Exception as exc:  # noqa: BLE001 - report one failed item
                    _funnel("worker", "error", item, position, str(exc))
                    processed.append((position, {"paragraph_index": item.get("paragraph_index"), "status": "error", "detail": f"processamento de midia: {str(exc)[:160]}"}))

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
    # Resumo por post da etapa que realmente baixa/converte/envia. O resumo de
    # discovery é emitido separadamente pelo resolver; manter os dois eventos
    # evita confundir candidato aprovado com mídia efetivamente aceita.
    accepted_results = [
        result for result in results
        if result.get("status") in {"accepted", "ok"} and result.get("media_id")
    ]
    append_telemetry(
        root,
        "media_post_summary",
        post_id=post_id,
        required=len(plan),
        found=len(plan),
        with_source=sum(bool(str(item.get("source_page_url") or "").strip()) for item in plan),
        source_verified=sum(result.get("status") not in {"rejected", "error"} for result in results),
        source_mismatch=sum("mismatch" in str(result.get("detail") or "").casefold() for result in results),
        missing_source_page=sum("origem" in str(result.get("detail") or "").casefold() for result in results if result.get("status") == "rejected"),
        relevance_match=sum(bool((item.get("evidence") or {}).get("verdict") == "deterministic_match") for item in plan),
        relevance_ambiguous=sum(bool((item.get("evidence") or {}).get("verdict") == "ambiguous") for item in plan),
        relevance_reject=sum(bool(result.get("status") == "rejected") for result in results),
        vision_approved=sum(bool(row.get("status") == "passed") for row in preflight_vision.values()),
        vision_rejected=sum(bool(row.get("status") == "rejected") for row in preflight_vision.values()),
        vision_input_unavailable=sum(bool(row.get("status") == "unavailable") for row in preflight_vision.values()),
        duplicate_frame=sum("duplicate" in str(result.get("detail") or "").casefold() for result in results),
        distinct=len({str(result.get("phash") or result.get("media_id") or "") for result in accepted_results}),
        downloaded=sum(result.get("status") not in {"error"} for result in results),
        converted=sum(bool(result.get("width") and result.get("height")) for result in results),
        uploaded=sum(bool(result.get("media_id")) for result in results),
        accepted=len(accepted_results),
    )
    return results, featured_id, featured_credit



def _normalize_existing_featured(
    client: WordPressClient,
    config: Config,
    post: dict[str, Any],
    editorial: dict[str, Any] | None = None,
    *,
    root: Path | None = None,
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
                url_policy=config.remote_url_policy,
                audit=(
                    lambda finding: append_telemetry(
                        root, "remote_url_audit", url=finding.url, reason=finding.reason
                    )
                    if root is not None
                    else None
                ),
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
        resolved["cleaned_html"] = clean_html(_repair_orphan_media(_raw_content(post)), post_title=_post_title(post) or "")
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
    body = clean_html(_raw_content(post), post_title=_post_title(post) or "")
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
        # Preserva campos extras do editorial (ex.: discarded/discarded_at da
        # triagem): antes tudo fora de post_id/status/site_relevance era
        # silenciosamente descartado na gravacao.
        extra = {
            k: v
            for k, v in editorial.items()
            if k not in ("post_id", "status", "site_relevance")
        }
        atomic_write_text(directory / "uncertain.json", json.dumps(
                {
                    "post_id": post_id,
                    "status": "uncertain",
                    "site_relevance": editorial.get("site_relevance"),
                    **extra,
                },
                ensure_ascii=False,
                indent=2,
            ))
    except OSError:
        pass


def _save_editorial_latest(root: Path, post_id: int, editorial: dict[str, Any]) -> None:
    """Persist the validated editorial so the publish flow can re-check it."""
    try:
        directory = root / "backups" / str(post_id)
        directory.mkdir(parents=True, exist_ok=True)
        atomic_write_text(directory / "editorial.latest.json", json.dumps(editorial, ensure_ascii=False, indent=2))
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
        atomic_write_text(directory / "editorial.blocked.json", json.dumps(
                {**editorial, "blocked_checklist": checklist},
                ensure_ascii=False,
                indent=2,
            ))
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
        atomic_write_text(directory / "editorial.blocked.json", json.dumps(
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
            ))
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


def _read_v2_work_state(post: dict[str, Any]) -> dict[str, Any] | None:
    meta = post.get("meta")
    if not isinstance(meta, dict):
        meta = {}
    raw = meta.get("_hermes_work_state")
    if raw is None:
        return None
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"state": "invalid"}
    return value if isinstance(value, dict) else {"state": "invalid"}


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
    v2_state = _read_v2_work_state(post)
    if v2_state is not None:
        if v2_state.get("state") != "ready":
            return {"post_id": post_id, "wordpress_changed": False, "status": "skipped", "reason": f"estado V2 {v2_state.get('state')} fora da fila de publicacao", "state": v2_state.get("state")}
        state_info = {"state": STATE_READY, "ready_hash": str((post.get("meta") or {}).get("_hermes_ready_hash") or "")}
    else:
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
    # P0 (auditoria): invariância no NÍVEL MAIS BAIXO — nenhum caminho de
    # publicação aceita envelope operacional. O sanity existia só no loop do
    # cron (publish_ready_posts); o fast-path `publish POST_ID` chama esta
    # função e ia direto ao manifest_match — era por ali que o 114180 passava.
    # E não basta pular: o post vira BLOCKED com motivo visível, senão o READY
    # volta a cada janela e é bloqueado em silêncio para sempre.
    from .content_quality import looks_like_operational_envelope

    if looks_like_operational_envelope(str(_raw_content(post) or "")):
        aviso = (
            "corpo é saída de comando do CLI (envelope JSON operacional) — "
            "o post precisa de conteúdo editorial real antes de publicar"
        )
        try:
            _write_state_markers(
                client, config, post_id, STATE_BLOCKED, root=root,
                last_error="operational_envelope_in_content",
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            from .observability import append_telemetry as _telemetria

            _telemetria(root, "publish_blocked_operational_envelope", post_id=post_id)
        except Exception:  # noqa: BLE001
            pass
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "status": "blocked",
            "reason": aviso,
            "state": STATE_BLOCKED,
        }
    post_meta = post.get("meta") if isinstance(post.get("meta"), dict) else {}
    language_report = editorial_language_report(
        title=str(_post_title(post) or ""),
        content=str(_raw_content(post) or ""),
        seo_title=str(post_meta.get("rank_math_title") or ""),
        meta_description=str(post_meta.get("rank_math_description") or ""),
    )
    if not language_report["passed"]:
        reason = (
            "idioma_pt_br: campos predominantemente em inglês: "
            + ", ".join(language_report.get("failing_fields") or [])
        )
        try:
            _write_state_markers(client, config, post_id, STATE_BLOCKED, root=root, last_error=reason)
        except Exception:  # noqa: BLE001
            pass
        try:
            from .observability import append_telemetry as _telemetria

            _telemetria(
                root,
                "publish_blocked_language",
                post_id=post_id,
                language=language_report.get("language"),
                confidence=language_report.get("confidence"),
                failing_fields=language_report.get("failing_fields") or [],
            )
        except Exception:  # noqa: BLE001
            pass
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "status": "blocked",
            "reason": reason,
            "state": STATE_BLOCKED,
            "language": language_report,
        }
    if state == STATE_READY:
        raw_meta = post.get("meta")
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        stored = parse_manifest(meta.get(META_READY_MANIFEST))
        if manifest_matches(post, stored, state_info["ready_hash"], policy_version=config.policy_version):
            return _publish_now(
                client,
                config,
                post_id,
                root=root,
                integrity="manifest_match",
                ready_hash=state_info["ready_hash"],
                ready_manifest=meta.get(META_READY_MANIFEST)
                if isinstance(meta.get(META_READY_MANIFEST), str)
                else None,
            )
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
            root=root,
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
    refreshed_manifest = build_ready_manifest(
        post_id=post_id,
        content=_raw_content(post),
        featured_media=post.get("featured_media") if isinstance(post.get("featured_media"), int) else None,
        seo=editorial["seo"],
        original_link=original_link_of(post),
        editorial=editorial,
        policy_version=config.policy_version,
    )
    return _publish_now(
        client,
        config,
        post_id,
        root=root,
        integrity="revalidated",
        ready_hash=manifest_hash(refreshed_manifest),
        ready_manifest=serialize_manifest(refreshed_manifest),
    )


def _publish_now(
    client: WordPressClient,
    config: Config,
    post_id: int,
    *,
    root: Path | None = None,
    integrity: str,
    ready_hash: str = "",
    ready_manifest: str | None = None,
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
    publish_meta = {
        **build_state_markers(
            STATE_PUBLISHED,
            policy_version=config.policy_version,
            ready_hash=ready_hash,
        ),
    }
    if ready_manifest is not None:
        publish_meta[META_READY_MANIFEST] = ready_manifest
    result = client.publish(
        post_id,
        meta={"_ai_editor_published_at": published_at, **publish_meta},
        # Política do dono: post antigo em pending publica como data corrente
        # (não fica enterrado no passado do site).
        date_gmt=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
    )
    # Keep the V2 lifecycle synchronized with the existing publisher. This is
    # a second, explicit state write; it never changes content or status.
    from dataclasses import replace as _replace
    from .pipeline_v2.model import LifecycleState as _LifecycleState, Phase as _Phase
    from .pipeline_v2.operational import WordPressStateBackend as _V2Backend
    from .pipeline_v2.state_store import StateStore as _V2Store
    try:
        v2_post = client.get_post(post_id)
        if v2_post.get("status") != "publish":
            raise WorkflowError("WordPress publish read-back mismatch")
        if read_state(v2_post).get("state") != STATE_PUBLISHED:
            raise WorkflowError("legacy publish read-back mismatch")
        v2_meta = v2_post.get("meta", {}) if isinstance(v2_post, dict) else {}
        if "_hermes_work_state" in v2_meta:
            v2_store = _V2Store(_V2Backend(client))
            v2_current = v2_store.load(post_id)
            v2_store.mark_published(post_id, _replace(v2_current, state=_LifecycleState.PUBLISHED, phase=_Phase.PUBLISH, blocker=None))
            v2_verified = v2_store.load(post_id)
            if v2_verified.state is not _LifecycleState.PUBLISHED or v2_verified.phase is not _Phase.PUBLISH:
                raise WorkflowError("V2 state read-back mismatch after publish")
        verified_post = client.get_post(post_id)
        verified_meta = verified_post.get("meta", {}) if isinstance(verified_post, dict) else {}
        if verified_post.get("status") != "publish" or read_state(verified_post).get("state") != STATE_PUBLISHED:
            raise WorkflowError("publication read-back mismatch")
        if ready_hash and verified_meta.get(META_READY_HASH) != ready_hash:
            raise WorkflowError("ready hash read-back mismatch after publish")
        if ready_manifest is not None and verified_meta.get(META_READY_MANIFEST) != ready_manifest:
            raise WorkflowError("ready manifest read-back mismatch after publish")
    except Exception as exc:
        try:
            append_telemetry(
                root or Path("."),
                "publish_v2_sync_failed",
                post_id=post_id,
                error=str(exc)[:200],
            )
        except Exception:  # noqa: BLE001 - telemetry must not hide the root failure
            pass
        raise
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
        v2_state = _read_v2_work_state(candidate)
        if v2_state is not None:
            if v2_state.get("state") != "ready":
                continue
            state_info = {"state": STATE_READY, "ready_hash": str((candidate.get("meta") or {}).get("_hermes_ready_hash") or "")}
        else:
            state_info = read_state(candidate)
        if state_info["state"] not in (None, STATE_READY):
            continue  # fora da fila de publicacao — sem chamadas caras
        if state_info["state"] is None:
            # P3.7: o modo legado (post sem `_hermes_state`) continua elegivel
            # por seguranca operacional, mas nunca em silencio — cada uso fica
            # registrado para que a migracao explicita seja concluida e o gate
            # passe a exigir SOMENTE STATE_READY.
            try:
                append_telemetry(
                    root, "legacy_state_used",
                    post_id=candidate.get("id"), bucket="publish_ready",
                )
            except Exception:  # noqa: BLE001 - telemetria nunca bloqueia
                pass
        if state_info["state"] is None and (
            root / "backups" / str(candidate_id) / "editorial.blocked.json"
        ).is_file():
            # Legado ja sinalizado como rework: nao revalida a cada janela —
            # o agente corrige (re-apply) e o estado vira READY.
            continue
        # P0 (auditoria): sanity ANTES do fast-path. O caminho "hash intacto ->
        # publica sem revalidar" confiava no manifesto; se o corpo gravado for
        # um envelope operacional (o acidente do post 114180), o manifesto não
        # percebe. Aqui é barato: uma leitura do conteúdo já carregado.
        # P0 (auditoria): o sanity do envelope mora no NÍVEL MAIS BAIXO
        # (_publish_post_unlocked), que é chamado pelo publish_post logo abaixo
        # E pelo fast-path `publish POST_ID`. Antes havia um `continue` aqui:
        # não publicava, mas também não marcava BLOCKED, não gerava outcome e
        # não deixava motivo na resposta — o READY voltava a cada janela e era
        # bloqueado em silêncio para sempre. Agora o próprio publish_post
        # devolve status=blocked, grava o estado e registra a telemetria.
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
        if outcome.get("status") == "published" and outcome.get("wordpress_changed"):
            # Base durável do relatório: custo e publicações usam a mesma
            # janela móvel de 24h, sem inferir o denominador da janela atual.
            append_telemetry(root, "post_published", post_id=candidate_id)
    return outcomes


def _discover_trailer(
    editorial: dict[str, Any], config: Config, *, root: Path | None = None
) -> tuple[dict[str, str] | None, str]:
    """Discover a YouTube trailer for game content; fail-closed to None."""
    game_name = editorial.get("game_name")
    if not isinstance(game_name, str) or not game_name.strip():
        return None, "not_applicable"
    try:
        return find_cached_game_trailer_with_status(
            game_name, root=root, timeout=config.http_timeout,
            discover=find_game_trailer_with_status,
        )
    except TrailerError:
        return None, "search_failed"


def attach_trailer_audit(
    editorial: dict[str, Any],
    trailer: dict[str, str] | None,
    *,
    search_status: str = "official_not_found",
) -> dict[str, Any]:
    """Attach code-generated evidence when no official trailer is found."""
    result = dict(editorial)
    game_name = editorial.get("game_name")
    if (
        isinstance(game_name, str)
        and game_name.strip()
        and trailer is None
        and search_status == "official_not_found"
    ):
        result["trailer_unavailable"] = True
        result["trailer_search_evidence"] = {
            "query": f"{game_name.strip()} trailer",
            "provider": "youtube",
            "searched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "result": "official_not_found",
        }
    else:
        result.pop("trailer_unavailable", None)
        result.pop("trailer_search_evidence", None)
    return result


def compose_final_content(
    editorial: dict[str, Any],
    config: Config,
    original_link: str | None,
    *,
    root: Path | None = None,
) -> tuple[str, dict[str, str] | None, str]:
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
    trailer, trailer_status = _discover_trailer(editorial, config, root=root)
    if trailer is not None:
        html = html.rstrip() + "\n\n" + build_trailer_html(trailer)
    return append_canonical_footer(html, original_link), trailer, trailer_status


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
        return sanitize_title(title["raw"]) or None
    if isinstance(title, str):
        return sanitize_title(title) or None
    return None



def migrate_legacy_state(
    client: Any,
    config: Any,
    root: Path,
    *,
    apply: bool = False,
    limit: int = 0,
) -> dict[str, Any]:
    """Migration EXPLÍCITA dos posts legados (P3.7).

    O ``publish-ready`` aceitava ``state is None`` para não travar a fila antiga.
    Com a máquina de estados madura, o modo legado sai do caminho: esta função
    percorre os posts do WordPress e grava o ``_hermes_state`` que falta —
    ``PUBLISHED`` para quem já está publicado, ``NEW`` para quem está pendente —
    deixando o ``publish-ready`` apto a exigir SOMENTE ``STATE_READY``.

    Só roda com ``apply=True`` (dry-run por padrão) e nunca sobrescreve um estado
    existente.
    """
    # Mapa explícito WP -> estado (o awaiting_human NÃO pode virar NEW: o
    # próprio reconcile apontaria a divergência depois).
    mapa = {
        "publish": STATE_PUBLISHED,
        "pending": STATE_NEW,
        "awaiting_human": STATE_AWAITING_HUMAN,
        "draft": STATE_NEW,
    }
    vistos = 0
    migrados: list[dict[str, Any]] = []
    aplicados = 0
    falhas = 0
    erros: list[str] = []
    for status_wp in ("publish", "pending", "draft", "awaiting_human"):
        pagina = 1
        while True:
            if limit and vistos >= limit:
                break
            try:
                posts = client.list_pending(status=status_wp, per_page=100, page=pagina) or []
            except Exception as exc:  # noqa: BLE001
                # Não engolir: "0 legados" por falha de conexão seria lido como
                # "nada a migrar" e a migração seria dada por concluída.
                erros.append(f"{status_wp} p{pagina}: {type(exc).__name__}: {exc}")
                break
            if not posts:
                break
            for post in posts:
                if limit and vistos >= limit:
                    break
                post_id = post.get("id")
                if not isinstance(post_id, int):
                    continue
                vistos += 1
                info = read_state(post)
                if info.get("state") is not None:
                    continue  # já tem estado: nunca sobrescreve
                destino = mapa[status_wp]
                registro = {"post_id": post_id, "wp_status": status_wp, "state": destino}
                if apply:
                    gravou = _write_state_markers(
                        client, config, post_id, destino, root=root,
                        attempts=int(info.get("attempts") or 0),
                    )
                    registro["written"] = bool(gravou)
                    if gravou:
                        aplicados += 1
                    else:
                        falhas += 1
                        registro["write_error"] = "estado nao persistiu no WordPress"
                migrados.append(registro)
            if len(posts) < 100:
                break  # última página
            pagina += 1
            if pagina > 200:
                break  # limite de segurança
        if limit and vistos >= limit:
            break
    return {
        "scanned": vistos,
        "legacy_found": len(migrados),
        "applied": bool(apply),
        "applied_count": aplicados,
        "failed_count": falhas,
        # Só se pode dizer "migração concluída" quando tudo foi gravado.
        "complete": bool(apply) and falhas == 0 and not erros,
        "items": migrados[:50],
        "errors": erros,
    }

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
    partial_ids: list[int] = []
    recent_blocked: list[int] = []
    eligible_rework: list[int] = []
    v2_eligible: list[int] = []
    ready_ids: list[int] = []
    awaiting_human_ids: list[int] = []
    uncertain_ids: list[int] = []
    uncertain_second_pass_ids: list[int] = []
    skipped_ids: list[int] = []
    for post in posts:
        post_id = post.get("id")
        if not isinstance(post_id, int):
            continue
        backups_dir = root / "backups" / str(post_id)
        state_info = read_state(post)
        state = state_info["state"]
        v2_state = None
        raw_v2_state = (post.get("meta") or {}).get("_hermes_work_state")
        if raw_v2_state:
            try:
                from .pipeline_v2.model import WorkState

                decoded_v2 = json.loads(raw_v2_state) if isinstance(raw_v2_state, str) else raw_v2_state
                if isinstance(decoded_v2, dict) and decoded_v2.get("version") == 2:
                    v2_state = WorkState.from_dict(decoded_v2)
                    # V2 is authoritative whenever present. The legacy
                    # _hermes_state marker is only a compatibility projection.
                    state = v2_state.state.value
                    state_info = {
                        **state_info,
                        "attempts": v2_state.retry.attempts,
                        "next_retry_at": v2_state.retry.next_at or "",
                        "no_progress_attempts": v2_state.retry.no_progress,
                    }
            except (TypeError, ValueError, json.JSONDecodeError):
                v2_state = None
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
            if uncertain_second_pass_eligible(post):
                uncertain_second_pass_ids.append(post_id)
        elif state in {STATE_AWAITING_HUMAN, "human_required"} or post.get("_wp_awaiting_human"):
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
        elif state == STATE_PARTIAL:
            partial_ids.append(post_id)
            if retry_eligible(effective_state):
                eligible_rework.append(post_id)
        else:  # NEW / PROCESSING / V2 pending / desconhecido
            if v2_state is None:
                unprocessed.append(post_id)
            if v2_state is not None and state == "pending" and cooldown_expired(state_info.get("next_retry_at") or ""):
                v2_eligible.append(post_id)
            if v2_state is None and _is_recent(post, cutoff):
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
                "partial": state == STATE_PARTIAL,
                "uncertain": state == STATE_UNCERTAIN,
                "uncertain_second_pass_eligible": uncertain_second_pass_eligible(post),
                "awaiting_human": state in {STATE_AWAITING_HUMAN, "human_required"} or post.get("_wp_awaiting_human"),
                "skipped": state == STATE_SKIPPED,
                "title": title,
                "v2": v2_state is not None,
                "phase": v2_state.phase.value if v2_state is not None else None,
                "blocker": v2_state.blocker.value if v2_state is not None and v2_state.blocker else None,
                "no_progress": v2_state.retry.no_progress if v2_state is not None else state_info.get("no_progress_attempts", 0),
                "eligible": (
                    cooldown_expired(v2_state.retry.next_at if v2_state is not None else state_info.get("next_retry_at") or "")
                    and state == "pending"
                    if v2_state is not None else state in (STATE_NEW, STATE_BLOCKED, STATE_PARTIAL)
                ),
                "media": (
                    {
                        "required": v2_state.media.required,
                        "accepted": v2_state.media.accepted,
                        "missing": v2_state.media.missing,
                        "featured": v2_state.media.featured.status.value,
                    }
                    if v2_state is not None else None
                ),
            }
        )
    rows.sort(key=lambda row: int(row["id"] or 0))
    unprocessed.sort()
    recent_unprocessed.sort()
    v2_eligible.sort()
    blocked_ids.sort()
    partial_ids.sort()
    recent_blocked.sort()
    eligible_rework.sort()
    # Loop sequencial: quando nao ha NEW nem BLOCKED elegivel, acorda o
    # proximo UNCERTAIN com cooldown vencido para uma segunda tentativa. O
    # segundo UNCERTAIN promove a AWAITING_HUMAN no apply, evitando ciclo infinito
    if not unprocessed and not eligible_rework:
        uncertain_retry_ids = [
            row["id"] for row in rows
            if row.get("state") == STATE_UNCERTAIN
            and int(row.get("attempts") or 0) <= 1
            and cooldown_expired(row.get("next_retry_at") or "")
        ]
        eligible_rework.extend(sorted(uncertain_retry_ids))
    ready_ids.sort()
    awaiting_human_ids.sort()
    uncertain_ids.sort()
    uncertain_second_pass_ids.sort()
    skipped_ids.sort()
    return {
        "pending": len(rows),
        "edited": len(ready_ids),
        "blocked": len(blocked_ids),
        "partial": len(partial_ids),
        "uncertain": len(uncertain_ids),
        "awaiting_human": len(awaiting_human_ids),
        "skipped": len(skipped_ids),
        "unprocessed_ids": unprocessed,
        "recent_unprocessed_ids": recent_unprocessed,
        "blocked_ids": blocked_ids,
        "partial_ids": partial_ids,
        "recent_blocked_ids": recent_blocked,
        "eligible_rework_ids": eligible_rework,
        "v2_eligible_ids": v2_eligible,
        "ready_ids": ready_ids,
        "awaiting_human_ids": awaiting_human_ids,
        "uncertain_ids": uncertain_ids,
        "uncertain_second_pass_ids": uncertain_second_pass_ids,
        "uncertain_second_pass_eligible": len(uncertain_second_pass_ids),
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
        if (entry / "editorial.blocked.json").is_file() or (entry / "editorial.partial.json").is_file():
            if not (entry / "uncertain.json").is_file():
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
        # Nem todo id com editorial.blocked.json é rework elegível: posts que
        # o humano devolveu ao status pending (ex.: awaiting_human) mantêm o
        # arquivo no filesystem, mas a meta _hermes_state manda. Resolve o
        # estado real de cada candidato e só conta os BLOCKED com cooldown
        # vencido — espelha o eligible_rework_ids do queue. (Sem isso, o
        # slice cego rework_ids[:per_page] saturava o lote com awaiting_human
        # e o trabalho real — rework elegível + posts novos — ficava invisível.)
        candidates = client.list_pending(
            include=rework_ids[:100], per_page=min(len(rework_ids[:100]), 100)
        )
        for cand in candidates:
            post_id = cand.get("id")
            if not isinstance(post_id, int):
                continue
            backups_dir = root / "backups" / str(post_id)
            state_info = read_state(cand)
            state = state_info["state"]
            if state is None:
                # Legado: marcadores de filesystem decidem (igual ao queue).
                if (backups_dir / "uncertain.json").is_file():
                    state = STATE_UNCERTAIN
                elif (backups_dir / "editorial.blocked.json").is_file():
                    state = STATE_BLOCKED
                elif (backups_dir / "editorial.latest.json").is_file():
                    state = STATE_READY
                else:
                    state = STATE_NEW
            if state in (STATE_BLOCKED, STATE_PARTIAL) and retry_eligible({**state_info, "state": state}):
                posts.append(cand)
    remaining = per_page - len(posts)
    if remaining > 0:
        # Percorre a fila até encontrar candidatos elegíveis. Consultar só a
        # página 1 fazia o monitor acordar o agente para trabalho que cards
        # não conseguia enxergar quando os primeiros pending já eram READY.
        page_size = max(remaining * 5, 20)
        for page in range(1, 101):
            chunk = client.list_pending(page=page, per_page=page_size)
            posts.extend(chunk)
            if len(chunk) < page_size:
                break
    seen: set[int] = set()
    ordered: list[dict[str, Any]] = []
    for post in posts:
        post_id = post.get("id")
        if not isinstance(post_id, int) or post_id in seen:
            continue
        seen.add(post_id)
        ordered.append(post)
    cards: list[dict[str, Any]] = []
    # Reserve deterministic capacity for UNCERTAIN second passes so NEW cannot
    # starve them, while keeping the independent quota bounded.
    def _state_for_card(post: dict[str, Any]) -> str | None:
        state_value = read_state(post)["state"]
        if state_value is not None:
            return state_value
        marker_dir = root / "backups" / str(post.get("id"))
        if (marker_dir / "uncertain.json").is_file():
            return STATE_UNCERTAIN
        if (marker_dir / "editorial.blocked.json").is_file():
            return STATE_BLOCKED
        if (marker_dir / "editorial.latest.json").is_file():
            return STATE_READY
        return STATE_NEW

    eligible_uncertain = [
        post for post in ordered
        if _state_for_card(post) == STATE_UNCERTAIN
        and uncertain_second_pass_eligible(post)
    ]
    uncertain_quota = max(0, int(getattr(config, "uncertain_second_pass_limit", 5)))
    reserved_uncertain_ids = {
        int(post["id"])
        for post in sorted(eligible_uncertain, key=lambda p: int(p["id"]))[:uncertain_quota]
    }
    def _priority(post: dict[str, Any]) -> tuple[int, int]:
        state_value = _state_for_card(post)
        post_id = int(post.get("id") or 0)
        state_info = read_state(post)
        partial_kind = _effective_partial_kind(state_info)
        if state_value == STATE_PARTIAL and partial_kind in {
            "featured_missing", "featured_vision",
        } and state_info.get("partial_missing", 0) == 0:
            return (-1, post_id)
        if state_value in (STATE_BLOCKED, STATE_PARTIAL):
            return (0, post_id)
        if post_id in reserved_uncertain_ids:
            return (1, post_id)
        if state_value == STATE_NEW:
            return (2, post_id)
        return (3, post_id)
    ordered.sort(key=_priority)
    uncertain_retry_mode = bool(reserved_uncertain_ids)
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
        uncertain_retry_eligible = False
        if state in (STATE_UNCERTAIN, STATE_AWAITING_HUMAN, STATE_SKIPPED, STATE_READY):
            uncertain_retry_eligible = (
                state == STATE_UNCERTAIN
                and uncertain_retry_mode
                and int(post_id) in reserved_uncertain_ids
                and uncertain_second_pass_eligible(post)
            )
            if not uncertain_retry_eligible:
                continue  # fora da fila, salvo fallback sequencial de uncertain
        blocked = state == STATE_BLOCKED
        if state in (STATE_BLOCKED, STATE_PARTIAL) and not retry_eligible({**state_info, "state": state}):
            continue
        title = (post.get("title") or {}).get("raw") or (post.get("title") or {}).get("rendered") or ""
        raw = (post.get("content") or {}).get("raw") or ""
        rendered = (post.get("content") or {}).get("rendered") or ""
        meta = post.get("meta") or {}
        if not isinstance(meta, dict):
            meta = {}
        wordpress_cleaned = clean_html(raw, post_title=str(title or ""))
        working_cleaned = wordpress_cleaned
        if state == STATE_PARTIAL:
            try:
                draft = load_draft(root, post_id)
                draft_html = str(draft.get("cleaned_html") or "")
                if draft_html:
                    working_cleaned = clean_html(draft_html, post_title=str(title or ""))
            except WorkflowError:
                pass
        entities = extract_entities(title=title, content_html=working_cleaned)
        # O conteúdo publicado e o working draft são visões diferentes:
        # PARTIAL ainda não grava HTML no WordPress, então a decisão de mídia
        # deve continuar do draft persistido, não do conteúdo antigo.
        wordpress_images = _images_summary(wordpress_cleaned, title, extract_entities(title=title, content_html=wordpress_cleaned))
        images = _images_summary(working_cleaned, title, entities)
        wordpress_featured = _featured_diagnosis(client, post, entities)
        partial_media_drift = None
        partial_manifest = _load_partial_manifest(root, post_id) if state == STATE_PARTIAL else {}
        featured = (
            _working_featured_diagnosis(client, post, entities, partial_manifest)
            if state == STATE_PARTIAL
            else wordpress_featured
        )
        if state == STATE_PARTIAL:
            images, partial_media_drift = _reconcile_partial_media(
                working_cleaned,
                title,
                entities,
                _partial_media_records(partial_manifest),
                stored={
                    "required": state_info.get("partial_required", images.get("required", 0)),
                    "completed": state_info.get("partial_completed", images.get("valid", 0)),
                    "missing": state_info.get("partial_missing", images.get("missing", 0)),
                },
            )
            if partial_media_drift:
                append_telemetry(root, "partial_media_drift", post_id=post_id, **partial_media_drift)
        fix = _fix_plan(backups_dir, images, featured, blocked) if blocked else None
        if state == STATE_PARTIAL:
            fix = {
                "find_inline_images": images.get("missing", 0),
                "featured": "provide" if featured.get("action") in {"provide", "replace"} else featured.get("action"),
                "requires_content": False,
            }
        partial_kind = _effective_partial_kind(state_info) if state == STATE_PARTIAL else None
        featured_only = partial_kind in {"featured_missing", "featured_vision"} and not images.get("missing")
        cards.append(
            {
                "id": post_id,
                "date": post.get("date"),
                "title": title,
                "word_count": word_count(rendered or raw),
                "entities": sorted(entities),
                "original_link": meta.get("original_link"),
                "seo_exists": _seo_is_valid(meta),
                "wordpress_images": wordpress_images,
                "working_images": images,
                "images": images,
                "featured": featured,
                "wordpress_featured": wordpress_featured,
                "working_featured": featured if state == STATE_PARTIAL else wordpress_featured,
                "game_hint": _game_hint(title),
                "state": state,
                "attempts": state_info["attempts"],
                "retry_mode": (
                    "featured_only" if featured_only else
                    "uncertain_second_pass"
                    if uncertain_retry_eligible
                    else None
                ),
                "next_retry_at": state_info["next_retry_at"],
                "uncertain_second_pass_eligible": uncertain_retry_eligible,
                "previous_relevance_reason": (
                    state_info["last_error"][:160] if uncertain_retry_eligible else ""
                ),
                "last_error": state_info["last_error"][:160],
                "blocked": blocked,
                "partial": state == STATE_PARTIAL,
                "partial_progress": {
                    "required": images.get("required", state_info.get("partial_required", 0)),
                    "completed": images.get("valid", state_info.get("partial_completed", 0)),
                    "missing": images.get("missing", state_info.get("partial_missing", 0)),
                    "processing_passes": state_info.get("processing_passes", 0),
                    "no_progress_attempts": state_info.get("no_progress_attempts", 0),
                } if state == STATE_PARTIAL else None,
                "partial_media_drift": partial_media_drift,
                "partial_kind": partial_kind,
                "blocked_reason": _blocked_reason(backups_dir) if blocked else None,
                "fix": fix,
                # P2 da auditoria de contexto: o card ja diz se o rework precisa
                # do artigo inteiro. Post NOVO fica `false` (o texto existente
                # normalmente basta; reescrever e uma decisao do agente, e para
                # isso existe `content POST_ID`).
                "requires_content": bool(fix.get("requires_content")) if fix else False,
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
    selected = cards[:per_page]
    try:
        append_telemetry(
            root,
            "uncertain_second_pass_funnel",
            eligible=len(eligible_uncertain),
            quota=uncertain_quota,
            reserved=sum(1 for card in selected if card.get("uncertain_second_pass_eligible")),
            selected=len(selected),
        )
    except Exception:  # noqa: BLE001 - telemetry never blocks cards
        pass
    return {
        "count": len(selected),
        "cards": selected,
        "uncertain_second_pass_eligible": len(eligible_uncertain),
        "uncertain_second_pass_reserved": sum(
            1 for card in selected if card.get("uncertain_second_pass_eligible")
        ),
    }


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


def _working_featured_diagnosis(
    client: WordPressClient,
    post: dict[str, Any],
    entities: set[str],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Use the PARTIAL featured ledger before falling back to WordPress."""
    featured = manifest.get("featured") if isinstance(manifest, dict) else None
    featured = featured if isinstance(featured, dict) else {}
    status = str(featured.get("status") or "missing").casefold()
    if status == "valid":
        return {
            "exists": True,
            "relevant": True,
            "webp": True,
            "dimensions": None,
            "valid": True,
            "action": "ok",
            "media_id": featured.get("media_id"),
            "source": "partial_manifest",
        }
    if status in {"vision_rejected", "rejected", "vision"}:
        return {
            "exists": bool(featured.get("media_id") or featured.get("media_url")),
            "relevant": False,
            "webp": None,
            "dimensions": None,
            "valid": False,
            "action": "replace",
            "media_id": featured.get("media_id"),
            "reason": featured.get("reason"),
            "source": "partial_manifest",
        }
    wordpress = _featured_diagnosis(client, post, entities)
    return {**wordpress, "source": "wordpress"}


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
    rewrite_text = "qualidade_texto" in names or "estrutura_lista" in names
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
        # P2 da auditoria de contexto: o agente SO pede `content POST_ID` quando
        # o rework realmente reescreve o texto. Um rework de midia/SEO/trailer
        # nunca precisa do artigo inteiro na conversa.
        "requires_content": bool(rewrite_text),
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


def _decision_fields(
    root: Path, post_id: int | None, plan: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Decisão de mídia do post para os eventos de resultado (auto/choose/reuse).

    É o que permite CRUZAR economia com qualidade: `apply_ready`/`apply_blocked`
    carregam a decisão que escolheu as imagens, então a telemetria mostra se os
    casos `auto` (sem julgamento do agente) bloqueiam mais ou menos que `choose`.

    ``plan`` (o `media_plan`) manda quando traz `decision_id`: o resultado passa a
    ser atribuído à decisão de CADA imagem (`decision_ids`) em vez da última
    decisão do post. Se o plano misturar decisões diferentes, o rótulo único
    `decision` é OMITIDO — atribuir o bloqueio do post a uma delas seria chute.
    """
    if not post_id:
        return {}
    campos: dict[str, Any] = {}
    itens = [item for item in (plan or []) if isinstance(item, dict)]
    itens_com_id = [item for item in itens if str(item.get("decision_id") or "")]
    # REGRA RÍGIDA: só se TODOS os itens do plano têm `decision_id`, TODOS resolvem
    # no ledger e TODAS as decisões resolvidas são IGUAIS é que o resultado pode
    # ser rotulado (auto/choose/reuse). Antes, um plano com 1 imagem rastreada e
    # outra SEM rastro saía como `resolved`/`auto` — e contaminava as estatísticas
    # de `auto` no apply_ready/apply_blocked.
    if itens and len(itens_com_id) != len(itens):
        campos["decision_attribution"] = "missing"
        campos["decision_ids"] = sorted(
            {str(item["decision_id"]) for item in itens_com_id}
        )
        return campos
    ids = [str(item.get("decision_id") or "") for item in itens_com_id]
    try:
        from .observability import read_media_decision, read_media_decision_by_id
    except Exception:  # noqa: BLE001 - telemetria nunca quebra o apply
        return campos
    if ids:
        campos["decision_ids"] = sorted(set(ids))
        # `decision` vem do LEDGER (pelo id), nunca do texto que o agente copiou
        # para o plano: um erro de cópia não pode virar medição.
        do_ledger: list[str] = []
        resolvidos = 0
        for identificador in ids:
            registro = read_media_decision_by_id(root, post_id, identificador)
            if registro:
                resolvidos += 1
                if str(registro.get("decision") or ""):
                    do_ledger.append(str(registro["decision"]))
        distintas = {d for d in do_ledger if d}
        if resolvidos != len(ids):
            # Algum id não existe no ledger: erro de cópia/invenção do agente —
            # sem rótulo (não se atribui um plano mal rastreado a uma decisão).
            campos["decision_attribution"] = "invalid"
            return campos
        campos["decision_attribution"] = "resolved"
        if len(distintas) == 1:
            campos["decision"] = next(iter(distintas))
            campos["decision_scope"] = "uniform"
        elif len(distintas) > 1:
            # Plano MISTO: atribuição resolvida, mas sem rótulo único — atribuir o
            # bloqueio do post a uma das decisões seria chute.
            campos["decision_scope"] = "mixed"
        return campos
    try:
        decisao = read_media_decision(root, post_id)
    except Exception:  # noqa: BLE001
        return campos
    if not plan and not decisao:
        # Nem plano nem histórico: o post não teve decisão de mídia — nada a
        # atribuir (não é "missing", é ausência legítima).
        return {}
    # Plano vazio (ou sem `decision_id`): a atribuição FALTA. E `decision` NÃO é
    # emitido: uma decisão de uma busca anterior não pode entrar em
    # `decision_quality` como se tivesse escolhido a imagem final — era assim que
    # um `media_plan: []` com histórico "auto" inflava `decision_quality.auto`.
    # O rótulo antigo fica só como contexto de diagnóstico (nada agrega nele).
    campos["decision_attribution"] = "missing"
    if decisao.get("decision"):
        campos["decision_unattributed"] = str(decisao["decision"])
    if decisao.get("score_gap") is not None:
        campos["score_gap_unattributed"] = decisao["score_gap"]
    return campos


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
    if post.get("status") not in ("pending", "awaiting_human"):
        raise WorkflowError(f"post {post_id} nao esta pending/awaiting_human ({post.get('status')})")
    if config.dry_run:
        raise WorkflowError("retry e uma operacao de escrita: exige write mode (EDITOR_DRY_RUN=false)")
    existing_v2 = (post.get("meta") or {}).get("_hermes_work_state")
    if existing_v2:
        try:
            from .pipeline_v2.model import LifecycleState, RetryInfo, WorkState
            raw_v2 = json.loads(existing_v2) if isinstance(existing_v2, str) else existing_v2
            previous = WorkState.from_dict(raw_v2)
            reopened = WorkState(
                state=LifecycleState.PENDING,
                phase=previous.phase,
                blocker=previous.blocker,
                retry=RetryInfo(attempts=0, no_progress=0, next_at=None),
                relevance_approved=previous.relevance_approved,
                media=previous.media,
            )
            serialized = json.dumps(reopened.to_dict(), ensure_ascii=False, separators=(",", ":"))
            if post.get("status") == "awaiting_human":
                client.move_to_status(post_id, "pending")
            client.update_post(post_id, {"meta": {"_hermes_work_state": serialized}})
            readback = client.get_post(post_id)
            meta = readback.get("meta") or {}
            verified = meta.get("_hermes_work_state") == serialized
            if not verified:
                raise WorkflowError(f"V2 retry read-back failed for post {post_id}")
            return {"post_id": post_id, "status": "retried", "state": reopened.state.value, "phase": reopened.phase.value, "blocker": reopened.blocker.value if reopened.blocker else None, "attempts": 0, "wordpress_changed": True, "readback": True}
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise WorkflowError(f"estado V2 invalido para retry {post_id}: {exc}") from exc

        # Devolve ao fluxo: status pending + reset de estado (o hook
        # transition_post_status do mu-plugin limpa as metas de pipeline).
        client.move_to_status(post_id, "pending")
    _write_state_markers(
        client,
        config,
        post_id,
        STATE_BLOCKED,
        root=root,
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

    Grava a decisão definitiva como ``SKIPPED`` — o post sai da agenda,
    nunca publica e não continua em UNCERTAIN/AWAITING_HUMAN.
    """
    post = client.get_post(post_id)
    # O discard humano precisa funcionar exatamente onde o pipeline PARA:
    # `pending` (fila) e `awaiting_human` (3a falha / midia esgotada). Antes so
    # `pending` era aceito, entao o operador tinha de fazer retry->pending->
    # discard para conseguir descartar justamente o post que pediu intervencao.
    if post.get("status") not in ("pending", "awaiting_human"):
        raise WorkflowError(
            f"post {post_id} nao esta pending nem awaiting_human ({post.get('status')})"
        )
    if config.dry_run:
        raise WorkflowError("discard e uma operacao de escrita: exige write mode (EDITOR_DRY_RUN=false)")
    # Discard NAO altera conteudo: a decisao humana pode ser exatamente "este
    # conteudo nao pertence ao portal". O baseline enrichment (CTA/fonte/links)
    # ficou restrito ao caminho BLOCKED, onde o post ja foi considerado
    # relevante pelo pipeline.
    baseline_changed = False
    # Preserva o motivo ANTERIOR: o discard nao pode apagar o historico do
    # bloqueio. Um rotulo generico ("off-topic") sobrescrevia o motivo real
    # (imagens_no_corpo/estrutura_lista/trailer) na meta do WP — depois disso o
    # post parecia "fora da pauta" para sempre e o conserto ficava invisivel.
    meta = post.get("meta") or {}
    anterior = str(meta.get("_hermes_last_error") or "").strip()
    motivo = reason or "descartado"
    if anterior and anterior != motivo and anterior not in motivo:
        motivo = f"{motivo} | motivo anterior do pipeline: {anterior}"
    editorial = {
        "site_relevance": {"decision": "skip", "confidence": 1.0, "reason": motivo},
        # Marcador de DECISAO (triagem): distingue o descarte deliberado ("nao
        # atende ao filtro do portal") do UNCERTAIN que ainda aguarda revisao
        # humana. Sem ele o watchdog seguiria alertando incertos ja decididos e
        # a fila nunca apareceria limpa para o operador.
        "discarded": True,
        "discarded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    _save_uncertain(root, post_id, editorial)
    _state_before = read_state(post)
    _attempts_after = int(_state_before.get("attempts") or 0) + 1
    # discard é decisão definitiva: não é uma nova rodada de incerteza.
    _target_state = STATE_SKIPPED
    _next_retry = ""
    persisted = _write_state_markers(
        client,
        config,
        post_id,
        _target_state,
        root=root,
        attempts=_attempts_after,
        next_retry_at=_next_retry,
        last_error=motivo,
    )
    if not persisted:
        raise WorkflowError(
            f"estado SKIPPED nao persistiu no WordPress (post {post_id})"
        )
    if _target_state == STATE_AWAITING_HUMAN and not config.dry_run:
        client.move_to_status(post_id, "awaiting_human")
    # `awaiting_human` ele continuaria aparecendo no filtro como se ainda
    # esperasse decisão — quem o retira da automação é o estado SKIPPED; o
    # status volta para pending e a confirmação é verificada.
    if _target_state != STATE_AWAITING_HUMAN and str(post.get("status") or "") == "awaiting_human":
        try:
            client.move_to_status(post_id, "pending")
            _confirmado = client.get_post(post_id)
            if str(_confirmado.get("status") or "") != "pending":
                append_telemetry(
                    root, "discard_status_mismatch",
                    post_id=post_id,
                    status_wp=str(_confirmado.get("status") or ""),
                )
        except Exception as exc:  # noqa: BLE001 - falha operacional deve ser explícita
            try:
                append_telemetry(root, "discard_status_move_failed",
                                 post_id=post_id, error=str(exc)[:200])
            except Exception:  # noqa: BLE001
                pass
            raise WorkflowError(
                f"estado SKIPPED persistiu, mas status WordPress nao voltou para pending "
                f"(post {post_id}): {exc}"
            ) from exc
    return {
        "post_id": post_id,
        "status": "awaiting_human" if _target_state == STATE_AWAITING_HUMAN else "discarded",
        "state": _target_state,
        "wordpress_changed": True,
        "baseline_enriched": baseline_changed,
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
    post = client.get_post(post_id)
    if post.get("status") != "pending":
        raise WorkflowError(
            f"post {post_id} nao esta pending ({post.get('status')}); "
            "use retry ou discard para uma decisao humana definitiva"
        )
    latest_editorial = root / "backups" / str(post_id) / "editorial.latest.json"
    try:
        latest = json.loads(latest_editorial.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        latest = {}
    relevance = latest.get("site_relevance") if isinstance(latest, dict) else None
    relevance_process = (
        isinstance(relevance, dict)
        and str(relevance.get("decision") or "").strip().lower() == "process"
    )
    if not relevance_process:
        for output in sorted(
            (root / "work" / "batches").glob("*/editorial.output.json"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        ):
            try:
                batch_output = json.loads(output.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            results = batch_output.get("results") if isinstance(batch_output, dict) else None
            for result in results if isinstance(results, list) else []:
                if int(result.get("post_id") or 0) != int(post_id):
                    continue
                editorial = result.get("editorial") if isinstance(result, dict) else None
                relevance = editorial.get("site_relevance") if isinstance(editorial, dict) else None
                relevance_process = (
                    isinstance(relevance, dict)
                    and str(relevance.get("decision") or "").strip().lower() == "process"
                )
                break
            if relevance_process:
                break
    if relevance_process:
        raise WorkflowError(
            "uncertain_not_allowed_after_relevance_process: continue media processing "
            "and execute apply; media-only failures belong to PARTIAL"
        )
    if config.dry_run:
        raise WorkflowError("uncertain e uma operacao de escrita: exige write mode (EDITOR_DRY_RUN=false)")
    motivo = reason.strip()
    state_before = read_state(post)
    attempts_before = int(state_before.get("attempts") or 0)
    attempts_after = attempts_before + 1
    target_state = (
        STATE_AWAITING_HUMAN
        if state_before.get("state") == STATE_UNCERTAIN and attempts_before >= 1
        else STATE_UNCERTAIN
    )
    backoff = rework_backoff(
        attempts_after,
        cooldown_minutes=config.rework_cooldown_minutes,
        max_attempts=config.max_rework_attempts,
    )
    next_retry_at = backoff["next_retry_at"] if target_state == STATE_UNCERTAIN else ""
    editorial = {
        "site_relevance": {"decision": "skip", "confidence": 0.0, "reason": motivo},
        "uncertain": True,
        "uncertain_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    _save_uncertain(root, post_id, editorial)
    persisted = _write_state_markers(
        client,
        config,
        post_id,
        target_state,
        root=root,
        attempts=attempts_after,
        next_retry_at=next_retry_at,
        last_error=motivo,
    )
    if not persisted:
        raise WorkflowError(
            f"estado {target_state.upper()} nao persistiu no WordPress (post {post_id})"
        )
    if target_state == STATE_AWAITING_HUMAN:
        client.move_to_status(post_id, "awaiting_human")
    return {
        "post_id": post_id,
        "status": "uncertain" if target_state == STATE_UNCERTAIN else "awaiting_human",
        "state": target_state,
        "wordpress_changed": True,
        "attempts": attempts_after,
        "next_retry_at": next_retry_at,
    }
