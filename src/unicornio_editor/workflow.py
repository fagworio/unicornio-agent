"""Application workflows shared by the CLI and integration tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Lock

import datetime
import hashlib
import json
import logging
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, cast
from urllib.parse import urlparse

from .backup import SnapshotStore, atomic_write_text
from .builder import BuilderError, append_canonical_footer
from .checklist import _canonical_image_url, _required_image_count, run_pre_publish_checklist
from .config import Config
from .editorial_schema import validate_editorial
from .html_cleaner import _repair_orphan_media, clean_html, remove_empty_paragraphs
from .language import editorial_language_report
from .list_quality import detect_list_format, extract_list_items, list_item_media_assets
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
    MediaConversionError,
    convert_to_webp,
    image_dimensions,
    image_has_transparency,
    image_is_mostly_flat,
    prepare_featured_webp,
)
from .media.downloader import MediaDownloadError, download_image
from .media.diagnostics import diagnostic_url_domain
from .media.inserter import (
    MediaInsertionError, append_featured_credit, insert_media, normal_media_slot_capacity,
    ordinary_game_media_slots, plan_normal_media_insertions,
    select_game_media_slots,
)
from .media.relevance import extract_entities, image_is_relevant, iter_content_images
from .media.phash_policy import PHASH_DUPLICATE_MAX_DISTANCE
from .media.text import sanitize_title, split_game_names
from .media.source_verify import (
    verification_result,
    verify_downloaded_against_source,
)
from .media.url_safety import URLSafetyError
from .media.vision_cache import get_cached_decision, set_cached_decision, vision_model_identity
from .media.vision_gate import (
    V1_FEATURED_VISION_POLICY,
    V1_INLINE_VISION_POLICY,
    V1_MATCH_THRESHOLD,
    VisionGateError,
    VisionInputUnavailable,
    prepare_vision_image_input_from_path,
    verify_image_subject_decision,
    v1_vision_context_key,
    vision_config_ready,
)
from .media.vision_policy import (
    VISION_POLICY_VERSION,
    featured_vision_category,
    featured_vision_context_key,
    featured_vision_subject,
    resolve_editorial_identity,
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
    META_STATE,
    build_state_markers,
    cooldown_expired,
    read_state,
    retry_eligible,
    rework_backoff,
    uncertain_second_pass_eligible,
)
from .trailer import (
    TrailerError, build_trailer_html, cached_game_trailer_checked_at,
    find_cached_game_trailer_with_status, find_game_trailer_with_status,
    trailer_search_query,
)
from .wordpress import SafetyError, WordPressClient, WordPressError
from .pipeline_v2.quarantine import (
    QuarantinePolicyError,
    assert_post_not_quarantined,
    filter_quarantined_posts,
    quarantine_for_root,
)
from .pipeline_v2.lock import RunSessionLock
from .pipeline_v2.epoch_protection import EpochProtectionError, load_epoch_protection
from .eligibility import assert_v1_mutation_allowed, resolve_post_eligibility

META_PUBLISH_OPERATION_ID = "_ai_editor_publish_operation_id"

_DIAGNOSTIC_MEDIA_ENGINES = frozenset({
    "duckduckgo", "bing", "yandex", "google", "article_source", "library_reuse",
})


def _diagnostic_media_engine(item: dict[str, Any]) -> str:
    engine = str(item.get("engine") or "").casefold()
    return engine if engine in _DIAGNOSTIC_MEDIA_ENGINES else "unknown"


# Media plan: download/upload/verificacao sao I/O-bound (rede); threads
# liberam o GIL durante E/S. Falhas de item são registradas no próprio item;
# nunca reexecutamos um lote parcial, pois upload é efeito colateral.
_MEDIA_WORKERS = 4


def _structured_media_failure(
    exc: Exception,
    *,
    stage: str,
    reason_code: str,
    recoverable: bool,
    retry_same_source: bool | None = None,
) -> dict[str, Any]:
    """Return bounded failure metadata without copying provider/URL details."""
    conversion_reason = str(getattr(exc, "reason_code", "") or "")
    conversion_kind = str(getattr(exc, "failure_kind", "") or "")
    conversion_stage = str(getattr(exc, "stage", "") or "")
    conversion_recoverable = getattr(exc, "recoverable", None)
    if stage == "conversion" and conversion_reason:
        reason_code = conversion_reason
    if stage == "conversion" and conversion_stage:
        stage = conversion_stage
    text = str(exc or "").casefold()
    http_match = re.search(r"\bHTTP[\s_-]*(\d{3})\b", text, re.IGNORECASE)
    if http_match:
        status = int(http_match.group(1))
        failure_kind = f"http_{status}" if status < 500 else "http_5xx"
    elif "timeout" in text or "timed out" in text:
        failure_kind = "timeout"
    elif "size limit" in text or "exceeds size" in text:
        failure_kind = "size_limit"
    elif "decode" in text or "invalid image" in text or "not an image" in text:
        failure_kind = "decode_error"
    elif "network" in text or "unreachable" in text or "connection" in text:
        failure_kind = "network_error"
    elif conversion_kind:
        failure_kind = conversion_kind
    else:
        failure_kind = "exception"
    if retry_same_source is None:
        retry_same_source = conversion_recoverable is True or failure_kind in {
            "timeout", "network_error", "http_408", "http_425", "http_429", "http_5xx",
        }
    return {
        "status": "error",
        "reason_code": reason_code,
        "failure_stage": stage,
        "failure_kind": failure_kind,
        "recoverable": bool(
            conversion_recoverable if conversion_recoverable is not None else recoverable
        ),
        "retry_same_source": bool(retry_same_source),
        "detail": f"{stage} failed ({failure_kind})",
        **(
            {"diagnostics": dict(getattr(exc, "diagnostics"))}
            if isinstance(getattr(exc, "diagnostics", None), dict)
            else {}
        ),
    }


class WorkflowError(RuntimeError):
    """Raised when a post cannot safely enter a workflow step."""


class PostLockContention(WorkflowError):
    """Lock admission refused before adopting ownership or running post work."""


def is_post_lock_blocked(exc: BaseException) -> bool:
    """Recognize an item-local post-lock conflict through workflow wrapping."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, LockError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def known_wordpress_entity_missing(exc: WordPressError) -> bool:
    """Recognize only a WordPress-confirmed missing item, not a broken route."""
    return exc.status_code in (404, 410) and exc.api_code == "rest_post_invalid_id"


def wordpress_read_stops_batch(client: Any, exc: WordPressError) -> bool:
    if known_wordpress_entity_missing(exc):
        return False
    # Standard REST calls provide status/cause; a legacy test/store adapter
    # can expose an untyped, post-local failure without claiming endpoint health.
    return bool(
        isinstance(client, WordPressClient) or exc.status_code is not None
        or exc.__cause__ is not None or exc.error_code != "wordpress_error"
    )


class CardsSelectionTimeout(WorkflowError):
    """Raised when card discovery reaches its explicit wall-clock budget."""

    error_code = "cards_selection_timeout"

    def __init__(self, stage: str, pages_scanned: int, elapsed_seconds: float):
        self.stage = stage
        self.pages_scanned = pages_scanned
        self.elapsed_seconds = max(0.0, elapsed_seconds)
        super().__init__(
            f"cards selection timed out during {stage} after "
            f"{self.elapsed_seconds:.3f}s ({pages_scanned} pages scanned)"
        )


class MediaFunnelInvariantError(WorkflowError):
    """Raised when media candidates lose their discovery identity."""


def _acquire_post_lock(root: Path, config: Config | None, post_id: int):
    """Serialize every mutating operation for one post.

    The WordPress status re-fetch protects against a late manual publish, but
    it cannot prevent two cron sessions from uploading the same media in
    parallel. The filesystem lock covers that expensive side effect.
    """
    try:
        return LockManager(
            root / "work" / "locks", ttl=int(getattr(config, "lock_ttl", 900))
        ).acquire(post_id)
    except LockError as exc:
        error_type = PostLockContention if exc.blocked_before_acquire else WorkflowError
        raise error_type(f"post {post_id} is already being processed") from exc


class _PostLockGuardedClient:
    """Assert current lock ownership immediately before each WordPress write."""

    _MUTATING_METHODS = {"update_post", "move_to_status", "publish", "upload_media"}

    def __init__(self, client: Any, lock_handle: Any, *, root: Path | None = None, post_id: int | None = None):
        self._client = client
        self._lock_handle = lock_handle
        self._root = Path(root) if root is not None else None
        self._post_id = int(post_id) if post_id is not None else None

    def __getattr__(self, name: str):
        method = getattr(self._client, name)
        if name not in self._MUTATING_METHODS:
            return method

        def guarded_write(*args, **kwargs):
            self._lock_handle.assert_owned()
            if self._root is not None and self._post_id is not None:
                load_epoch_protection(self._root).assert_writable(self._post_id, effect=name)
            scope = getattr(self._client, "post_write_guard", None)
            if callable(scope):
                with scope(self._lock_handle.assert_owned):
                    self._lock_handle.assert_owned()
                    return method(*args, **kwargs)
            return method(*args, **kwargs)

        return guarded_write


def _guard_post_client(client: Any, lock_handle: Any, *, root: Path | None = None, post_id: int | None = None) -> _PostLockGuardedClient:
    return _PostLockGuardedClient(client, lock_handle, root=root, post_id=post_id)


def _prepare_post_locked(client: WordPressClient, root: Path, post_id: int, lock_handle: Any) -> dict[str, Any]:
    assert_post_not_quarantined(root, post_id, operation="prepare")
    post = client.get_post(post_id)
    _require_pending(post)
    try:
        assert_v1_mutation_allowed(post, root, operation="prepare")
    except RuntimeError as exc:
        raise WorkflowError(str(exc)) from exc
    lock_handle.assert_owned()
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


def prepare_post(
    client: WordPressClient,
    root: Path,
    post_id: int,
    *,
    config: Config | None = None,
    lock_handle: Any | None = None,
) -> dict[str, Any]:
    """Snapshot under a per-post lock; default the lock TTL for legacy callers."""
    if lock_handle is not None:
        return _prepare_post_locked(client, root, post_id, lock_handle)
    with _acquire_post_lock(root, config, post_id) as acquired_lock:
        return _prepare_post_locked(client, root, post_id, acquired_lock)


def _partial_manifest_path(root: Path, post_id: int) -> Path:
    return root / "backups" / str(post_id) / "editorial.partial.json"


def _is_v1_fresh_media_layout_source(post: dict[str, Any], root: Path, post_id: int) -> bool:
    resolved = resolve_post_eligibility(post, root)
    content = post.get("content")
    raw = content.get("raw") if isinstance(content, dict) else None
    return (
        post.get("status") == "pending"
        and not resolved.is_v2
        and resolved.eligible_for_v1
        and resolved.state == STATE_NEW
        and resolved.attempts == 0
        and isinstance(raw, str)
        and not re.search(r"<img\b", raw, re.IGNORECASE)
        and not _partial_manifest_path(root, post_id).exists()
    )


class V1PartialResumeError(WorkflowError):
    """A retained inline attachment or its recorded layout cannot be reused safely."""

    def __init__(self, code: str, *, post_id: int) -> None:
        super().__init__(f"{code}: post {post_id}")
        self.error_code = code


def _v1_is_listicle(post: dict[str, Any], editorial: dict[str, Any], body: str) -> bool:
    categories = editorial.get("categories") or post.get("categories") or ()
    seo = editorial.get("seo")
    seo_title = str(seo.get("title") or "") if isinstance(seo, dict) else ""
    titles = (_post_title(post) or "", str(editorial.get("title") or ""), seo_title)
    return any(detect_list_format(title, body, categories) is not None for title in titles)


def _v1_partial_binding(post: dict[str, Any], editorial: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "post_id": post["id"],
        "source": hashlib.sha256(json.dumps(
            [_raw_content(post), _post_title(post), _original_link(post)],
            ensure_ascii=False, separators=(",", ":"),
        ).encode()).hexdigest(),
        "body": hashlib.sha256(editorial["cleaned_html"].encode()).hexdigest(),
        "game_name": str(editorial.get("game_name") or ""),
    }


def verified_v1_partial_resume(
    client: Any, root: Path, post: dict[str, Any],
) -> dict[str, Any] | None:
    """Read authoritative, layout-bound retained inline assets for a normal article."""
    if read_state(post).get("state") != STATE_PARTIAL:
        return None
    post_id = int(post["id"])
    try:
        manifest = _load_partial_manifest(root, post_id, required=True)
    except WorkflowError as exc:
        raise V1PartialResumeError("partial_resume_manifest_invalid", post_id=post_id) from exc
    accepted = _partial_media_records(manifest)
    if not accepted:
        return None
    binding = manifest.get("resume_binding")
    try:
        draft = load_draft(root, post_id)
    except WorkflowError as exc:
        code = "partial_resume_unbound" if binding is None else "partial_resume_draft_unverified"
        raise V1PartialResumeError(code, post_id=post_id) from exc
    body = draft.get("cleaned_html")
    if binding is None and isinstance(body, str) and _v1_is_listicle(post, draft, body):
        # Existing item-scoped listicle continuation is not a flat layout.
        return None
    if not isinstance(binding, dict):
        raise V1PartialResumeError("partial_resume_unbound", post_id=post_id)
    if not isinstance(body, str) or not body.strip() or binding != _v1_partial_binding(post, draft):
        raise V1PartialResumeError("partial_resume_source_changed", post_id=post_id)
    if _v1_is_listicle(post, draft, body):
        raise V1PartialResumeError("partial_resume_listicle_requires_review", post_id=post_id)
    slots: set[int] = set()
    ids: set[int] = set()
    urls: set[str] = set()
    for row in accepted:
        media_id = row.get("media_id")
        slot = row.get("paragraph_index")
        url = str(row.get("media_url") or "")
        if (
            type(media_id) is not int or media_id <= 0 or media_id in ids
            or type(slot) is not int or slot < 0 or slot in slots
            or not _is_webp_attachment_url(url) or _canonical_image_url(url) in urls
            or not re.fullmatch(r"[0-9a-f]{16}", str(row.get("phash") or ""), re.IGNORECASE)
            or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("sha256") or ""), re.IGNORECASE)
        ):
            raise V1PartialResumeError("partial_attachment_record_invalid", post_id=post_id)
        try:
            attachment = client.get_media(media_id)
        except WordPressError as exc:
            code = "partial_attachment_missing" if exc.status_code in {404, 410} else "partial_attachment_unverified"
            raise V1PartialResumeError(code, post_id=post_id) from exc
        if not isinstance(attachment, dict):
            raise V1PartialResumeError("partial_attachment_unverified", post_id=post_id)
        details = attachment.get("media_details") or {}
        if not isinstance(details, dict) or not all(key in details for key in ("width", "height")):
            raise V1PartialResumeError("partial_attachment_unverified", post_id=post_id)
        if (
            attachment.get("id") != media_id or attachment.get("source_url") != url
            or attachment.get("mime_type") != "image/webp"
            or details.get("width") != row.get("width") or details.get("height") != row.get("height")
        ):
            raise V1PartialResumeError("partial_attachment_changed", post_id=post_id)
        slots.add(slot)
        ids.add(media_id)
        urls.add(_canonical_image_url(url))
    return {"binding": binding, "body": body, "accepted": accepted, "manifest": manifest}


def _v1_media_subject(editorial: dict[str, Any]) -> tuple[str, str]:
    identity = resolve_editorial_identity(editorial)
    return (
        " ".join(str(identity.get("primary_subject") or "").casefold().split()),
        str(identity.get("subject_type") or ""),
    )


def _v1_progress_source(post: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        [post["id"], _original_link(post)],
        ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()


def _v1_content_digest(html: str) -> str:
    return hashlib.sha256(html.encode()).hexdigest()


def _v1_progress_record_valid(row: Any) -> bool:
    return (
        isinstance(row, dict) and row.get("status") == "accepted"
        and type(row.get("media_id")) is int and row["media_id"] > 0
        and isinstance(row.get("media_url"), str) and _is_webp_attachment_url(row["media_url"])
        and all(type(row.get(key)) is int and row[key] > 0 for key in ("width", "height"))
        and isinstance(row.get("alt_text"), str) and isinstance(row.get("credit_text"), str)
        and bool(re.fullmatch(r"[0-9a-f]{64}", str(row.get("sha256") or "")))
        and bool(re.fullmatch(r"[0-9a-f]{16}", str(row.get("phash") or "")))
        and isinstance(row.get("visual_verification"), dict)
    )


def _verified_v1_progress_asset(
    client: Any, config: Config, post_id: int, row: dict[str, Any],
) -> bool:
    """Use positive proof for reuse; a transient read never becomes a new deficit."""
    try:
        asset = client.get_media(row["media_id"])
    except WordPressError as exc:
        if known_wordpress_entity_missing(exc):
            return False
        if exc.status_code in {401, 403}:
            raise
        raise V1PartialResumeError("retained_media_read_unavailable", post_id=post_id) from exc
    if not isinstance(asset, dict) or not isinstance(asset.get("media_details"), dict):
        raise V1PartialResumeError("retained_media_read_incomplete", post_id=post_id)
    details = asset["media_details"]
    if (
        type(asset.get("id")) is not int or not asset.get("source_url") or not asset.get("mime_type")
        or type(details.get("width")) is not int or type(details.get("height")) is not int
    ):
        raise V1PartialResumeError("retained_media_read_incomplete", post_id=post_id)
    if (
        asset.get("id") != row["media_id"] or asset.get("source_url") != row["media_url"]
        or asset.get("mime_type") != "image/webp"
        or (details.get("width"), details.get("height")) != (row["width"], row["height"])
    ):
        return False
    try:
        _verify_v1_retained_bytes(
            config, post_id, [row],
            url_policy="enforce" if config.media_source_policy == "credit_only" else None,
        )
    except V1PartialResumeError as exc:
        if exc.error_code == "partial_attachment_bytes_changed":
            return False
        raise
    return True


def verified_v1_media_progress(
    client: Any, config: Config, root: Path, post: dict[str, Any], *, reconcile_uploads: bool = False,
) -> dict[str, Any] | None:
    """Read V1 progress independent of editorial disposition, not a READY decision."""
    if post.get("status") != "pending" or read_state(post).get("state") not in {None, STATE_NEW, STATE_PARTIAL, STATE_BLOCKED}:
        return None
    post_id = int(post["id"])
    path = _partial_manifest_path(root, post_id)
    if not path.is_file() or resolve_post_eligibility(post, root).is_v2:
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise V1PartialResumeError("partial_resume_manifest_invalid", post_id=post_id) from exc
    if not isinstance(manifest, dict):
        raise V1PartialResumeError("partial_resume_manifest_invalid", post_id=post_id)
    if "v1_media_progress" not in manifest:
        return None
    saved = manifest["v1_media_progress"]
    if (
        not isinstance(saved, dict) or saved.get("version") != 1
        or saved.get("post_id") != post_id
        or not isinstance(saved.get("source"), str)
        or not isinstance(saved.get("bodies"), list)
        or not isinstance(saved.get("rows"), list)
        or not isinstance(saved.get("subject"), list) or len(saved["subject"]) != 2
        or not all(isinstance(key, str) for key in saved["subject"])
        or not isinstance(saved.get("intents", {}), dict)
    ):
        raise V1PartialResumeError("retained_media_manifest_invalid", post_id=post_id)
    final_manifest = saved.get("ready_manifest") if saved.get("ready_staged") else None
    if (
        isinstance(final_manifest, dict) and saved["source"] == _v1_progress_source(post)
        and saved.get("source_title") == _post_title(post) and not saved.get("intents")
        and manifest_matches(post, final_manifest, manifest_hash(final_manifest), policy_version=config.policy_version)
    ):
        return {**saved, "finalization_pending": True}
    if (
        saved["source"] != _v1_progress_source(post)
        or _v1_content_digest(_raw_content(post)) not in saved["bodies"]
        or saved.get("policy") != [config.media_source_policy, config.policy_version, VISION_POLICY_VERSION]
    ):
        if saved.get("intents"):
            raise V1PartialResumeError("retained_media_upload_context_changed", post_id=post_id)
        return None
    if saved.get("intents"):
        if saved.get("source_title") != _post_title(post):
            raise V1PartialResumeError("retained_media_upload_context_changed", post_id=post_id)
        saved = _reconcile_v1_uploads(
            client, config, root, post_id, manifest, saved,
            persist=reconcile_uploads and not config.dry_run,
        )
    accepted: list[dict[str, Any]] = []
    for row in saved["rows"]:
        if not _v1_progress_record_valid(row):
            continue
        if any(
            row[key] == other[key] for other in accepted for key in ("media_id", "media_url", "sha256", "phash")
        ):
            continue
        if _verified_v1_progress_asset(client, config, post_id, row):
            accepted.append(row)
    return {**saved, "rows": accepted}


def place_v1_retained_media(
    progress: dict[str, Any], editorial: dict[str, Any], *, title: str, categories: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Preserve proof independently of the old paragraph positions."""
    if not progress.get("subject", [""])[0] or tuple(progress["subject"]) != _v1_media_subject(editorial):
        return [], None
    body = str(editorial.get("cleaned_html") or "")
    seo = editorial.get("seo") or {}
    title = next((value for value in (title, str(editorial.get("title") or ""), str(seo.get("title") or ""))
                  if detect_list_format(value, body, categories) is not None), title)
    is_list = detect_list_format(title, body, categories) is not None
    items = {item.identity: item for item in extract_list_items(title, body, categories)} if is_list else {}
    raw_urls = {_canonical_image_url(row["src"]) for row in iter_content_images(body) if row.get("src")}
    existing_items = {
        row.get("item_identity") for row in list_item_media_assets(title, body, categories)
    } if is_list else set()
    game_names = split_game_names(editorial.get("game_name"))
    section_slots: dict[int, tuple[str, str]] = {}
    if not is_list and len(game_names) > 1:
        try:
            section_slots = ordinary_game_media_slots(body, game_names, source_html=body)
        except MediaInsertionError:
            pass  # No persisted image may be moved to an unbound section.
    section_layout = normal_media_slot_capacity(body) if section_slots else None
    section_rows: dict[str, list[dict[str, Any]]] = {}
    retained = []
    featured = None
    for row in progress["rows"]:
        if row.get("featured"):
            featured = row
            continue
        if _canonical_image_url(row["media_url"]) in raw_urls:
            continue
        key = row.get("item_identity")
        if is_list:
            item = items.get(key)
            if item is None or key in existing_items:
                continue
            if " ".join(str(row.get("subject") or "").casefold().split()) != " ".join(item.heading.casefold().split()):
                continue
            retained.append({
                **row, "paragraph_index": item.paragraph_index, "section_slot": item.paragraph_index,
                "html_position": item.html_position, "section_heading": item.heading,
            })
            # Multiple distinct roles within the same list item can be retained;
            # the list planner still checks that every position stays in that item.
        elif len(game_names) > 1:
            evidence = row.get("vision_evidence") or {}
            subject = evidence.get("subject")
            if subject in game_names and evidence.get("image_role") == "inline" and evidence.get("verdict") == "accept":
                section_rows.setdefault(subject, []).append(row)
        elif not key:
            retained.append({**row, "placement": None})
    if section_rows and section_layout is not None:
        entities = extract_entities(title=title, content_html=body, game_name=editorial.get("game_name"))
        deficit = _images_summary(body, title, entities)["missing"]
        counts = {subject: len(rows) for subject, rows in section_rows.items()}
        for count in range(deficit, 0, -1):
            try:
                slots = select_game_media_slots(section_slots, count, retained_counts=counts)
            except MediaInsertionError:
                continue
            for slot in slots:
                subject, heading = section_slots[slot]
                if not section_rows.get(subject):
                    continue
                row = section_rows[subject].pop(0)
                retained.append({
                    **row, "paragraph_index": slot, "section_slot": slot,
                    "section_heading": heading, "subject": subject,
                    "target_id": f"game-section-{slot}",
                    "placement": ("after_editorial_body" if section_layout["body_end_available"]
                                  and slot == section_layout["last_slot"] else None),
                })
            break
    if retained and not is_list and len(game_names) <= 1:
        entities = extract_entities(title=title, content_html=body, game_name=editorial.get("game_name"))
        required = _images_summary(body, title, entities)["missing"]
        retained = retained[:required]
        # Reserve the current edition's full quota if its layout allows it so
        # retained assets do not occupy slots required by new candidates.
        try:
            return plan_normal_media_insertions(
                body, retained + [{}] * (required - len(retained))
            )[:len(retained)], featured
        except MediaInsertionError:
            try:
                return plan_normal_media_insertions(body, retained), featured
            except MediaInsertionError as exc:
                raise V1PartialResumeError(exc.code, post_id=progress["post_id"]) from exc
    return retained, featured


class _V1MediaProgress:
    """Checkpoint writes while the caller still owns the existing post lock."""

    def __init__(self, client: Any, config: Config, root: Path, post: dict[str, Any],
                 editorial: dict[str, Any], prior: dict[str, Any] | None) -> None:
        self.client, self.config, self.root = client, config, root
        self.post_id = int(post["id"])
        subject = list(_v1_media_subject(editorial))
        self.record = dict(prior) if prior and prior["subject"] == subject else {
            "version": 1, "post_id": self.post_id, "source": _v1_progress_source(post),
            "subject": subject, "rows": [], "intents": {},
            "policy": [config.media_source_policy, config.policy_version, VISION_POLICY_VERSION],
        }
        self.record.pop("ready_manifest", None)
        self.record.pop("ready_staged", None)
        self.record["source_title"] = _post_title(post)
        self.record["bodies"] = sorted({
            _v1_content_digest(_raw_content(post)),
            _v1_content_digest(_baseline_content(post, config)),
        })

    def save(self) -> None:
        existing = _load_partial_manifest(self.root, self.post_id)
        _save_partial_manifest(self.root, self.post_id, {**existing, "v1_media_progress": self.record})

    def begin(self, row: dict[str, Any]) -> str:
        token = "unicornio-" + uuid.uuid4().hex
        self.record["intents"][token] = row.copy()
        self.save()
        return token

    def no_write(self, token: str) -> None:
        del self.record["intents"][token]
        self.save()

    def accepted(self, row: dict[str, Any]) -> None:
        token = row.get("upload_token")
        if token not in self.record["intents"]:
            return
        self.record["intents"][token] = row
        self.save()
        self.remember(row)

    def remember(self, row: dict[str, Any]) -> None:
        if not _v1_progress_record_valid(row):
            raise V1PartialResumeError("retained_media_upload_uncertain", post_id=self.post_id)
        if not _verified_v1_progress_asset(
            self.client, self.config, self.post_id, row,
        ):
            raise V1PartialResumeError("retained_media_upload_uncertain", post_id=self.post_id)
        self.record["rows"] = [
            previous for previous in self.record["rows"]
            if previous["media_id"] != row["media_id"]
        ] + [row]
        self.record["intents"].pop(row.get("upload_token"), None)
        self.save()


def _reconcile_v1_uploads(
    client: Any, config: Config, root: Path, post_id: int, manifest: dict[str, Any],
    progress: dict[str, Any], *, persist: bool,
) -> dict[str, Any]:
    """Adopt only a unique positive match; absence is not a safe retry signal."""
    updated = {**progress, "rows": list(progress["rows"]), "intents": dict(progress["intents"])}
    for token, intent in progress["intents"].items():
        if not isinstance(token, str) or not re.fullmatch(r"unicornio-[0-9a-f]{32}", token) or not isinstance(intent, dict):
            raise V1PartialResumeError("retained_media_manifest_invalid", post_id=post_id)
        row = intent
        if not row.get("media_id"):
            if not callable(getattr(client, "search_media", None)):
                raise V1PartialResumeError("retained_media_upload_uncertain", post_id=post_id)
            matches = [
                media for media in client.search_media(token)
                if isinstance(media, dict) and type(media.get("id")) is int
                and isinstance(media.get("title"), dict)
                and str(media["title"].get("rendered") or "").endswith(f" [{token}]")
            ]
            if len(matches) != 1:
                raise V1PartialResumeError("retained_media_upload_uncertain", post_id=post_id)
            row = {**intent, "media_id": matches[0]["id"], "media_url": matches[0].get("source_url"),
                   "upload_token": token}
        if not _v1_progress_record_valid(row) or not _verified_v1_progress_asset(client, config, post_id, row):
            raise V1PartialResumeError("retained_media_upload_uncertain", post_id=post_id)
        updated["rows"] = [r for r in updated["rows"] if not isinstance(r, dict)
                           or r.get("media_id") != row["media_id"]] + [row]
        del updated["intents"][token]
    if persist:
        _save_partial_manifest(root, post_id, {**manifest, "v1_media_progress": updated})
    return updated


def _confirmed_v1_ready(post: dict[str, Any], manifest: dict[str, Any], config: Config) -> bool:
    meta = post.get("meta") or {}
    digest = manifest_hash(manifest)
    return (
        post.get("status") == "pending"
        and meta.get(META_STATE) == STATE_READY
        and meta.get(META_READY_HASH) == digest
        and parse_manifest(meta.get(META_READY_MANIFEST)) == manifest
        and manifest_matches(post, manifest, digest, policy_version=config.policy_version)
    )


def _finish_v1_ready(
    client: Any, config: Config, root: Path, post: dict[str, Any], manifest: dict[str, Any],
) -> dict[str, Any]:
    post_id = int(post["id"])
    digest = manifest_hash(manifest)
    if post.get("status") != "pending" or not manifest_matches(
        post, manifest, digest, policy_version=config.policy_version,
    ):
        raise V1PartialResumeError("ready_readback_mismatch", post_id=post_id)
    client.update_post(post_id, {"meta": {
        **build_state_markers(STATE_READY, ready_hash=digest,
                              policy_version=config.policy_version, media_search_attempts=0),
        META_READY_MANIFEST: serialize_manifest(manifest),
    }})
    readback = client.get_post(post_id)
    if not _confirmed_v1_ready(readback, manifest, config):
        raise V1PartialResumeError("ready_readback_mismatch", post_id=post_id)
    _clear_processing_markers(root, post_id)
    return readback


def _resume_v1_final_write(
    client: Any, config: Config, root: Path, post: dict[str, Any],
) -> dict[str, Any] | None:
    post_id = int(post["id"])
    saved = _load_partial_manifest(root, post_id).get("v1_media_progress")
    if not isinstance(saved, dict) or not saved.get("ready_staged"):
        return None
    manifest = saved.get("ready_manifest")
    if (
        saved.get("post_id") != post_id or saved.get("source") != _v1_progress_source(post)
        or saved.get("source_title") != _post_title(post) or not isinstance(manifest, dict)
        or saved.get("intents")
    ):
        raise V1PartialResumeError("ready_readback_mismatch", post_id=post_id)
    if manifest_matches(post, manifest, manifest_hash(manifest), policy_version=config.policy_version):
        rows = saved.get("rows")
        if not isinstance(rows, list) or not all(
            _v1_progress_record_valid(row) and _verified_v1_progress_asset(client, config, post_id, row)
            for row in rows
        ):
            raise V1PartialResumeError("ready_readback_mismatch", post_id=post_id)
        _finish_v1_ready(client, config, root, post, manifest)
        return {
            "post_id": post_id, "status": "ready", "state": STATE_READY,
            "wordpress_changed": True, "ready_hash": manifest_hash(manifest),
            "final_write_reconciled": True,
        }
    if _v1_content_digest(_raw_content(post)) not in (saved.get("bodies") or []):
        raise V1PartialResumeError("ready_readback_mismatch", post_id=post_id)
    return None  # original pending body is intact; normal guarded replay is safe


def _is_webp_attachment_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and parsed.path.lower().endswith(".webp")


def _verify_v1_retained_bytes(
    config: Config, post_id: int, retained: list[dict[str, Any]], *, url_policy: str | None = None,
) -> None:
    # Media endpoint metadata can stay unchanged when an attachment's file is
    # replaced in place. Compare the bytes before committing budget or writing.
    with tempfile.TemporaryDirectory(prefix="unicornio-v1-retained-") as directory:
        for row in retained:
            try:
                downloaded = download_image(
                    row["media_url"], Path(directory) / f"{row['media_id']}.webp",
                    max_attempts=1, url_policy=url_policy or config.remote_url_policy,
                )
                digest = hashlib.sha256(Path(downloaded).read_bytes()).hexdigest()
            except (MediaDownloadError, OSError) as exc:
                raise V1PartialResumeError("partial_attachment_unverified", post_id=post_id) from exc
            if digest != row["sha256"].lower():
                raise V1PartialResumeError("partial_attachment_bytes_changed", post_id=post_id)


def _retained_partial_candidate(candidate: dict[str, Any], retained: list[dict[str, Any]]) -> bool:
    media_id = candidate.get("media_library_id") or candidate.get("media_id")
    url = _canonical_image_url(str(candidate.get("direct_image_url") or candidate.get("url") or ""))
    phash = str(candidate.get("phash") or "")
    return any(
        media_id == row["media_id"]
        or (phash and phash == row["phash"])
        or (url and url in {
            _canonical_image_url(str(row.get("media_url") or "")),
            _canonical_image_url(str(row.get("direct_image_url") or "")),
        })
        for row in retained
    )


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
        baseline_urls = value.get("baseline_inline_urls")
        if baseline_urls is None:
            expected_completed = len(completed_items)
        elif (
            isinstance(baseline_urls, list)
            and all(isinstance(url, str) and url for url in baseline_urls)
            and len(baseline_urls) == len(set(baseline_urls))
        ):
            accepted_urls = {_canonical_image_url(str(item.get("media_url") or "")) for item in completed_items}
            expected_completed = min(value["required"], len(set(baseline_urls) | (accepted_urls - {""})))
        else:
            expected_completed = -1
        if (
            any(not isinstance(item, dict) or not item.get("media_id") or not item.get("media_url") for item in accepted_items)
            or value["required"] < 0
            or value["completed"] < 0
            or value["completed"] != expected_completed
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
    *,
    media_fallback_candidates: list[dict[str, Any]] | None = None,
    on_work_started: Callable[[], None] | None = None,
    media_layout_source_signature: str | None = None,
) -> dict[str, Any]:
    """Apply an editorial result while exclusively owning the post."""
    assert_post_not_quarantined(root, post_id, operation="apply")
    with _acquire_post_lock(root, config, post_id) as lock_handle:
        guarded_client = _guard_post_client(client, lock_handle, root=root, post_id=post_id)
        return _apply_editorial_unlocked(
            guarded_client,
            config,
            root,
            post_id,
            payload,
            media_fallback_candidates=media_fallback_candidates,
            on_work_started=on_work_started,
            media_layout_source_signature=media_layout_source_signature,
        )


def _v1_checklist_media_context(
    editorial: dict[str, Any],
    content: str,
    accepted_media: list[dict[str, Any]],
) -> dict[str, Any]:
    """Provide checklist media facts, including item-scoped list identities."""
    seo = editorial.get("seo") if isinstance(editorial.get("seo"), dict) else {}
    title = str(seo.get("title") or editorial.get("title") or "")
    categories = editorial.get("categories") or ()
    media_rows = [dict(row) for row in accepted_media if isinstance(row, dict)]
    if detect_list_format(title, content, categories) is not None:
        recorded = {
            str(row.get("media_url") or "").strip(): row
            for row in media_rows
            if str(row.get("media_url") or "").strip()
        }
        scoped: list[dict[str, Any]] = []
        for asset in list_item_media_assets(title, content, categories):
            row = dict(asset)
            known = recorded.get(str(asset.get("media_url") or "").strip())
            if known:
                row.update(known)
            # The final HTML section remains the authority for item association;
            # keep its identity even when an older partial record lacks it.
            for key in (
                "subject", "item_title", "item_identity", "item_number",
                "item_group", "section_heading", "section_slot", "html_position",
            ):
                if asset.get(key) is not None:
                    row[key] = asset[key]
            scoped.append(row)
        media_rows = scoped
    context: dict[str, Any] = {"inline": {"accepted": media_rows}}
    featured = next((row for row in accepted_media if row.get("featured")), None)
    if featured:
        context["featured"] = {**featured, "status": "valid"}
    return context


def _apply_editorial_unlocked(
    client: WordPressClient,
    config: Config,
    root: Path,
    post_id: int,
    payload: dict[str, Any],
    *,
    media_fallback_candidates: list[dict[str, Any]] | None = None,
    on_work_started: Callable[[], None] | None = None,
    media_layout_source_signature: str | None = None,
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
    if media_layout_source_signature is not None and (
        _post_write_guard_signature(post) != media_layout_source_signature
        or not _is_v1_fresh_media_layout_source(post, root, post_id)
    ):
        raise WorkflowError("editorial media layout source changed; prepare the post again")
    resolved = resolve_post_eligibility(post, root)
    if not resolved.is_v2 and resolved.state == STATE_READY:
        saved = _load_partial_manifest(root, post_id).get("v1_media_progress")
        if isinstance(saved, dict):
            expected = saved.get("ready_manifest")
            if not isinstance(expected, dict) or saved.get("intents") or not _confirmed_v1_ready(
                post, expected, config,
            ):
                raise V1PartialResumeError("ready_readback_mismatch", post_id=post_id)
            if not config.dry_run:
                _clear_processing_markers(root, post_id)
        # A pending post can legitimately be READY while Publisher is paused.
        # Reapplying the same editorial would spend provider budget and could
        # duplicate uploads; the durable READY marker is already the result.
        return {
            "post_id": post_id,
            "status": "noop",
            "state": STATE_READY,
            "wordpress_changed": False,
            "idempotent": True,
        }
    try:
        eligibility = assert_v1_mutation_allowed(post, root, operation="apply")
    except RuntimeError as exc:
        raise WorkflowError(str(exc)) from exc
    if not config.dry_run:
        resumed = _resume_v1_final_write(client, config, root, post)
        if resumed is not None:
            return resumed
    editorial = validate_editorial(
        payload,
        min_confidence=config.min_relevance_confidence,
        media_source_policy=str(getattr(config, "media_source_policy", "strict")),
    )
    decision = editorial["site_relevance"]["decision"]
    confidence = float(editorial["site_relevance"].get("confidence") or 0.0)
    state_before = read_state(post)
    partial_path = _partial_manifest_path(root, post_id)
    if state_before.get("state") == STATE_PARTIAL:
        partial_manifest = _load_partial_manifest(root, post_id, required=True)
    else:
        partial_manifest = {}
    partial_resume = None
    prior_media = _partial_media_records(partial_manifest)
    media_progress = (
        verified_v1_media_progress(client, config, root, post, reconcile_uploads=True)
        if decision == "process" else None
    )
    if media_progress is None and not partial_manifest and partial_path.is_file():
        append_telemetry(root, "partial_manifest_orphaned", post_id=post_id)
    if media_progress is None and decision == "process" and state_before.get("state") == STATE_PARTIAL:
        partial_resume = verified_v1_partial_resume(client, root, post)
        if partial_resume is not None:
            if editorial.get("cleaned_html") is None:
                editorial = {**editorial, "cleaned_html": partial_resume["body"]}
            if _v1_partial_binding(post, editorial) != partial_resume["binding"]:
                raise V1PartialResumeError("partial_resume_editorial_changed", post_id=post_id)
            if not config.dry_run:
                _verify_v1_retained_bytes(config, post_id, partial_resume["accepted"])
        elif prior_media:
            # The only unbound accepted records the verifier permits are legacy
            # item-scoped lists. Do not move them onto an edited or flat body.
            saved_body = load_draft(root, post_id)["cleaned_html"]
            if editorial.get("cleaned_html") not in (None, saved_body):
                raise V1PartialResumeError("partial_resume_editorial_changed", post_id=post_id)
            editorial = {**editorial, "cleaned_html": saved_body}
    ordinary_slots = None
    game_names = split_game_names(editorial.get("game_name"))
    if decision == "process" and config.media_source_policy == "credit_only" and len(game_names) > 1:
        editorial = resolve_editorial_defaults(editorial, post)
        body = editorial["cleaned_html"]
        title = _post_title(post)
        categories = editorial.get("categories") or post.get("categories") or ()
        if not _v1_is_listicle(post, editorial, body):
            retained = (
                place_v1_retained_media(media_progress, editorial, title=title, categories=categories)[0]
                if media_progress is not None else prior_media
            )
            entities = extract_entities(title=title, content_html=body, game_name=editorial.get("game_name"))
            coverage, _ = _reconcile_partial_media(body, title, entities, retained)
            plan = editorial["media_plan"]
            if coverage["missing"] or not post.get("featured_media") or plan:
                retained_slots = tuple(row["paragraph_index"] for row in retained)
                try:
                    ordinary_slots = ordinary_game_media_slots(
                        body, game_names, source_html=f"{title} {_raw_content(post)}", retained_slots=retained_slots,
                    )
                    select_game_media_slots(ordinary_slots, coverage["missing"])
                    layout = normal_media_slot_capacity(body, retained_slots=retained_slots)
                    inline_slots = []
                    for item in plan:
                        if item.get("is_featured"):
                            continue
                        slot = item["paragraph_index"]
                        expected = ordinary_slots.get(slot)
                        placement = ("after_editorial_body"
                                     if layout["body_end_available"] and slot == layout["last_slot"] else None)
                        if (
                            expected is None or item.get("subject") not in {None, "", expected[0]}
                            or item.get("placement") != placement
                            or (str(item.get("target_id") or "").startswith("game-section-") and (
                                item.get("target_id") != f"game-section-{slot}"
                                or item.get("section_heading") != expected[1]
                            ))
                            or any(abs(slot - previous) < 3 for previous in inline_slots)
                        ):
                            raise MediaInsertionError(
                                "o alvo de mídia não corresponde à seção editorial atual",
                                code="multi_game_media_targets_unbound",
                            )
                        item["subject"] = expected[0]
                        inline_slots.append(slot)
                except MediaInsertionError as exc:
                    return {
                        "post_id": post_id, "status": "preflight_blocked",
                        "error_code": exc.code, "reason": str(exc), "wordpress_changed": False,
                    }
    if on_work_started is not None:
        on_work_started()
    attempts_before = eligibility.attempts
    append_telemetry(
        root, "apply_started", post_id=post_id,
        attempt=attempts_before + 1, first_pass=attempts_before == 0,
    )
    backup = SnapshotStore(root).save(post_id, post)
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

    progress_writes = None
    progress_featured = None
    if media_progress is not None:
        prior_media, progress_featured = place_v1_retained_media(
            media_progress, editorial, title=_post_title(post),
            categories=editorial.get("categories") or post.get("categories") or (),
        )
        partial_manifest = {}
    if not config.dry_run:
        progress_writes = _V1MediaProgress(client, config, root, post, editorial, media_progress)

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
    partial_featured = (
        {**progress_featured, "status": "valid"} if progress_featured
        else partial_manifest.get("featured") or {}
    )
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
    retain_credit_featured = (
        config.media_source_policy == "credit_only"
        and partial_featured.get("status") in {"valid", "verification_pending"}
    )
    retained_featured = ({**progress_featured, "retained": True, "attachment": client.get_media(
        progress_featured["media_id"],
    )} if progress_featured else None)
    if retain_credit_featured and not progress_featured:
        retained_id = partial_featured.get("media_id")
        if type(retained_id) is not int or retained_id <= 0:
            raise WorkflowError(f"featured_retained_identity_unverified: post {post_id}")
        # Check the authoritative attachment before any normalizer/upload.
        # One stable metadata snapshot is then used by every checklist gate.
        try:
            attachment = client.get_media(retained_id)
        except WordPressError as exc:
            if not known_wordpress_entity_missing(exc):
                raise WorkflowError(f"featured_retained_recheck_pending: post {post_id}") from exc
            partial_featured = {
                **partial_featured, "status": "vision_rejected",
                "reason": "anexo retido não existe mais no WordPress",
            }
            partial_manifest = {**partial_manifest, "featured": partial_featured}
            if not config.dry_run:
                _save_partial_manifest(root, post_id, partial_manifest)
            raise WorkflowError(f"featured_retained_attachment_missing: post {post_id}") from exc
        except (TimeoutError, OSError) as exc:
            raise WorkflowError(f"featured_retained_recheck_pending: post {post_id}") from exc
        if not isinstance(attachment, dict) or attachment.get("id") != retained_id:
            raise WorkflowError(f"featured_retained_identity_unverified: post {post_id}")
        retained_featured = {
            **partial_featured, "featured": True, "retained": True, "attachment": attachment,
        }
    proven_assets = prior_media + ([progress_featured] if progress_featured else [])
    if partial_manifest or media_progress is not None:
        remaining_plan = []
        for item in (editorial.get("media_plan") or []):
            if isinstance(item, dict) and item.get("is_featured") and (partial_featured_valid or retain_credit_featured):
                continue
            if isinstance(item, dict) and (
                (not item.get("is_featured") and item.get("paragraph_index") in partial_slots
                 and not item.get("item_identity"))
                or ((partial_resume is not None or media_progress is not None) and _retained_partial_candidate(item, proven_assets))
            ):
                continue
            remaining_plan.append(item)
        editorial = {**editorial, "media_plan": remaining_plan}
    if partial_resume is not None or media_progress is not None:
        media_fallback_candidates = [
            item for item in (media_fallback_candidates or [])
            if not _retained_partial_candidate(item, proven_assets)
        ]
    v1_vision_deadline = (
        time.monotonic() + config.media_operation_timeout_seconds
        if config.media_source_policy == "credit_only" and config.media_operation_timeout_seconds > 0
        else None
    )
    media_preflight = validate_media_plan(
        client,
        editorial,
        v1_vision_deadline=v1_vision_deadline,
        config=config,
        root=root,
        post_title=_post_title(post),
        post_id=post_id,
        v1_visual_fallback=True,
        post_categories=post.get("categories") or (),
        existing_featured_id=(
            None
            if partial_featured_rejected
            or retain_credit_featured
            or progress_featured
            else int(post.get("featured_media") or 0) or None
        ),
    )
    media_results, featured_id, featured_credit = _execute_media_plan(
        editorial, config, client, root, preflight=media_preflight, post_id=post_id,
        v1_vision_deadline=v1_vision_deadline,
        ordinary_game_slots=ordinary_slots,
        post_title=_post_title(post) or "",
        media_fallback_candidates=media_fallback_candidates,
        v1_visual_fallback=True,
        post_categories=post.get("categories") or (),
        **({"before_upload": progress_writes.begin, "on_accepted": progress_writes.accepted,
            "upload_rejected": progress_writes.no_write}
           if progress_writes is not None else {}),
        **({"featured_visual_asset": progress_featured} if progress_featured else {}),
        **({
            "previous_inline_phashes": tuple(str(row["phash"]) for row in prior_media),
            "previous_inline_visual_assets": [dict(row) for row in prior_media],
        } if partial_resume is not None or media_progress is not None else {}),
    )
    if progress_writes is not None and progress_writes.record["intents"]:
        return {
            "post_id": post_id, "status": "retry", "retryable": True,
            "wordpress_changed": False, "error_code": "retained_media_upload_uncertain",
            "media_plan_results": media_results,
        }
    combined_media_results = prior_media + [
        item for item in media_results
        if item.get("media_url") and not item.get("featured")
    ]
    if featured_id is None and retained_featured:
        featured_id = retained_featured["media_id"]
        featured_credit = str(retained_featured.get("credit_text") or "") or None
    elif featured_id is None and partial_featured_valid:
        featured_id, featured_credit = _recover_partial_featured(
            client, post_id, partial_manifest
        )
    v1_original_cover = config.media_source_policy == "credit_only" and bool(post.get("featured_media"))
    original_cover_receipt = None
    original_cover_check = next((
        row for row in media_preflight.get("featured_vision", [])
        if v1_original_cover and row.get("existing_featured_id") == post["featured_media"]
    ), {})
    if featured_id is None and not config.dry_run and not partial_featured_rejected:
        if v1_original_cover:
            approved_sha = original_cover_check.get("image_sha256") if original_cover_check.get("status") == "passed" else None
            if approved_sha:
                featured_id = _normalize_existing_featured(
                    client, config, post, editorial, root=root, approved_sha=approved_sha,
                    progress=progress_writes,
                )
                if featured_id:
                    original_cover_receipt = {
                        "featured": True, "media_id": featured_id, "sha256": approved_sha,
                        "vision_evidence": {
                            "image_role": "featured", "verdict": "accept", "image_sha256": approved_sha,
                        },
                    }
        else:
            featured_id = _normalize_existing_featured(client, config, post, editorial, root=root)
    html = editorial["cleaned_html"]
    if combined_media_results and not config.dry_run:
        plan = [
            {
                "paragraph_index": result["paragraph_index"],
                "placement": result.get("placement"),
                "media_url": result["media_url"],
                "alt_text": result["alt_text"],
                "credit_text": result["credit_text"],
                "width": result.get("width"),
                "height": result.get("height"),
                "target_id": result.get("target_id"),
                "item_identity": result.get("item_identity"),
                "item_title": result.get("item_title"),
                "item_group": result.get("item_group"),
                "item_number": result.get("item_number"),
                "section_heading": result.get("section_heading"),
                "section_slot": result.get("section_slot"),
                "html_position": result.get("html_position"),
                "subject": result.get("subject"),
            }
            for result in combined_media_results
            if result.get("media_url") and not result.get("featured")
        ]
        if plan:
            html = insert_media(html, plan, listicle=_v1_is_listicle(post, editorial, html))
    editorial_with_media = {**editorial, "cleaned_html": html}
    content, trailer, trailer_status = compose_final_content(
        editorial_with_media, config, original_link_of(post), root=root
    )
    editorial_with_media = attach_trailer_audit(
        editorial_with_media, trailer, search_status=trailer_status, root=root
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
    # Use the same cleaned body for the checklist, manifest, and WordPress write.
    # Do not remove paragraph boundaries after using positional media plans.
    # Fresh assembled plans already remove blank spacers before slot selection.
    if not editorial.get("media_plan") and not prior_media:
        content = remove_empty_paragraphs(content)
    if not config.dry_run:
        editorial_with_media = {**editorial_with_media, "cleaned_html": content}
    checklist_media_context = _v1_checklist_media_context(
        {
            **editorial_with_media,
            "title": _post_title(post),
            "categories": editorial.get("categories") or post.get("categories") or (),
        },
        content,
        [row for row in (retained_featured, original_cover_receipt) if row]
        + [*prior_media, *media_results],
    )
    # Tentativas anteriores (antes desta): base do teto deterministico de
    # buscas de imagem. Cada apply falho = 1 busca completa esgotada.
    checklist = run_pre_publish_checklist(
        post={
            **post,
            "featured_media": (
                featured_id
                if featured_id is not None
                else (None if partial_featured_rejected or v1_original_cover else post.get("featured_media"))
            ),
        },
        editorial=editorial_with_media,
        content=content,
        backup_path=backup,
        config=config,
        client=client,
        attempts=attempts_before,
        media_context=checklist_media_context,
        v1_visual_featured=True,
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
                        if (
                            config.media_source_policy == "credit_only"
                            and item.get("name") in {"destaque_relevancia", "destaque_1280x720"}
                        ) or any(token in str(item.get(key) or "").casefold()
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
                retained_featured_needs_recheck = bool(
                    retained_featured
                    and any(row["name"] == "destaque_relevancia" and row.get("retryable") for row in failed_items)
                    and not any(row["name"] in {"destaque_1280x720", "imagem_destaque"} for row in failed_items)
                )
                original_cover_pending = original_cover_check.get("status") == "deferred"
                if original_cover_pending:
                    featured_manifest = {"status": "missing", "reason": original_cover_check["reason"]}
                elif retained_featured_needs_recheck:
                    featured_manifest.update(
                        status="verification_pending",
                        reason="revalidação do anexo de destaque retido pendente",
                    )
                elif featured_vision_failure:
                    failed_index = featured_vision_failure.get("index")
                    candidate = (
                        (editorial.get("media_plan") or [])[failed_index]
                        if isinstance(failed_index, int)
                        and 0 <= failed_index < len(editorial.get("media_plan") or [])
                        else {}
                    )
                    rejected_id = (candidate.get("media_library_id") or featured_id
                                   or featured_vision_failure.get("existing_featured_id")
                                   or featured_manifest.get("media_id"))
                    rejected_url = str(
                        candidate.get("direct_image_url")
                        or (current_featured or {}).get("media_url")
                        or featured_manifest.get("media_url")
                        or ""
                    )
                    if retained_featured and retained_featured["media_id"] == rejected_id:
                        rejected_url = str(retained_featured["attachment"].get("source_url") or rejected_url)
                    elif isinstance(rejected_id, int) and rejected_id > 0:
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
                    featured_manifest.pop("reason", None)
                    featured_manifest["media_id"] = featured_id
                    featured_manifest["media_url"] = (current_featured or {}).get("media_url") or featured_manifest.get("media_url", "")
                    featured_manifest["credit_text"] = featured_credit or featured_manifest.get("credit_text", "")
                    if config.media_source_policy == "credit_only" and current_featured:
                        featured_manifest.pop("sha256", None)
                        featured_manifest.pop("vision_evidence", None)
                    visual_receipt = checklist.get("confirmed_v1_featured")
                    if isinstance(visual_receipt, dict) and visual_receipt.get("media_id") == featured_id:
                        featured_manifest.update(visual_receipt)
                images_summary, media_drift = _reconcile_partial_media(
                    editorial["cleaned_html"],
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
                elif featured_status in {"rejected", "vision", "failed", "vision_rejected", "verification_pending"}:
                    partial_kind = "featured_vision"
                elif featured_status != "valid":
                    partial_kind = "featured_missing"
                else:
                    partial_kind = "inline_missing"
                baseline_inline_urls = _partial_relevant_urls(editorial["cleaned_html"], image_entities)
                previous_completed = min(
                    required, len(baseline_inline_urls | _partial_accepted_urls(prior_media))
                )
                featured_before_valid = (
                    (partial_manifest.get("featured") or {}).get("status") in {"valid", "verification_pending"}
                )
                featured_progress = bool(featured_id and featured_status == "valid" and not featured_before_valid)
                progress = completed > previous_completed or featured_progress
                state_info = read_state(post)
                passes = state_info.get("processing_passes", 0) + 1
                no_progress_before = state_info.get("no_progress_attempts", 0)
                no_progress = (
                    no_progress_before if (retained_featured_needs_recheck or original_cover_pending) and not progress
                    else _next_partial_no_progress(
                        no_progress_before, progress=progress, media_results=media_results,
                    )
                )
                operational_vision = original_cover_pending or retained_featured_needs_recheck or any(
                    item.get("status") == "deferred" and item.get("reason_code") in {
                        "vision_provider_error", "vision_budget_exhausted",
                    }
                    for item in media_results
                )
                partial_retry = _partial_retry(
                    no_progress,
                    cooldown_minutes=(
                        max(120, config.rework_cooldown_minutes)
                        if operational_vision else config.rework_cooldown_minutes
                    ),
                    max_no_progress_attempts=config.max_partial_no_progress_attempts,
                )
                partial_state = partial_retry["state"]
                manifest = {
                    "state": "partial",
                    "kind": "media",
                    "required": required,
                    "completed": completed,
                    "missing": missing,
                    "partial_kind": partial_kind,
                    "accepted_media": accepted,
                    "baseline_inline_urls": sorted(baseline_inline_urls),
                    **(
                        {"resume_binding": _v1_partial_binding(post, editorial)}
                        if accepted and not _v1_is_listicle(
                            post, editorial, editorial["cleaned_html"],
                        ) and (not prior_media or partial_resume is not None)
                        else {}
                    ),
                    "featured": featured_manifest,
                    "processing_passes": passes,
                    "no_progress_attempts": no_progress,
                }
                # Ordinary PARTIAL keeps its established stricter contract.
                # Once a mixed rework is active, carry the independent journal
                # over when that post has only media blockers left.
                if media_progress is not None and progress_writes is not None and progress_writes.record["rows"]:
                    manifest["v1_media_progress"] = progress_writes.record
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
                    "trailer_search_status": trailer_status,
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
            # A separate editorial blocker (e.g. trailer) still consumes a
            # rework attempt. It cannot turn a deferred visual check into an
            # exhausted media search, even when the prior count is at its cap.
            exhausted_media_round = media_failure and not any(
                _is_deferred_media_verification(item) for item in media_results
            )
            if exhausted_media_round:
                media_search_attempts += 1
            deterministic_exhausted = (
                exhausted_media_round
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
                rework_attempt = attempts
                if progress_writes is not None and progress_writes.record["rows"]:
                    # Preserve the existing rate limits; only durable new assets
                    # or a formerly failed gate now passing count as progress.
                    previous_ids = {row["media_id"] for row in (media_progress or {}).get("rows", [])}
                    new_assets = any(row["media_id"] not in previous_ids for row in progress_writes.record["rows"])
                    previous_failures = set((media_progress or {}).get("failed_gates", []))
                    passed = {item["name"] for item in checklist["items"] if item.get("status") == "pass"}
                    repaired = (previous_failures & passed) - set((media_progress or {}).get("repaired_gates", []))
                    progress_writes.record["failed_gates"] = sorted({item["name"] for item in failed_items})
                    progress_writes.record["repaired_gates"] = sorted(
                        set((media_progress or {}).get("repaired_gates", [])) | repaired
                    )
                    no_progress = 0 if new_assets or repaired else state_info["no_progress_attempts"] + 1
                    progress_writes.record["no_progress"] = no_progress
                    progress_writes.save()
                    rework_attempt = max(1, no_progress)
                backoff = rework_backoff(
                    rework_attempt,
                    cooldown_minutes=config.rework_cooldown_minutes,
                    max_attempts=config.max_rework_attempts,
                )
                backoff["attempts"] = attempts
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
                no_progress_attempts=(
                    progress_writes.record.get("no_progress") if progress_writes is not None else None
                ),
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
                if retained_featured or v1_original_cover:
                    # Human routing may proceed, but unconfirmed cover bytes
                    # must not be associated with the WordPress post as a cover.
                    cover_receipt = checklist.get("confirmed_v1_featured") or {}
                    awaiting_featured_id = featured_id if (
                        cover_receipt.get("media_id") == featured_id
                        and not any(row["name"] in {
                            "imagem_destaque", "destaque_1280x720", "destaque_relevancia",
                            "imagens_visao", "imagens_webp", "featured_inline_position",
                        } for row in failed_items)
                    ) else None
                else:
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
                "trailer_search_status": trailer_status,
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
            "trailer_search_status": trailer_status,
            "media_plan_results": media_results,
            "checklist": checklist,
            "images": _images_summary(content, _post_title(post) or editorial["seo"]["title"], image_entities),
        }

    latest = client.get_post(post_id)
    _require_pending(latest)
    if _post_write_guard_signature(latest) != _post_write_guard_signature(post):
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "dry_run": False,
            "status": "retry",
            "retryable": True,
            "trailer_search_status": trailer_status,
            "reason": "WordPress post changed during apply preflight; refusing stale overwrite",
        }
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
            **build_meta(editorial["seo"]),
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
    final_readback = progress_writes is not None and bool(progress_writes.record["rows"])
    if final_readback:
        assert progress_writes is not None
        progress_writes.record.update(ready_manifest=manifest, ready_staged=media_progress is not None)
        progress_writes.save()
    if final_readback and media_progress is not None:
        staged_payload = {
            **update_payload,
            "meta": {**build_meta(editorial["seo"]), **build_processing_markers(
                editorial["site_relevance"]["decision"],
                editorial["site_relevance"]["confidence"],
            )},
        }
        # Do not expose READY to the Publisher until the new HTML and its
        # manifest inputs have been confirmed in WordPress.
        client.update_post(post_id, staged_payload)
        result = _finish_v1_ready(client, config, root, client.get_post(post_id), manifest)
    else:
        result = client.update_post(post_id, update_payload)
        if final_readback:
            result = client.get_post(post_id)
            if not _confirmed_v1_ready(result, manifest, config):
                raise V1PartialResumeError("ready_readback_mismatch", post_id=post_id)
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
        "trailer_search_status": trailer_status,
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
    if root is None:
        raise QuarantinePolicyError("quarantine policy root is required before a WordPress state write")
    state_markers = build_state_markers(
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
    # Repeat the caller's preflight at the lowest shared legacy metadata
    # writer, after payload construction and immediately before the REST call.
    assert_post_not_quarantined(root, post_id, operation="state_marker_write")
    try:
        client.update_post(post_id, {"meta": state_markers})
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
    relevante é baixada, convertida (transparência achatada,
    largura limitada a 1280px), re-upload como NOVO attachment preservando
    alt e legenda opcional, e a URL trocada no conteúdo — o WebP publicado nunca é
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


def _partial_relevant_urls(content: str, entities: set[str] | None) -> set[str]:
    return {
        _canonical_image_url(str(item.get("src") or ""))
        for item in iter_content_images(content)
        if item.get("src") and image_is_relevant(
            alt_text=str(item.get("alt") or ""),
            credit_text=str(item.get("caption") or ""),
            source_url=str(item.get("src") or ""),
            entities=entities or set(),
        )
    } - {""}


def _partial_accepted_urls(rows: list[dict[str, Any]] | None) -> set[str]:
    return {
        _canonical_image_url(str(row.get("media_url") or ""))
        for row in (rows or []) if isinstance(row, dict) and not row.get("featured")
    } - {""}


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
    content_urls = _partial_relevant_urls(content, entities)
    accepted_urls = _partial_accepted_urls(accepted_media)
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



def _expected_subject(
    item: dict[str, Any], editorial: dict[str, Any] | None, *, v1_categories: bool = False,
) -> str:
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
    numerados = extract_list_items(
        str((editorial.get("seo") or {}).get("title") or ""), html,
        (editorial.get("categories") or ()) if v1_categories else (),
    )
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
        return ""
    candidatos = [item.heading for item in numerados if item.heading_start <= limite]
    return candidatos[-1] if candidatos else ""


def _item_entities(
    item: dict[str, Any],
    entities: set[str],
    editorial: dict[str, Any] | None = None,
) -> set[str]:
    """Entidades que valem para ESTE item (P1 da auditoria).

    Listicle items remain bound to their section-specific subject. For ordinary
    posts, an explicit game name or concrete focus keyword supplies the shared
    canonical identity; legacy/generic inputs retain the declared subject or
    article entity set.
    """
    declarado = " ".join(str(item.get("subject") or "").split()).strip().lower()
    if bool(item.get("is_featured")) or str(item.get("role") or "").casefold() == "featured":
        editorial_payload = editorial if isinstance(editorial, dict) else {}
        seo_value = editorial_payload.get("seo")
        seo = seo_value if isinstance(seo_value, dict) else {}
        generic_focus = {"game", "games", "jogo", "jogos", "videogame", "anime", "filme", "series"}
        global_subject = " ".join(str(
            editorial_payload.get("game_name") or seo.get("focus_keyword") or ""
        ).split()).strip().casefold()
        if global_subject and global_subject not in generic_focus:
            return {global_subject} | {
                entity.casefold() for entity in entities
                if entity and entity.casefold() in global_subject
            }
        declared_matches = sorted(
            (entity.casefold() for entity in entities if entity and entity.casefold() in declarado),
            key=len,
            reverse=True,
        )
        return {declared_matches[0]} if declared_matches else set(entities)
    expected_subject = _expected_subject(item, editorial)
    if expected_subject:
        # Specific section identities stay item-scoped. Generic headings such
        # as "Jogo 1" do not replace the article's concrete identity (and must
        # not make every source image fail relevance).
        normalized_expected = expected_subject.casefold()
        if any(
            entity.casefold() in normalized_expected
            or normalized_expected in entity.casefold()
            for entity in entities if entity
        ):
            declarado = normalized_expected
        else:
            return set(entities)
    elif isinstance(editorial, dict):
        is_listicle = len(extract_list_items(
            str((editorial.get("seo") or {}).get("title") or ""),
            str(editorial.get("cleaned_html") or ""),
        )) >= 2
        if is_listicle:
            return set()
        # Ordinary posts share the same canonical subject as V2 discovery and
        # featured vision. Listicle items keep their section-specific identity.
        seo = editorial.get("seo") if isinstance(editorial.get("seo"), dict) else {}
        focus = str(seo.get("focus_keyword") or "").strip()
        generic_focus = {"game", "games", "jogo", "jogos", "videogame", "anime", "filme", "series"}
        if editorial.get("game_name") or (focus and focus.casefold() not in generic_focus):
            from .media.vision_policy import resolve_editorial_identity

            canonical = str(
                resolve_editorial_identity(editorial).get("primary_subject") or ""
            ).strip().casefold()
            if canonical:
                declarado = canonical
    # The section mapping is used only when the numbered list structure is
    # unambiguous; otherwise keep the normal-post identity path above.
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
    from .media.evidence_attestation import verify_deterministic_media

    if verify_deterministic_media(item):
        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        fields = evidence.get("evidence") if isinstance(evidence.get("evidence"), dict) else {}
        signed_subject = str(evidence.get("subject") or "").strip()
        target_entities = _item_entities(item, entities, editorial)
        if signed_subject and any(
            signed_subject.casefold() == str(entity).strip().casefold()
            for entity in target_entities if entity
        ):
            # Re-run the common relevance predicate over origin-sourced fields
            # sealed by the runtime. Search query, generated ALT and credit are
            # deliberately excluded; the byte/source gate still runs at apply.
            return image_is_relevant(
                alt_text=" ".join(str(fields.get(key) or "") for key in (
                    "alt_original", "figcaption", "heading", "og_title", "page_title",
                )),
                credit_text="",
                source_url=" ".join(str(fields.get(key) or "") for key in (
                    "filename", "page_url",
                )) + " " + str(item.get("direct_image_url") or ""),
                search_query="",
                entities=target_entities,
            )
    if str(item.get("provenance_mode") or "") == "credit_only":
        direct_url = str(item.get("direct_image_url") or "").strip()
        credit = str(item.get("credit_text") or "").strip()
        if not direct_url or not credit:
            return False
        return image_is_relevant(
            alt_text="",
            credit_text=credit,
            source_url=direct_url,
            search_query="",
            entities=_item_entities(item, entities, editorial),
            source_only=False,
        )
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


def _structured_vision_evidence(
    row: dict[str, Any], *, image_role: str, model_identity: str = ""
) -> dict[str, Any] | None:
    """Create a compact decision record linked to the evaluated image bytes."""
    decision = row.get("decision")
    if not isinstance(decision, dict):
        return None
    result = {
        key: decision[key]
        for key in (
            "status", "confidence", "visual_type", "relation", "editorial_fit",
            "role_fit", "dimensions_source", "verdict", "detail_used",
            "low_status", "low_confidence", "low_visual_type",
        )
        if key in decision
    }
    result.update({
        "image_sha256": str(row.get("image_sha256") or ""),
        "image_role": image_role,
        "policy_version": VISION_POLICY_VERSION,
        "decision_source": (
            "deterministic_evidence" if row.get("deterministic")
            else "cache" if row.get("cached")
            else "vision_provider"
        ),
        "model_identity": model_identity,
        "context_key": str(row.get("context_key") or ""),
        "subject": str(row.get("subject") or ""),
    })
    return result


def _vision_evidence_matches_item(
    item: dict[str, Any],
    editorial: dict[str, Any] | None,
    config: Config | None,
    *,
    image_sha256: str | None = None,
) -> bool:
    """Validate an inline vision decision against policy/model/subject/bytes."""
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    decision = evidence.get("vision_decision") if isinstance(evidence.get("vision_decision"), dict) else {}
    if not decision or config is None:
        return False
    from .media.vision_cache import vision_model_identity

    expected_sha = str(evidence.get("vision_image_sha256") or decision.get("image_sha256") or "")
    expected_model = vision_model_identity(config.vision_base_url, config.vision_model)
    expected_subject = str(item.get("subject") or "").strip().casefold()
    expected_context = featured_vision_context_key(editorial or {})
    try:
        confidence = float(decision.get("confidence") or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        evidence.get("vision_policy_version") == VISION_POLICY_VERSION
        and decision.get("policy_version") == VISION_POLICY_VERSION
        and decision.get("image_role") == "inline"
        and decision.get("verdict") == "accept"
        and decision.get("status") == "MATCH"
        and confidence >= 0.85
        and expected_sha
        and decision.get("image_sha256") == expected_sha
        and (image_sha256 is None or expected_sha == image_sha256)
        and expected_subject
        and str(decision.get("subject") or "").strip().casefold() == expected_subject
        and decision.get("model_identity") == expected_model
        and decision.get("context_key") == expected_context
    )


def _featured_preflight_evidence_matches(
    row: dict[str, Any],
    editorial: dict[str, Any],
    config: Config,
    *,
    image_sha256: str,
    context_key: str = "",
) -> bool:
    """Only reuse a featured preflight decision bound to this exact context."""
    evidence = row.get("vision_evidence")
    if not isinstance(evidence, dict):
        return False
    expected = {
        "image_sha256": image_sha256,
        "image_role": "featured",
        "policy_version": VISION_POLICY_VERSION,
        "model_identity": vision_model_identity(config.vision_base_url, config.vision_model),
        "context_key": context_key or featured_vision_context_key(editorial),
        "subject": featured_vision_subject(editorial),
    }
    return bool(
        evidence.get("status") == "MATCH"
        and evidence.get("verdict") == "accept"
        and all(evidence.get(key) == value for key, value in expected.items())
    )


def _revalidated_origin_page_relevance(
    item: dict[str, Any],
    entities: set[str],
    editorial: dict[str, Any] | None,
    *,
    config: Config | None,
    root: Path | None,
    source_page_cache: dict[str, list[str] | None],
    source_html_cache: dict[str, str],
) -> bool:
    """Re-fetch the source and score its own context for serialized V1 plans.

    Resolver evidence is retained for audit, but is not trusted by itself after
    crossing a process/JSON boundary. The consuming process independently
    verifies that the direct asset is still listed by the origin and computes
    relevance from current origin HTML (never from generated ALT/credit/query).
    """
    page_url = str(item.get("source_page_url") or "").strip()
    image_url = str(item.get("direct_image_url") or "").strip()
    evidence = item.get("origin_evidence")
    item_subject = str(
        item.get("subject")
        or (evidence.get("subject") if isinstance(evidence, dict) else "")
        or ""
    ).strip()
    if (
        str(item.get("provenance_mode") or "") != "source_page"
        or not page_url
        or not image_url
        or not item_subject
    ):
        return False
    from .media.source_verify import validate_discovered_candidate
    from .media.evidence import evidence_score, source_context

    target_entities = _item_entities(item, entities, editorial)
    subject = next(
        (
            str(value).strip()
            for value in target_entities
            if str(value).strip().casefold() == item_subject.casefold()
        ),
        "",
    )
    if not subject:
        return False

    try:
        verification = validate_discovered_candidate(
            {"source_page_url": page_url, "direct_image_url": image_url},
            cache=source_page_cache,
            cache_html=source_html_cache,
            subject=subject,
            audit=None,
            url_policy=str(getattr(config, "remote_url_policy", "audit") or "audit"),
        )
    except Exception:  # noqa: BLE001 - inability to revalidate is fail-closed
        return False
    if verification.get("valid") is not True:
        return False
    html = source_html_cache.get(page_url)
    if not isinstance(html, str) or not html:
        return False
    context = source_context(
        html,
        str(verification.get("matched_image_url") or image_url),
        base_url=page_url,
    )
    scored = evidence_score(
        subject,
        filename=context.get("filename", ""),
        og_title=context.get("og_title", ""),
        page_title=context.get("page_title", ""),
        alt_original=context.get("alt_original", ""),
        figcaption=context.get("figcaption", ""),
        heading=context.get("heading", ""),
        page_url=page_url,
        query="",
        source_page_present=True,
        image_in_source=True,
        source_verification_reason=str(verification.get("reason_code") or ""),
        source_verification_recoverable=bool(verification.get("recoverable")),
        source_comparison_proved=bool(verification.get("source_comparison_proved")),
    )
    return scored.get("verdict") == "deterministic_match"


def _v1_visual_subject(
    item: dict[str, Any], editorial: dict[str, Any], ordinary_game_slots: dict[int, tuple[str, str]] | None = None,
) -> str:
    if not item.get("is_featured"):
        seo = editorial.get("seo") or {}
        if detect_list_format(
            str(editorial.get("title") or seo.get("title") or ""),
            str(editorial.get("cleaned_html") or ""), editorial.get("categories") or (),
        ) is not None:
            return _expected_subject(item, editorial, v1_categories=True)
        if len(split_game_names(editorial.get("game_name"))) > 1:
            return (ordinary_game_slots or {}).get(item.get("paragraph_index"), ("", ""))[0]
    return featured_vision_subject(editorial)


def _v1_needs_pixel_evidence(
    item: dict[str, Any], config: Config | None, enabled: bool,
) -> bool:
    # The installed site policy permits unknown origin without fabricating
    # provenance, but only a trusted visual decision can approve pixels for
    # this editorial use, even when they already exist in the Media Library.
    return bool(enabled and config is not None and config.media_source_policy == "credit_only")


def _media_item_rejection(
    item: dict[str, Any],
    entities: set[str],
    client: WordPressClient,
    attachment_cache: dict[int, dict[str, Any]],
    editorial: dict[str, Any] | None = None,
    root: Path | None = None,
    config: Config | None = None,
    source_page_cache: dict[str, list[str] | None] | None = None,
    source_html_cache: dict[str, str] | None = None,
    v1_visual_fallback: bool = False,
) -> str | None:
    """Motivo de rejeicao de um item do media_plan, ou None se valido.

    Compartilhada pelo ``_execute_media_plan`` (apply) e pelo
    ``validate_media_plan`` (media-validate, 1 chamada antes do apply):
    reuso da Media Library não depende de legenda no attachment; featured deve
    retratar o assunto citado pela evidencia real (arquivo/pagina de origem);
    inline deve referenciar entidade distintiva do post.
    """
    is_featured = bool(item.get("is_featured"))
    media_id = item.get("media_library_id")
    needs_pixels = _v1_needs_pixel_evidence(item, config, v1_visual_fallback)
    if needs_pixels and media_id:
        # Only fetch authoritative REST metadata inside the bounded candidate worker.
        return None
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
    if needs_pixels:
        return None  # provisional; only the real APPLY vision result can approve
    if attachment is not None:
        title = str((attachment.get("title") or {}).get("rendered") or "")
        alt = str(attachment.get("alt_text") or "")
        caption = str((attachment.get("caption") or {}).get("rendered") or "")
        url = str(attachment.get("source_url") or "")
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
        if _revalidated_origin_page_relevance(
            item,
            entities,
            editorial,
            config=config,
            root=root,
            source_page_cache=source_page_cache if source_page_cache is not None else {},
            source_html_cache=source_html_cache if source_html_cache is not None else {},
        ):
            return None
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
    if not is_featured and _vision_evidence_matches_item(item, editorial, config):
        # This is provisional until APPLY downloads/converts the source and
        # compares its actual SHA-256 with the recorded vision input.
        if not _item_evidence_relevant(item, entities, editorial):
            return None
    # P1 (auditoria): alt/credit/search_query escritos pelo AGENTE não são
    # evidência — uma imagem errada com alt "Bleach anime" passava no apply
    # embora a descoberta já a tivesse rejeitado (o agente provava a si mesmo).
    # Aplicado aqui: só a evidência de ORIGEM (arquivo + página) e o subject que
    # a descoberta carimbou no item (entidades LOCAIS, não o conjunto do artigo).
    if (
        not _item_evidence_relevant(item, entities, editorial)
        and not _revalidated_origin_page_relevance(
            item,
            entities,
            editorial,
            config=config,
            root=root,
            source_page_cache=source_page_cache if source_page_cache is not None else {},
            source_html_cache=source_html_cache if source_html_cache is not None else {},
        )
    ):
        listed = ", ".join(sorted(_item_entities(item, entities, editorial))) or "nenhuma"
        return f"imagem sem relacao com o conteudo (entidades distintas: {listed})"
    return None


def _is_deferred_media_verification(result: dict[str, Any]) -> bool:
    return (
        isinstance(result, dict) and result.get("status") == "deferred"
        and result.get("reason_code") in {
            "visual_identity_pending", "vision_provider_error", "vision_budget_exhausted",
        }
    )


def _next_partial_no_progress(
    current: int, *, progress: bool, media_results: list[dict[str, Any]],
) -> int:
    """Do not exhaust media retries for a visual-comparison outage."""
    if progress:
        return 0
    if any(_is_deferred_media_verification(item) for item in media_results):
        return max(0, int(current or 0))
    return max(0, int(current or 0)) + 1


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
    v1_visual_fallback: bool = False,
    post_categories: Any = (),
    v1_vision_deadline: float | None = None,
) -> dict[str, Any]:
    """Valida o media_plan de um editorial SEM executar download/upload.

    Retorna ``{valid, rejected: [{index, reason}]}`` — o agente corrige o
    plano antes do apply (1 chamada compacta em vez de aplicar e ver itens
    rejeitados no resultado). Deterministico e somente leitura.
    """
    plan = editorial.get("media_plan") or []
    v1_feature_policy = bool(v1_visual_fallback and config is not None and config.media_source_policy == "credit_only")
    vision_editorial = {
        **editorial,
        **({"title": post_title} if post_title else {}),
        **({"categories": post_categories} if v1_visual_fallback and post_categories else {}),
    }
    entities = extract_entities(
        title=str(vision_editorial.get("title") or (editorial.get("seo") or {}).get("title") or ""),
        content_html=str(vision_editorial.get("cleaned_html") or ""),
        focus_keyword=str((vision_editorial.get("seo") or {}).get("focus_keyword") or ""),
        game_name=vision_editorial.get("game_name"),
    )
    cache: dict[int, dict[str, Any]] = {}
    valid = 0
    rejected: list[dict[str, Any]] = []
    featured_vision: list[dict[str, Any]] = []
    vision_pending: list[int] = []
    seen_sources: set[str] = set()
    source_page_cache: dict[str, list[str] | None] = {}
    source_html_cache: dict[str, str] = {}
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
                engine=_diagnostic_media_engine(item),
                candidate_id=str(item.get("candidate_id") or ""),
                source_domain=(
                    urlparse(str(item.get("direct_image_url") or "")).hostname or ""
                ).lower(),
            )
        reason = _media_item_rejection(
            item,
            entities,
            client,
            cache,
            vision_editorial,
            root=root,
            config=config,
            source_page_cache=source_page_cache,
            source_html_cache=source_html_cache,
            v1_visual_fallback=v1_visual_fallback,
        )
        if reason is None:
            reason = _duplicate_source_reason(plan, index, seen_sources)
        needs_pixels = reason is None and _v1_needs_pixel_evidence(
            item, config, v1_visual_fallback,
        )
        if needs_pixels:
            vision_pending.append(index)
        if reason is None and not needs_pixels and bool(item.get("is_featured")):
            vision = _validate_featured_candidate_vision(
                item, vision_editorial, client, cache, config=config, root=root,
                v1_feature_policy=v1_feature_policy, post_id=post_id,
            )
            vision_row = {
                "index": index,
                "subject": featured_vision_subject(vision_editorial),
                "category": featured_vision_category(vision_editorial),
                "context_key": (
                    v1_vision_context_key(vision_editorial, post_id) if v1_feature_policy
                    else featured_vision_context_key(vision_editorial)
                ),
                "cache_hit": bool(vision.get("cached")),
                **vision,
            }
            if config is not None:
                from .media.vision_cache import vision_model_identity
                vision_row["vision_evidence"] = _structured_vision_evidence(
                    vision_row,
                    image_role="featured",
                    model_identity=vision_model_identity(config.vision_base_url, config.vision_model),
                )
            featured_vision.append(vision_row)
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
        elif not needs_pixels:
            valid += 1
        if root is not None:
            append_telemetry(
                root,
                "media_funnel",
                stage="preflight",
                status="rejected" if reason else "pending" if needs_pixels else "passed",
                item_index=index,
                featured=bool(item.get("is_featured")),
                post_id=post_id,
                query=str(item.get("search_query") or item.get("query") or ""),
                subject=str(item.get("subject") or ""),
                role=str(item.get("role") or ("featured" if item.get("is_featured") else "inline")),
                engine=_diagnostic_media_engine(item),
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
            existing_item, vision_editorial, client, cache, config=config, root=root,
            v1_feature_policy=v1_feature_policy, post_id=post_id, v1_vision_deadline=v1_vision_deadline,
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
                **({
                    "reason_code": str(vision.get("reason_code") or ""),
                    "vision_attempted": bool(vision.get("vision_calls")),
                    "vision_status": (vision.get("decision") or {}).get("status"),
                    "vision_confidence": (vision.get("decision") or {}).get("confidence"),
                } if v1_feature_policy else {}),
            )
    not_verified = {row["index"] for row in rejected} | set(vision_pending)
    accepted = set(range(len(plan))) - not_verified
    listicle = _listicle_media_capacity(
        vision_editorial, entities, accepted, post_title=post_title,
        v1_categories=v1_visual_fallback,
    )
    return {
        "valid": valid,
        "rejected": rejected,
        "listicle": listicle,
        "featured_vision": featured_vision,
        **({"vision_pending": vision_pending} if vision_pending else {}),
    }


def _existing_v1_featured_pixels(source: Path, destination: Path) -> Path:
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(source) as image:
            if image.format == "WEBP" and image.size == (1280, 720):
                image.load()
                return source
    except (UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise MediaConversionError(
            "featured image bytes are not usable", reason_code="invalid_image_format",
            failure_kind="decode_error",
        ) from exc
    except OSError as exc:
        if exc.errno is not None:  # real I/O failure, not a decoder failure
            raise
        raise MediaConversionError(
            "featured image bytes could not be decoded", reason_code="image_decode_failed",
            failure_kind="decode_error",
        ) from exc
    return prepare_featured_webp(source, destination)


def _validate_featured_candidate_vision(
    item: dict[str, Any],
    editorial: dict[str, Any],
    client: WordPressClient,
    attachment_cache: dict[int, dict[str, Any]],
    *,
    config: Config | None,
    root: Path | None,
    v1_feature_policy: bool = False,
    post_id: int | None = None,
    v1_vision_deadline: float | None = None,
) -> dict[str, Any]:
    """Validate normalized featured bytes during ``media-validate``.

    The exact normalized WebP is sent to the model and its content hash keys the
    cache. The apply path compares its own normalized bytes with this hash and
    revalidates if the source changed between validation and upload.
    """
    if config is None:
        return {"status": "skipped", "reason": "configuracao de visao nao fornecida"}
    subject = featured_vision_subject(editorial)
    editorial_context = str(resolve_editorial_identity(editorial).get("news_context") or "")
    vision_context_key = (
        v1_vision_context_key(editorial, post_id) if v1_feature_policy
        else featured_vision_context_key(editorial)
    )
    policy = V1_FEATURED_VISION_POLICY if v1_feature_policy else "featured-key-art-v1"
    threshold = V1_MATCH_THRESHOLD if v1_feature_policy else 0.85
    cache_subject = subject
    media_id = item.get("media_library_id")
    vision_ready, vision_message = vision_config_ready(
        enabled=bool(getattr(config, "vision_enabled", False)),
        api_key=str(getattr(config, "vision_api_key", "")),
        base_url=str(getattr(config, "vision_base_url", "")),
        command=str(getattr(config, "hermes_command", "hermes")),
    )
    # An existing attachment is only fetched when the vision gate can actually
    # validate it. This keeps disabled-vision planning independent of the
    # client interface while preserving mandatory validation when enabled.
    if media_id and not vision_ready:
        return {"status": "deferred", "reason": vision_message, "reason_code": "vision_provider_error"} if v1_feature_policy else {"status": "skipped", "reason": vision_message}
    if v1_feature_policy and config.vision_max_low <= 0:
        return {"status": "deferred", "reason": "orçamento visual esgotado", "reason_code": "vision_budget_exhausted"}
    vision_calls = 0
    attachment: dict[str, Any] | None = None
    if media_id:
        attachment = attachment_cache.get(media_id)
        if attachment is None:
            try:
                attachment = client.get_media(media_id)
            except (WordPressError, TimeoutError, OSError) as exc:
                if not v1_feature_policy or isinstance(exc, WordPressError) and exc.status_code in {401, 403}:
                    raise
                if isinstance(exc, WordPressError) and known_wordpress_entity_missing(exc):
                    return {"status": "rejected", "reason": "anexo do destaque não existe mais", "reason_code": "media_not_found"}
                return {"status": "deferred", "reason": "rechecagem do attachment pendente", "reason_code": "vision_provider_error"}
            if v1_feature_policy and not isinstance(attachment, dict):
                return {"status": "rejected", "reason": "identidade do attachment não confirmada"}
            attachment_cache[media_id] = attachment
        image_url = str(attachment.get("source_url") or "").strip()
        if v1_feature_policy:
            from .media.page_assets import is_noise_image_url

            if attachment.get("id") != media_id or is_noise_image_url(image_url):
                return {"status": "rejected", "reason": "attachment incorreto ou não editorial"}
    else:
        image_url = str(item.get("direct_image_url") or "").strip()
    if not image_url or not subject:
        return {
            "status": "rejected",
            "reason": "featured sem URL ou assunto para validar por visao",
            "reason_code": "incomplete_provenance",
            "failure_stage": "featured_validation",
            "failure_kind": "missing_input",
            "recoverable": False,
        }

    if getattr(config, "vision_mode", "ambiguous") == "ambiguous" and not media_id:
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

    if not vision_ready:
        return {"status": "skipped", "reason": vision_message}

    cache_root = root or Path(".")
    try:
        with tempfile.TemporaryDirectory(prefix="unicornio-featured-vision-") as directory:
            source = download_image(
                image_url,
                Path(directory) / "featured-source.img",
                max_attempts=config.max_source_retries + 1,
                url_policy="enforce" if v1_feature_policy else config.remote_url_policy,
            )
            destination = Path(directory) / "featured-final.webp"
            normalized = (
                _existing_v1_featured_pixels(source, destination)
                if v1_feature_policy and media_id else prepare_featured_webp(source, destination)
            )
            image_sha256 = hashlib.sha256(Path(normalized).read_bytes()).hexdigest()
            cached = get_cached_decision(
                cache_root, image_url, cache_subject,
                image_sha256=image_sha256, policy=policy,
                model_identity=vision_model_identity(
                    getattr(config, "vision_base_url", ""),
                    getattr(config, "vision_model", "gpt-4o-mini"),
                ),
                context_key=vision_context_key,
            )
            if cached is not None and v1_feature_policy and (
                cached.get("image_sha256") != image_sha256 or cached.get("context_key") != vision_context_key
                or cached.get("subject") != subject or cached.get("image_role") != "featured"
            ):
                cached = None
            if cached is not None:
                ok = (
                    cached.get("status") == "MATCH"
                    and float(cached.get("confidence") or 0) >= threshold
                    and cached.get("visual_type") not in {"text_banner", "infographic"}
                )
                return {
                    "status": "passed" if ok else "rejected",
                    "reason": str(cached.get("reason") or "decisao visual reutilizada para os mesmos bytes"),
                    "cached": True,
                    "image_sha256": image_sha256,
                    "decision": dict(cached),
                }
            vision_input = prepare_vision_image_input_from_path(normalized)
            allow_high = not v1_feature_policy or (config.vision_detail == "low" and config.vision_max_low >= 2)
            timeout = float(config.vision_timeout)
            if v1_feature_policy:
                reserved = 2 if allow_high else 1
                if v1_vision_deadline is not None:
                    remaining = v1_vision_deadline - time.monotonic()
                    if remaining <= 0:
                        return {"status": "deferred", "reason": "orçamento visual esgotado", "reason_code": "vision_budget_exhausted"}
                    timeout = min(timeout, remaining / reserved)
                vision_calls = reserved
            decision = verify_image_subject_decision(
                image_url=vision_input,
                subject=subject,
                api_key=str(getattr(config, "vision_api_key", "")),
                base_url=str(getattr(config, "vision_base_url", "")),
                model=str(getattr(config, "vision_model", "gpt-4o-mini")),
                timeout=timeout,
                context=(
                    f"{editorial_context} | preflight da imagem de destaque"
                    if editorial_context else "preflight da imagem de destaque do artigo"
                ),
                category=featured_vision_category(editorial),
                alt=str(item.get("alt_text") or subject),
                detail=str(getattr(config, "vision_detail", "low")),
                allow_high=allow_high,
                require_key_art=True,
                root=root,
                **({"match_threshold": threshold} if v1_feature_policy else {}),
            )
        accepted = bool(decision.get("accepted"))
        if v1_feature_policy:
            vision_calls = 2 if allow_high and "low_status" in decision else 1
            # The backward-compatible decision helper can say accepted=True
            # for an inconclusive low pass when no high budget is available.
            accepted = decision.get("verdict") == "accept"
        if accepted:
            cache_decision = {
                key: decision[key]
                for key in (
                    "status", "confidence", "visual_type", "relation", "editorial_fit",
                    "role_fit", "dimensions_source", "verdict", "reason",
                    "detail_used", "low_status", "low_confidence", "low_visual_type",
                )
                if key in decision
            }
            if v1_feature_policy:
                cache_decision.update(
                    image_sha256=image_sha256, context_key=vision_context_key,
                    image_role="featured", subject=subject,
                )
            set_cached_decision(
                cache_root,
                image_url,
                cache_subject,
                cache_decision,
                image_sha256=image_sha256,
                policy=policy,
                model_identity=vision_model_identity(
                    getattr(config, "vision_base_url", ""),
                    getattr(config, "vision_model", "gpt-4o-mini"),
                ),
                context_key=vision_context_key,
            )
        v1_reason = (
            "accepted" if accepted else
            "role_unsuitable" if decision.get("visual_type") in {"text_banner", "infographic"} else
            "subject_mismatch" if decision.get("verdict") == "reject" else "vision_inconclusive"
        )
        return {
            "status": "passed" if accepted else "rejected",
            "reason": str(decision.get("reason") or "decisao visual sem motivo"),
            "cached": False,
            "image_sha256": image_sha256,
            "decision": decision | ({"accepted": accepted} if v1_feature_policy else {}),
            **({"vision_calls": vision_calls, "reason_code": v1_reason} if v1_feature_policy else {}),
        }
    except MediaDownloadError as exc:
        if not v1_feature_policy:
            result = _structured_media_failure(
                exc, stage="featured_vision", reason_code="vision_provider_error", recoverable=True,
            )
            return {**result, "technical": True, "reason": result["detail"], "cached": False}
        cause = exc.__cause__
        # A DNS outage does not prove that the URL violates the policy.
        unsafe = isinstance(cause, URLSafetyError) and str(cause) != "DNS não resolveu o host"
        invalid = unsafe or getattr(cause, "code", None) in {404, 410} or (
            cause is None and str(exc) in {
                "image URL must use HTTP(S)", "remote resource is not an image",
                "remote image exceeds size limit", "remote image was empty",
            }
        )
        result = _structured_media_failure(
            exc, stage="featured_download",
            reason_code="unsafe_url" if unsafe else ("candidate_asset_invalid" if invalid else "vision_provider_error"),
            recoverable=not invalid,
        )
        return {**result, "status": "rejected" if invalid else "deferred",
                "technical": True, "reason": result["detail"], "cached": False}
    except (VisionGateError, VisionInputUnavailable) as exc:
        result = _structured_media_failure(
            exc,
            stage="featured_vision",
            reason_code=(
                "vision_input_unavailable"
                if isinstance(exc, VisionInputUnavailable)
                else "vision_provider_error"
            ),
            recoverable=True,
        )
        result.update({"technical": True, "reason": result["detail"], "cached": False})
        if v1_feature_policy:
            result.update(status="deferred", vision_calls=vision_calls, reason_code="vision_provider_error")
        return result
    except MediaConversionError as exc:
        # A portrait/invalid source is a deterministic candidate rejection,
        # not an outage of the vision provider.  Keeping this distinction
        # prevents an endless technical retry loop for an incompatible image.
        result = _structured_media_failure(
            exc,
            stage="featured_conversion",
            reason_code="media_conversion_error",
            recoverable=False,
        )
        if v1_feature_policy and exc.recoverable:
            result.update(status="deferred", technical=True, reason_code="vision_provider_error")
        else:
            result.update(status="rejected", technical=False)
        result.update(reason=result["detail"], cached=False)
        return result
    except Exception as exc:  # noqa: BLE001 - fail closed
        result = _structured_media_failure(
            exc,
            stage="featured_vision",
            reason_code="vision_provider_error",
            recoverable=True,
        )
        result.update({"technical": True, "reason": result["detail"], "cached": False})
        if v1_feature_policy:
            result.update(status="deferred", vision_calls=vision_calls)
        return result


def _listicle_media_capacity(
    editorial: dict[str, Any],
    entities: set[str],
    accepted_plan_indexes: set[int] | None = None,
    *,
    post_title: str = "",
    v1_categories: bool = False,
) -> dict[str, Any]:
    """Return the verified inline-image capacity before listicle authoring.

    A listicle may promise only as many items as it can cover with distinct,
    relevant inline images. Featured media is deliberately excluded.
    """
    html = str(editorial.get("cleaned_html") or "")
    title = post_title or str((editorial.get("seo") or {}).get("title") or "")
    categories = (editorial.get("categories") or ()) if v1_categories else ()
    promised = detect_list_format(title, html, categories)
    if promised is None:
        return {"applicable": False}

    list_items = extract_list_items(title, html, categories)
    covered_slots: set[int] = set()
    covered_items: set[str] = set()

    sources: set[str] = set()
    for position, list_item in enumerate(list_items):
        next_start = list_items[position + 1].heading_start if position + 1 < len(list_items) else len(html)
        section_html = html[list_item.heading_end:next_start]
        for image in iter_content_images(section_html):
            source = str(image.get("src") or "").strip()
            if source and image_is_relevant(
                alt_text=str(image.get("alt") or ""),
                credit_text=str(image.get("caption") or ""),
                source_url=source,
                entities=_item_entities({"subject": list_item.heading}, entities, editorial),
            ):
                sources.add(f"content:{source}")
                covered_slots.add(list_item.paragraph_index)
                covered_items.add(list_item.identity.casefold())

    plan = editorial.get("media_plan") or []
    for index, item in enumerate(plan):
        if bool(item.get("is_featured")):
            continue
        if accepted_plan_indexes is not None and index not in accepted_plan_indexes:
            continue
        key = _plan_source_key(item)
        if key and not key.endswith(":"):
            sources.add(f"plan:{key}")
        paragraph_index = item.get("paragraph_index")
        item_identity = str(item.get("item_identity") or "").casefold()
        matched = next(
            (
                list_item for list_item in list_items
                if (
                    item_identity and item_identity == list_item.identity.casefold()
                ) or (
                    not item_identity
                    and isinstance(paragraph_index, int)
                    and paragraph_index == list_item.paragraph_index
                )
            ),
            None,
        )
        if matched is not None:
            covered_slots.add(matched.paragraph_index)
            covered_items.add(matched.identity.casefold())

    missing_items = [
        item.number if item.number is not None else item.position for item in list_items
        if item.paragraph_index not in covered_slots or item.identity.casefold() not in covered_items
    ]

    available = len(sources)
    return {
        "applicable": True,
        "promised_items": promised,
        "verified_inline_capacity": available,
        "missing": max(0, promised - available),
        "missing_items": missing_items,
        "feasible": available >= promised and not missing_items,
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


def _is_final_phash_duplicate(
    final_phash: str,
    baseline_phashes: list[str] | tuple[str, ...],
    *,
    threshold: int = PHASH_DUPLICATE_MAX_DISTANCE,
) -> bool:
    """Return whether a final WebP pHash repeats an accepted frame."""
    if not final_phash:
        return False
    try:
        candidate_text = str(final_phash).strip().lower()
        if not candidate_text or any(char not in "0123456789abcdef" for char in candidate_text):
            return False
        candidate = int(candidate_text, 16)
        for previous in baseline_phashes:
            previous_text = str(previous or "").strip().lower()
            if (
                previous_text
                and len(previous_text) == len(candidate_text)
                and all(char in "0123456789abcdef" for char in previous_text)
                and (candidate ^ int(previous_text, 16)).bit_count() <= threshold
            ):
                return True
        return False
    except (TypeError, ValueError, OverflowError):
        return False


def _execute_media_plan(
    editorial: dict[str, Any],
    config: Config,
    client: WordPressClient,
    root: Path,
    *,
    preflight: dict[str, Any] | None = None,
    post_id: int | None = None,
    run_id: str = "",
    post_title: str = "",
    previous_inline_phashes: list[str] | tuple[str, ...] = (),
    previous_inline_visual_assets: list[dict[str, Any]] | None = None,
    featured_visual_asset: dict[str, Any] | None = None,
    visual_comparison_budget: list[int] | None = None,
    on_accepted: Callable[[dict[str, Any]], None] | None = None,
    before_upload: Callable[[dict[str, Any]], str] | None = None,
    upload_rejected: Callable[[str], None] | None = None,
    media_fallback_candidates: list[dict[str, Any]] | None = None,
    v1_visual_fallback: bool = False,
    post_categories: Any = (),
    v1_vision_deadline: float | None = None,
    ordinary_game_slots: dict[int, tuple[str, str]] | None = None,
) -> tuple[list[dict[str, Any]], int | None, str | None]:
    """Download, convert to WebP, upload and report the editorial media plan.

    Featured candidates are prepared at exactly 1200x720. In dry-run the plan
    is reported but never executed (uploads are write operations).

    Relevance gate: every candidate must reference a distinctive entity of the
    post (title/keyword/game name). Generic concept matches (e.g. a real bat
    for a game vampire) are rejected before any download/insert — the
    editorial rule is "no image beats a wrong image".
    """
    if post_id is not None:
        load_epoch_protection(root).assert_writable(post_id, effect="media_upload")
    vision_editorial = {
        **editorial, **({"title": post_title} if post_title else {}),
        **({"categories": post_categories} if v1_visual_fallback and post_categories else {}),
    }
    vision_context_key = featured_vision_context_key(vision_editorial)
    plan = editorial.get("media_plan") or []
    if not plan:
        return [], None, None
    planning = (preflight or {}).get("listicle") or {}
    # Capacity is a final eligibility gate, not a per-candidate acquisition
    # gate. A listicle may already have four valid items and acquire the fifth
    # independently; READY/publisher still reject incomplete coverage.
    if planning.get("applicable") and not planning.get("feasible"):
        append_telemetry(
            root,
            "media_incremental_capacity_deferred",
            post_id=post_id,
            promised_items=planning.get("promised_items"),
            verified_inline_capacity=planning.get("verified_inline_capacity"),
            missing_items=planning.get("missing_items") or [],
        )
    entities = extract_entities(
        title=str(vision_editorial.get("title") or (editorial.get("seo") or {}).get("title") or ""),
        content_html=str(vision_editorial.get("cleaned_html") or ""),
        focus_keyword=str((vision_editorial.get("seo") or {}).get("focus_keyword") or ""),
        game_name=vision_editorial.get("game_name"),
    )
    # P1 (auditoria de contexto): o reuso da Media Library e casado por SUBJECT.
    # Quando o media_plan nao declara `subject`, usa o MESMO subject que o
    # `media-search-web` deriva do post (entidade principal do titulo) — antes a
    # entrada ia para o indice com subject vazio e o reuso nunca achava nada.
    subjects_for_index: list[dict[str, Any]] = []
    try:
        from .media.evidence import post_subjects

        subjects_for_index = post_subjects(
            title=str(vision_editorial.get("title") or (editorial.get("seo") or {}).get("title") or ""),
            content_html=str(vision_editorial.get("cleaned_html") or ""),
            focus_keyword=str((vision_editorial.get("seo") or {}).get("focus_keyword") or ""),
            game_name=vision_editorial.get("game_name"),
        )
    except Exception:  # noqa: BLE001 - registro do indice e otimizacao
        subjects_for_index = []

    def _index_subject(item: dict[str, Any]) -> str:
        # Listicle assets belong to their own section. If the section cannot
        # be mapped, leave the index entry unbound instead of using item one.
        if ordinary_game_slots is not None and not item.get("is_featured"):
            return ordinary_game_slots.get(item.get("paragraph_index"), ("", ""))[0]
        expected = _expected_subject(item, vision_editorial)
        if expected:
            return expected
        if len(subjects_for_index) > 1:
            return ""
        declared = str(item.get("subject") or "").strip()
        if declared:
            return declared
        return str(subjects_for_index[0].get("subject") or "") if subjects_for_index else ""

    attachment_cache: dict[int, dict[str, Any]] = {}

    def _funnel(
        stage: str,
        status: str,
        item: dict[str, Any],
        position: int,
        detail: str = "",
        **decision_fields: Any,
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
            engine=_diagnostic_media_engine(item),
            candidate_id=str(item.get("candidate_id") or ""),
            target_id=str(item.get("target_id") or ""),
            source_domain=(
                diagnostic_url_domain(item.get("source_page_url"))
                if str(item.get("provenance_mode") or "") != "credit_only" else ""
            ),
            image_domain=diagnostic_url_domain(item.get("direct_image_url")),
            provenance_mode=str(item.get("provenance_mode") or "source_page"),
            detail=detail[:160],
            **decision_fields,
        )
        if stage == "source_verify":
            normalized_detail = detail.casefold()
            if status == "waived" and str(detail or "") == "credit_only_site_policy":
                verification = "credit_only_site_policy"
            elif status == "passed":
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
                source_domain=diagnostic_url_domain(source),
                detail=detail[:160],
            )

    def _vision_funnel_fields(evidence: dict[str, Any]) -> dict[str, Any]:
        # Select only structured fields; never log the input data URL or prompt.
        return {
            "vision_status": evidence.get("status"),
            "vision_confidence": evidence.get("confidence"),
            "vision_verdict": evidence.get("verdict"),
            "vision_detail_used": evidence.get("detail_used"),
            "vision_decision_source": evidence.get("decision_source"),
            "vision_image_sha256": evidence.get("image_sha256"),
            "vision_subject": evidence.get("subject"),
            "vision_model_identity": evidence.get("model_identity"),
        }

    v1_decisions: list[dict[str, Any]] = []

    def _record_decision(
        position: int, item: dict[str, Any], result: dict[str, Any],
    ) -> dict[str, Any]:
        if not v1_visual_fallback or config.media_source_policy != "credit_only":
            return result
        source = str(item.get("direct_image_url") or "").strip()
        # Audit path only: query parameters can carry CDN access signatures.
        try:
            parsed = urlparse(source)
            observed_url = (
                f"{parsed.scheme}://{parsed.hostname}{parsed.path}"[:300]
                if parsed.scheme in {"http", "https"} and parsed.hostname else ""
            )
        except ValueError:
            observed_url = ""
        evidence = result.get("vision_evidence") or {}
        stage = str(result.get("failure_stage") or ("upload" if result.get("media_id") else "preflight"))
        technical = (
            "failed" if stage in {"download", "conversion"} else
            "passed" if stage in {"featured_vision", "inline_vision", "upload"} else "not_confirmed"
        )
        row = {
            **result,
            "candidate_id": str(item.get("candidate_id") or hashlib.sha256(source.encode()).hexdigest()[:20]),
            "candidate_url": observed_url,
            "candidate_stage": stage,
            "technical_status": technical,
            "vision_attempted": bool(result.get("vision_attempted") or evidence.get("decision_source") == "vision_provider"),
        }
        v1_decisions.append(row)
        _funnel(
            "candidate_decision", str(row.get("status") or ""), {**item, "candidate_id": row["candidate_id"]},
            position, str(row.get("detail") or ""),
            candidate_url=observed_url, candidate_stage=stage, technical_status=technical,
            reason_code=str(row.get("reason_code") or ("accepted" if row.get("media_id") else "preflight_rejected")),
            failure_kind=str(row.get("failure_kind") or ""),
            media_id=row.get("media_id"),
            vision_attempted=row["vision_attempted"], **_vision_funnel_fields(evidence),
        )
        return row

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
        return _media_item_rejection(
            item, entities, client, attachment_cache, vision_editorial, root=root, config=config,
            v1_visual_fallback=v1_visual_fallback,
        )

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
    confirmed_uploads: set[int] = set()
    # Compartilhado entre threads; o lock elimina buscas duplicadas da mesma
    # pagina de origem e conserva a verificacao byte-a-byte por imagem.
    page_cache: dict[str, list[str] | None] = {}
    page_html_cache: dict[str, str | None] = {}
    page_cache_lock = Lock()
    seen_sources: set[str] = set()
    accepted_final_phashes = {str(value) for value in previous_inline_phashes if str(value).strip()}
    accepted_final_phashes_lock = Lock()
    # Pre-passe SERIAL: rejeicao (relevancia/reuso) + deteccao de duplicatas.
    # Duplicata depende da ORDEM (primeira ocorrencia vence) e o
    # attachment_cache e populado aqui (get_media), por isso fica fora do
    # paralelismo.
    pending: list[tuple[int, dict[str, Any]]] = []
    fallback_vision_remaining = config.vision_max_low if v1_visual_fallback else 0
    fallback_vision_deadline = v1_vision_deadline
    if v1_visual_fallback and config.media_source_policy == "credit_only":
        fallback_vision_remaining -= sum(
            int(row.get("vision_calls") or 0) for row in (preflight or {}).get("featured_vision", [])
        )
    fallback_vision_counts = {"approved": 0, "rejected": 0, "unavailable": 0}
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
            outcomes[position] = _record_decision(position, item, {
                "paragraph_index": item.get("paragraph_index"),
                "status": "rejected",
                "detail": reason,
                **(
                    {"vision_evidence": preflight_vision[position]["vision_evidence"]}
                    if position in preflight_vision
                    and isinstance(preflight_vision[position].get("vision_evidence"), dict)
                    else {}
                ),
            })
            continue
        pending.append((position, item))

    if pending or media_fallback_candidates:
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
                from .media.text import credit_text_with_source_site

                # The article page, if verified, is the source of this candidate.
                # Attribute that site, without treating it as an author or
                # inferring legal permission, before upload and composition.
                item["credit_text"] = credit_text_with_source_site(
                    str(item.get("credit_text") or ""),
                    str(item.get("source_page_url") or ""),
                )
                nonlocal fallback_vision_remaining, fallback_vision_deadline
                vision_evidence_record: dict[str, Any] | None = None
                needs_pixels = _v1_needs_pixel_evidence(
                    item, config, v1_visual_fallback,
                )
                evidence = {
                    name: item.get(name)
                    for name in ("source_page_url", "direct_image_url", "alt_text", "credit_text", "provenance_mode")
                }
                suffix = Path(item["direct_image_url"].split("?", 1)[0]).suffix or ".jpg"
                try:
                    attachment = _attachment_evidence(item)
                except (WordPressError, TimeoutError, OSError) as exc:
                    if not needs_pixels or isinstance(exc, WordPressError) and exc.status_code in {401, 403}:
                        raise
                    missing = isinstance(exc, WordPressError) and known_wordpress_entity_missing(exc)
                    return position, {
                        "paragraph_index": item.get("paragraph_index"),
                        "status": "rejected" if missing else "deferred",
                        "reason_code": "media_not_found" if missing else "vision_provider_error",
                        "detail": "anexo da biblioteca indisponível",
                    }
                if needs_pixels and item.get("media_library_id"):
                    from .media.page_assets import is_noise_image_url

                    if not isinstance(attachment, dict) or attachment.get("id") != item["media_library_id"]:
                        return position, {"status": "rejected", "detail": "identidade do attachment não confirmada"}
                    if is_noise_image_url(str(attachment.get("source_url") or "")):
                        return position, {"status": "rejected", "reason_code": "noise_domain", "detail": "imagem de avatar/profil não é mídia editorial"}
                download_url = (
                    attachment.get("source_url") if attachment is not None else item["direct_image_url"]
                )
                try:
                    source = download_image(
                        str(download_url),
                        tmp / f"source_{position}{suffix}",
                        max_attempts=config.max_source_retries + 1,
                        url_policy="enforce" if needs_pixels else config.remote_url_policy,
                        audit=lambda finding: append_telemetry(
                            root, "remote_url_audit", url_domain=diagnostic_url_domain(finding.url), reason=finding.reason
                        ),
                    )
                except MediaDownloadError as exc:
                    unsafe = needs_pixels and isinstance(exc.__cause__, URLSafetyError)
                    _funnel("download", "error", item, position, "unsafe_url" if unsafe else type(exc).__name__)
                    return position, _structured_media_failure(
                        exc,
                        stage="download",
                        reason_code="unsafe_url" if unsafe else "candidate_asset_unavailable",
                        recoverable=not unsafe,
                    ) | {
                        "paragraph_index": item.get("paragraph_index"),
                        **({"failure_kind": "unsafe_url"} if unsafe else {}),
                    }
                _funnel("download", "passed", item, position)
                # Verificacao de conteudo: a imagem baixada deve estar listada
                # na pagina de origem (fail-closed).
                source_verify_status = "passed"
                if (
                    str(item.get("origin_type") or item.get("source_origin_type") or "")
                    == "article_source"
                    and bool(item.get("provenance_verified") or item.get("source_verified"))
                ):
                    # The candidate was extracted from this exact article
                    # document. The remaining download, format, relevance,
                    # dimension, WebP and pHash gates still run normally.
                    ok, verify_reason = True, "article_source_extracted"
                elif str(item.get("provenance_mode") or "") == "credit_only" and str(item.get("credit_text") or "").strip():
                    # Explicit site policy: the direct asset URL and visible
                    # credit suffice; this is not source-page or license proof.
                    ok, verify_reason = True, "credit_only_site_policy"
                    source_verify_status = "waived"
                else:
                    ok, verify_reason = verify_downloaded_against_source(
                        source_page_url=str(item.get("source_page_url") or ""),
                        downloaded=source,
                        direct_image_url=str(download_url),
                        cache=page_cache,
                        cache_html=page_html_cache,
                        cache_lock=page_cache_lock,
                        url_policy=config.remote_url_policy,
                        audit=lambda finding: append_telemetry(
                            root, "remote_url_audit", url_domain=diagnostic_url_domain(finding.url), reason=finding.reason
                        ),
                    )
                if not ok:
                    verification = verification_result(ok, verify_reason)
                    _funnel("source_verify", "rejected", item, position, verify_reason)
                    return position, {
                        "paragraph_index": item.get("paragraph_index"),
                        "status": "rejected",
                        "detail": f"verificacao de origem: {verify_reason}",
                        "reason_code": verification["reason_code"],
                        "recoverable": verification["recoverable"],
                        "failure_stage": verification["failure_stage"],
                        "failure_kind": verification["failure_kind"],
                        "retry_same_source": verification["retry_same_source"],
                        "source_comparison_proved": verification[
                            "source_comparison_proved"
                        ],
                    }
                _funnel("source_verify", source_verify_status, item, position, verify_reason)
                is_featured = bool(item.get("is_featured"))
                if (
                    not is_featured and not needs_pixels
                    and isinstance(item.get("evidence"), dict)
                    and isinstance(item["evidence"].get("vision_decision"), dict)
                ):
                    source_sha256 = hashlib.sha256(Path(source).read_bytes()).hexdigest()
                    if (
                        not _vision_evidence_matches_item(
                            item, vision_editorial, config, image_sha256=source_sha256
                        )
                        and not _item_evidence_relevant(item, entities, vision_editorial)
                    ):
                        _funnel(
                            "inline_vision_evidence", "rejected", item, position,
                            "vision evidence does not match downloaded source bytes",
                        )
                        return position, {
                            "paragraph_index": item.get("paragraph_index"),
                            "status": "rejected",
                            "detail": "inline vision evidence does not match downloaded source bytes",
                            "reason_code": "identity_mismatch",
                            "failure_stage": "inline_vision_evidence",
                            "failure_kind": "sha256_mismatch",
                            "recoverable": False,
                        }
                transparency = "flattened" if image_has_transparency(source) else "none"
                conversion_started = time.monotonic()
                try:
                    if is_featured:
                        webp = prepare_featured_webp(source, tmp / f"featured_{position}.webp")
                    else:
                        webp = convert_to_webp(source, tmp / f"inline_{position}.webp")
                except MediaConversionError as exc:
                    _funnel("conversion", "error", item, position, type(exc).__name__)
                    diagnostics = dict(getattr(exc, "diagnostics", {}) or {})
                    diagnostics.update({
                        "post_id": int(post_id) if post_id is not None else None,
                        "run_id": str(run_id or ""),
                        "candidate_id": str(item.get("candidate_id") or ""),
                        "role": "featured" if is_featured else "inline",
                        "transparency_detected": transparency != "none",
                        "elapsed_ms": round((time.monotonic() - conversion_started) * 1000, 1),
                    })
                    append_telemetry(
                        root,
                        "media_conversion_failure",
                        **diagnostics,
                    )
                    return position, _structured_media_failure(
                        exc,
                        stage="conversion",
                        reason_code="media_conversion_error",
                        recoverable=False,
                    ) | {"paragraph_index": item.get("paragraph_index")}
                from .media.visual_hash import phash_from_path

                final_phash = phash_from_path(str(webp)) or str(item.get("phash") or "")
                # V1 credit-only requires an exact-byte, role-aware Vision
                # decision below. Low edge density cannot distinguish a real
                # stylized artwork from a text-only banner. Keep the legacy
                # filter only where that mandatory pixel decision is absent.
                if is_featured and not needs_pixels and image_is_mostly_flat(webp):
                    _funnel("conversion", "rejected", item, position, "featured plana")
                    return position, {
                        "paragraph_index": item.get("paragraph_index"),
                        "status": "rejected",
                        "detail": "featured aparenta ser so texto/arte plana sem conteudo visual; "
                        "escolha uma key art/imagem real da obra",
                        "reason_code": "role_unsuitable",
                        "failure_stage": "conversion",
                        "failure_kind": "featured_flat_art",
                        "recoverable": False,
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
                            "reason_code": "duplicate_existing_frame",
                            "failure_stage": "preflight",
                            "failure_kind": "duplicate_frame",
                            "recoverable": False,
                        }
                    from .media.visual_identity import verify_candidate_identity
                    try:
                        identity, visual = verify_candidate_identity(
                            webp, candidate_id=str(item.get("candidate_id") or position),
                            baseline=visual_baseline, config=config, root=root,
                            comparison_budget=visual_comparison_budget,
                        )
                    except Exception as exc:
                        # A missing baseline is not permission to accept an
                        # asset without durable identity. Defer it as a
                        # technical comparison failure; do not poison the
                        # candidate's rejection history or exhaust no_progress.
                        detail = f"visual_identity_unverified:{type(exc).__name__}"
                        _funnel("visual_identity", "deferred", item, position, detail)
                        return position, {
                            "paragraph_index": item.get("paragraph_index"),
                            "status": "deferred",
                            "detail": detail,
                            "reason_code": "visual_identity_pending",
                            "failure_stage": "visual_identity",
                            "failure_kind": "comparison_error",
                            "recoverable": True,
                        }
                    if visual_baseline_unavailable:
                        visual = type(visual)("UNVERIFIED", False, reason="baseline_identity_unavailable")
                    if not visual.verified:
                        detail = f"visual_identity_unverified:{visual.reason}"
                        _funnel("visual_identity", "deferred", item, position, visual.reason)
                        return position, {
                            "paragraph_index": item.get("paragraph_index"),
                            "status": "deferred",
                            "detail": detail,
                            "reason_code": "visual_identity_pending",
                            "failure_stage": "visual_identity",
                            "failure_kind": "unverified",
                            "recoverable": True,
                        }
                    if not identity.sha256 or not identity.phash or not identity.visual_group_id:
                        detail = "visual_identity_unverified:identity_incomplete"
                        _funnel("visual_identity", "deferred", item, position, "identity_incomplete")
                        return position, {
                            "paragraph_index": item.get("paragraph_index"),
                            "status": "deferred",
                            "detail": detail,
                            "reason_code": "visual_identity_pending",
                            "failure_stage": "visual_identity",
                            "failure_kind": "identity_incomplete",
                            "recoverable": True,
                        }
                    if visual.decision in {"SAME_IMAGE", "SAME_ART_CROP"}:
                        _funnel("visual_identity", "rejected", item, position, visual.reason)
                        return position, {
                            "paragraph_index": item.get("paragraph_index"),
                            "status": "rejected",
                            "detail": "duplicate_existing_frame",
                            "reason_code": "duplicate_existing_frame",
                            "failure_stage": "preflight",
                            "failure_kind": "duplicate_frame",
                            "recoverable": False,
                        }
                if needs_pixels:
                    subject = _v1_visual_subject(item, vision_editorial, ordinary_game_slots)
                    stage = "featured_vision" if is_featured else "inline_vision"
                    if not subject:
                        detail = "posição de mídia sem assunto editorial verificável"
                        _funnel(stage, "rejected", item, position, detail)
                        return position, {
                            "paragraph_index": item.get("paragraph_index"), "status": "rejected",
                            "detail": detail, "reason_code": "role_unsuitable",
                            "failure_stage": stage, "failure_kind": "unbound_subject", "recoverable": False,
                        }
                    model_identity = vision_model_identity(config.vision_base_url, config.vision_model)
                    cache_policy = V1_FEATURED_VISION_POLICY if is_featured else V1_INLINE_VISION_POLICY
                    cache_context = v1_vision_context_key(vision_editorial, post_id)
                    cached = get_cached_decision(
                        root, str(download_url), subject, image_sha256=identity.sha256,
                        policy=cache_policy, model_identity=model_identity, context_key=cache_context,
                    )
                    if (
                        cached and cached.get("status") == "MATCH"
                        and cached.get("image_sha256") == identity.sha256
                        and cached.get("context_key") == cache_context
                        and cached.get("subject") == subject
                        and cached.get("image_role") == ("featured" if is_featured else "inline")
                        and float(cached.get("confidence") or 0) >= V1_MATCH_THRESHOLD
                        and (not is_featured or cached.get("visual_type") not in {"text_banner", "infographic"})
                    ):
                        decision = {**cached, "verdict": "accept", "accepted": True}
                        decision_source = "cache_same_model_context_bytes"
                    else:
                        now = time.monotonic()
                        budget_seconds = config.media_operation_timeout_seconds
                        if fallback_vision_deadline is None and budget_seconds > 0:
                            fallback_vision_deadline = now + budget_seconds
                        time_left = (fallback_vision_deadline - now) if fallback_vision_deadline is not None else None
                        if fallback_vision_remaining <= 0 or (time_left is not None and time_left <= 0):
                            fallback_vision_counts["unavailable"] += 1
                            _funnel(stage, "deferred", item, position, "vision_budget_exhausted")
                            return position, {
                                "paragraph_index": item.get("paragraph_index"), "status": "deferred",
                                "detail": "limite de avaliações visuais por post atingido",
                                "reason_code": "vision_budget_exhausted", "failure_stage": stage,
                                "failure_kind": "vision_budget_exhausted", "recoverable": True,
                            }
                        # Existing low-to-high decision path can resolve an
                        # inconclusive first pass on the same pixels + actual
                        # article context. Neither ALT nor credit is evidence.
                        allow_high = config.vision_detail == "low" and fallback_vision_remaining >= 2
                        reserved_calls = 2 if allow_high else 1
                        fallback_vision_remaining -= reserved_calls
                        timeout = float(config.vision_timeout)
                        if time_left is not None:
                            timeout = min(timeout, time_left / reserved_calls)
                        vision_attempted = False
                        try:
                            if not config.vision_enabled:
                                raise VisionGateError("visao desabilitada")
                            vision_input = prepare_vision_image_input_from_path(webp)
                            vision_attempted = bool(config.vision_api_key)
                            decision = verify_image_subject_decision(
                                image_url=vision_input, subject=subject,
                                api_key=config.vision_api_key, base_url=config.vision_base_url,
                                model=config.vision_model, timeout=timeout,
                                context=str(resolve_editorial_identity(vision_editorial).get("news_context") or ""),
                                category=featured_vision_category(vision_editorial),
                                detail=config.vision_detail, allow_high=allow_high,
                                require_key_art=is_featured, match_threshold=V1_MATCH_THRESHOLD, root=root,
                            )
                            if allow_high and "low_status" not in decision:
                                fallback_vision_remaining += 1
                            decision_source = "vision_provider"
                        except (VisionGateError, VisionInputUnavailable) as exc:
                            fallback_vision_counts["unavailable"] += 1
                            _funnel(stage, "deferred", item, position, str(exc))
                            return position, {
                                "paragraph_index": item.get("paragraph_index"), "status": "deferred",
                                "detail": str(exc)[:200], "reason_code": "vision_provider_error",
                                "failure_stage": stage, "failure_kind": "vision_unavailable", "recoverable": True,
                                "vision_attempted": vision_attempted,
                            }
                    vision_evidence_record = {
                        "status": decision.get("status"), "confidence": decision.get("confidence"),
                        "visual_type": decision.get("visual_type"), "verdict": decision.get("verdict"),
                        "image_sha256": identity.sha256, "image_role": "featured" if is_featured else "inline",
                        "policy_version": VISION_POLICY_VERSION, "model_identity": model_identity,
                        "context_key": cache_context, "subject": subject,
                        "decision_source": decision_source,
                        **{
                            key: decision[key] for key in ("detail_used", "low_status", "low_confidence")
                            if key in decision
                        },
                    }
                    # Historical inline verify can return accepted=True on
                    # inconclusive. With no source evidence that proves nothing.
                    if decision.get("verdict") != "accept":
                        fallback_vision_counts["rejected"] += 1
                        reason = str(decision.get("reason") or "imagem não confirmada visualmente")
                        code = (
                            "role_unsuitable"
                            if is_featured and decision.get("visual_type") in {"text_banner", "infographic"}
                            else "subject_mismatch" if decision.get("verdict") == "reject"
                            else "vision_confidence_insufficient" if decision.get("status") == "MATCH"
                            else "vision_inconclusive"
                        )
                        _funnel(
                            stage, "rejected", item, position, reason,
                            reason_code=code, **_vision_funnel_fields(vision_evidence_record),
                        )
                        return position, {
                            "paragraph_index": item.get("paragraph_index"), "status": "rejected",
                            "detail": reason[:200], "reason_code": code,
                            "failure_stage": stage, "failure_kind": str(decision.get("verdict") or ""),
                            "recoverable": False, "vision_evidence": vision_evidence_record,
                        }
                    fallback_vision_counts["approved"] += 1
                    if decision_source == "vision_provider":
                        set_cached_decision(
                            root, str(download_url), subject, vision_evidence_record | {"reason": decision.get("reason")},
                            image_sha256=identity.sha256, policy=cache_policy,
                            model_identity=model_identity, context_key=cache_context,
                        )
                    # Generate accessible, contextual text only after a real
                    # match. Reproduce no credit/author claim from the plan.
                    label = {
                        "gameplay": "Cena de gameplay", "key_art": "Arte", "movie_still": "Cena",
                        "character": "Personagem", "product": "Produto", "poster": "Pôster",
                        "photograph": "Foto", "infographic": "Infográfico",
                    }.get(str(decision.get("visual_type") or ""), "Imagem ilustrativa")
                    item["alt_text"] = f"{label}: {subject}"[:160]
                    item["credit_text"] = "Crédito: Imagem ilustrativa."
                    evidence.update(alt_text=item["alt_text"], credit_text=item["credit_text"])
                    _funnel(
                        stage, "passed", item, position, "validated_before_upload",
                        **_vision_funnel_fields(vision_evidence_record),
                    )
                elif is_featured and config.vision_enabled and config.vision_api_key:
                    v1_feature_policy = v1_visual_fallback and config.media_source_policy == "credit_only"
                    policy = V1_FEATURED_VISION_POLICY if v1_feature_policy else "featured-key-art-v1"
                    threshold = V1_MATCH_THRESHOLD if v1_feature_policy else 0.85
                    context_key = (
                        v1_vision_context_key(vision_editorial, post_id) if v1_feature_policy else vision_context_key
                    )
                    vision_preflight = preflight_vision.get(position) or {}
                    expected_sha256 = str(vision_preflight.get("image_sha256") or "")
                    decision = None
                    decision_source = ""
                    if vision_preflight.get("deterministic"):
                        decision = {
                            "accepted": True,
                            "reason": str(vision_preflight.get("reason") or "evidência determinística"),
                        }
                        decision_source = "deterministic_evidence"
                    elif (
                        vision_preflight.get("status") == "passed"
                        and expected_sha256
                        and expected_sha256 == identity.sha256
                        and _featured_preflight_evidence_matches(
                            vision_preflight,
                            vision_editorial,
                            config,
                            image_sha256=identity.sha256, context_key=context_key,
                        )
                    ):
                        preflight_decision = vision_preflight.get("decision")
                        if isinstance(preflight_decision, dict) and preflight_decision.get("accepted"):
                            decision = preflight_decision
                            decision_source = "preflight_same_bytes"
                    if decision is None:
                        subject = featured_vision_subject(vision_editorial)
                        cache_subject = vision_cache_subject(vision_editorial)
                        cached = get_cached_decision(
                            root, str(item.get("direct_image_url") or ""),
                            cache_subject, image_sha256=identity.sha256,
                            policy=policy,
                            model_identity=vision_model_identity(config.vision_base_url, config.vision_model),
                            context_key=context_key,
                        )
                        if cached is not None and v1_feature_policy and (
                            cached.get("image_sha256") != identity.sha256 or cached.get("context_key") != context_key
                            or cached.get("subject") != subject or cached.get("image_role") != "featured"
                        ):
                            cached = None
                        if cached is not None and (
                            cached.get("status") == "MATCH"
                            and float(cached.get("confidence") or 0) >= threshold
                            and cached.get("visual_type") not in {"text_banner", "infographic"}
                        ):
                            decision = {
                                **cached, "verdict": "accept", "accepted": True,
                                "reason": str(cached.get("reason") or "cache de bytes idênticos"),
                            }
                            decision_source = "cache_same_model_context_bytes"
                        else:
                            try:
                                vision_input = prepare_vision_image_input_from_path(webp)
                                decision = verify_image_subject_decision(
                                    image_url=vision_input,
                                    subject=subject,
                                    api_key=config.vision_api_key,
                                    base_url=config.vision_base_url,
                                    model=config.vision_model,
                                    timeout=float(getattr(config, "vision_timeout", config.http_timeout)),
                                    context=(
                                        f"{resolve_editorial_identity(vision_editorial).get('news_context')} | validação final da featured"
                                        if resolve_editorial_identity(vision_editorial).get("news_context")
                                        else "validação final da featured antes do upload"
                                    ),
                                    category=featured_vision_category(vision_editorial),
                                    alt=str(item.get("alt_text") or subject),
                                    detail=config.vision_detail,
                                    allow_high=True,
                                    require_key_art=True,
                                    root=root,
                                    **({"match_threshold": threshold} if v1_feature_policy else {}),
                                )
                                decision_source = "vision_provider"
                            except (VisionGateError, VisionInputUnavailable) as exc:
                                decision = {
                                    "status": "ERROR", "confidence": 0.0,
                                    "visual_type": "other", "verdict": "error",
                                    "accepted": False,
                                    "reason": f"validacao dos bytes finais falhou: {type(exc).__name__}",
                                }
                                decision_source = "vision_provider_error"
                            if decision.get("accepted"):
                                cache_decision = {
                                    key: decision[key]
                                    for key in (
                                        "status", "confidence", "visual_type", "relation", "editorial_fit",
                                        "role_fit", "dimensions_source", "verdict", "reason",
                                        "detail_used", "low_status", "low_confidence", "low_visual_type",
                                    )
                                    if key in decision
                                }
                                if v1_feature_policy:
                                    cache_decision.update(
                                        image_sha256=identity.sha256, context_key=context_key,
                                        subject=subject, image_role="featured",
                                    )
                                set_cached_decision(
                                    root,
                                    str(item.get("direct_image_url") or ""),
                                    cache_subject,
                                    cache_decision,
                                    image_sha256=identity.sha256,
                                    policy=policy,
                                    model_identity=vision_model_identity(config.vision_base_url, config.vision_model),
                                    context_key=context_key,
                                )
                    if decision is not None:
                        vision_evidence_record = {
                            key: decision[key]
                            for key in (
                                "status", "confidence", "visual_type", "relation", "editorial_fit",
                                "role_fit", "dimensions_source", "verdict", "detail_used",
                                "low_status", "low_confidence", "low_visual_type",
                            )
                            if key in decision
                        }
                        vision_evidence_record.update({
                            "image_sha256": identity.sha256,
                            "image_role": "featured",
                            "policy_version": VISION_POLICY_VERSION,
                            "decision_source": decision_source,
                            "model_identity": vision_model_identity(config.vision_base_url, config.vision_model),
                            "context_key": context_key,
                            "subject": featured_vision_subject(vision_editorial),
                        })
                    if not decision.get("accepted"):
                        reason = str(decision.get("reason") or "featured não confirmada visualmente")
                        _funnel("featured_vision_final_bytes", "rejected", item, position, reason)
                        vision_reason_code = (
                            "vision_provider_error"
                            if str(decision.get("status") or "").casefold() == "error"
                            or str(decision.get("verdict") or "").casefold() == "error"
                            else "role_unsuitable"
                            if str(decision.get("role_fit") or "") == "unsuitable"
                            else "subject_mismatch"
                        )
                        return position, {
                            "paragraph_index": item.get("paragraph_index"),
                            "status": "rejected",
                            "detail": reason[:200],
                            "reason_code": vision_reason_code,
                            "failure_stage": "featured_vision",
                            "failure_kind": str(decision.get("verdict") or "vision_rejected")[:48],
                            "recoverable": vision_reason_code == "vision_provider_error",
                            **({"vision_evidence": vision_evidence_record} if vision_evidence_record else {}),
                        }
                    _funnel(
                        "featured_vision_final_bytes", "passed", item, position,
                        "same_sha256_as_preflight" if expected_sha256 == identity.sha256 else "validated_before_upload",
                    )
                visual_record = {"decision": visual.decision, "reason": visual.reason, "comparisons": list(visual.comparisons)}
                accepted_result = {
                    "status": "accepted",
                    "paragraph_index": item["paragraph_index"],
                    "placement": item.get("placement"),
                    "candidate_id": item.get("candidate_id"),
                    "target_id": item.get("target_id"),
                    "direct_image_url": item.get("direct_image_url"),
                    "alt_text": item["alt_text"], "credit_text": item.get("credit_text") or "",
                    "subject": item.get("subject"),
                    "item_identity": item.get("item_identity"),
                    "item_title": item.get("item_title") or item.get("subject"),
                    "item_group": item.get("item_group"),
                    "item_number": item.get("item_number"),
                    "section_heading": item.get("section_heading"),
                    "section_slot": item.get("section_slot"),
                    "html_position": item.get("html_position"),
                    "featured": is_featured, "width": width, "height": height,
                    "provenance_mode": str(item.get("provenance_mode") or "source_page"),
                    "source_page_verified": source_verify_status == "passed",
                    "rights_verified": False if str(item.get("provenance_mode") or "") == "credit_only" else None,
                    "transparency": transparency, "phash": final_phash or identity.phash,
                    "sha256": identity.sha256, "visual_group_id": identity.visual_group_id,
                    "visual_verification": visual_record,
                    **({"vision_evidence": vision_evidence_record} if vision_evidence_record else {}),
                }
                token = before_upload(accepted_result) if before_upload is not None else None
                try:
                    media = upload_image(client, webp, {**evidence, "_v1_upload_token": token} if token else evidence)
                except Exception as exc:  # noqa: BLE001 - upload failure is candidate-scoped
                    if token and upload_rejected is not None and (
                        isinstance(exc, SafetyError)
                        or isinstance(exc, WordPressError) and exc.api_code in {
                            "rest_cannot_create", "rest_upload_no_data", "rest_invalid_param",
                        }
                    ):
                        upload_rejected(token)
                    _funnel("upload", "error", item, position, type(exc).__name__)
                    return position, _structured_media_failure(
                        exc,
                        stage="upload",
                        reason_code="media_upload_error",
                        recoverable=True,
                    ) | {"paragraph_index": item.get("paragraph_index")}
                media_id = media.get("id")
                media_url = media.get("source_url")
                if not media_id or not media_url:
                    return position, {
                        "paragraph_index": item.get("paragraph_index"),
                        "status": "error",
                        "reason_code": "media_upload_error",
                        "failure_stage": "upload",
                        "failure_kind": "missing_response_fields",
                        "recoverable": True,
                        "detail": "upload returned incomplete media identity",
                    }
                accepted_result.update(media_id=media_id, media_url=media_url)
                if token:
                    accepted_result["upload_token"] = token

                confirmed_uploads.add(position)
                # Commit verified work before index/cache/telemetry can fail.
                if on_accepted is not None:
                    on_accepted(accepted_result)
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
                        subject=_index_subject(item),
                        media_id=int(media_id),
                        article_id=int(post_id) if post_id else None,
                        sha256=identity.sha256,
                        visual_group_id=identity.visual_group_id,
                        credit_text=str(item.get("credit_text") or ""),
                        alt_text=str(item.get("alt_text") or ""),
                    )
                except (OSError, ValueError) as exc:
                    try:
                        append_telemetry(root, "media_index_write_failed",
                                         post_id=post_id, error=str(exc)[:200])
                    except Exception:  # noqa: BLE001
                        pass
                _funnel("upload", "passed", item, position)
                if identity.sha256:
                    visual_baseline.append({**identity.to_dict(), "media_id": int(media_id), "media_url": str(media_url), "local_path": str(webp)})
                if final_phash:
                    accepted_final_phashes.add(final_phash)
                return position, accepted_result

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
                    _, result = _process_item(pair)
                    processed.append((position, _record_decision(position, item, result)))
                except Exception as exc:  # noqa: BLE001 - report one failed item
                    if (on_accepted is not None and position in confirmed_uploads) or (
                        v1_visual_fallback and config.media_source_policy == "credit_only"
                        and isinstance(exc, WordPressError) and exc.status_code in {401, 403}
                    ):
                        raise
                    _funnel("worker", "error", item, position, type(exc).__name__)
                    processed.append((position, _record_decision(position, item, _structured_media_failure(
                        exc,
                        stage="worker",
                        reason_code="media_processing_error",
                        recoverable=True,
                    ) | {"paragraph_index": item.get("paragraph_index")})))

            for position, result in processed:
                outcomes[position] = result

            fallback_queue = [
                candidate
                for candidate in (media_fallback_candidates or [])
                if isinstance(candidate, dict)
            ][:max(len(plan), 2)]
            used_fallbacks: set[int] = set()
            asset_fields = (
                "direct_image_url", "source_page_url", "credit_text", "candidate_id",
                "provenance_mode", "source_page_verified", "rights_verified",
                "evidence_verdict", "evidence_score", "origin_evidence", "evidence",
                "phash", "media_library_id", "author", "license", "license_url",
                "captured_at", "source_verified", "provenance_verified", "origin_type",
                "source_origin_type",
            )

            def _fallback_is_candidate_specific(result: dict[str, Any]) -> bool:
                status = str(result.get("status") or "").casefold()
                reason_code = str(result.get("reason_code") or "").casefold()
                stage = str(result.get("failure_stage") or "").casefold()
                if status == "rejected":
                    return reason_code != "vision_provider_error"
                return status == "error" and stage in {"download", "source_verify", "conversion"}

            def _fallback_matches_slot(candidate: dict[str, Any], slot: dict[str, Any]) -> bool:
                candidate_target = str(candidate.get("target_id") or "").strip()
                slot_target = str(slot.get("target_id") or "").strip()
                if candidate_target or slot_target:
                    return bool(candidate_target and candidate_target == slot_target)
                candidate_identity = str(candidate.get("item_identity") or "").strip()
                slot_identity = str(slot.get("item_identity") or "").strip()
                if candidate_identity or slot_identity:
                    return bool(candidate_identity and candidate_identity == slot_identity)
                # Normal articles can use a shared reserve for either role;
                # role/ALT are copied from the failed slot before validation.
                return True

            for position in sorted(outcomes):
                primary_result = outcomes[position]
                if not _fallback_is_candidate_specific(primary_result):
                    continue
                slot = plan[position]
                tried = 0
                while True:
                    candidate_index = next(
                        (
                            index for index, candidate in enumerate(fallback_queue)
                            if index not in used_fallbacks and _fallback_matches_slot(candidate, slot)
                        ),
                        None,
                    )
                    if candidate_index is None:
                        break
                    used_fallbacks.add(candidate_index)
                    candidate = fallback_queue[candidate_index]
                    tried += 1
                    fallback_item = dict(slot)
                    for name in asset_fields:
                        if name in candidate:
                            fallback_item[name] = candidate[name]
                        elif v1_visual_fallback and config.media_source_policy == "credit_only":
                            fallback_item.pop(name, None)
                    fallback_item["paragraph_index"] = slot.get("paragraph_index")
                    fallback_item["placement"] = slot.get("placement")
                    fallback_item["is_featured"] = bool(slot.get("is_featured"))
                    fallback_item["role"] = "featured" if fallback_item["is_featured"] else "inline"
                    subject = str(slot.get("subject") or editorial.get("game_name") or "imagem")
                    role_label = "imagem principal" if fallback_item["is_featured"] else "imagem de apoio"
                    fallback_item["alt_text"] = f"{subject} - {role_label}"[:160]

                    reason = _rejection_reason(fallback_item)
                    if reason is None:
                        reason = _duplicate_source_reason([fallback_item], 0, seen_sources)
                    if reason:
                        _funnel("fallback", "rejected", fallback_item, position, reason)
                        _record_decision(position, fallback_item, {
                            "status": "rejected", "failure_stage": "preflight", "detail": reason,
                            "reason_code": "duplicate_source" if "repetida" in reason else "preflight_rejected",
                        })
                        continue

                    _funnel("fallback", "attempted", fallback_item, position)
                    try:
                        _, fallback_result = _process_item((position, fallback_item))
                    except Exception as exc:  # noqa: BLE001 - candidate-scoped fallback
                        if (on_accepted is not None and position in confirmed_uploads) or (
                            v1_visual_fallback and config.media_source_policy == "credit_only"
                            and isinstance(exc, WordPressError) and exc.status_code in {401, 403}
                        ):
                            raise
                        _funnel("fallback", "error", fallback_item, position, type(exc).__name__)
                        fallback_result = _structured_media_failure(
                            exc,
                            stage="worker",
                            reason_code="media_processing_error",
                            recoverable=True,
                        ) | {"paragraph_index": slot.get("paragraph_index")}
                    fallback_result = _record_decision(position, fallback_item, fallback_result)
                    if fallback_result.get("status") in {"accepted", "ok"}:
                        fallback_result["fallback_used"] = True
                        fallback_result["fallback_for_candidate_id"] = slot.get("candidate_id")
                        outcomes[position] = fallback_result
                        _funnel("fallback", "accepted", fallback_item, position)
                        break
                    _funnel(
                        "fallback",
                        "failed",
                        fallback_item,
                        position,
                        str(fallback_result.get("reason_code") or fallback_result.get("detail") or "rejected"),
                    )
                    if _is_deferred_media_verification(fallback_result):
                        # The primary rejection is already audited. This slot
                        # is not exhausted while a viable reserve still needs
                        # verification; reporting the primary would burn retry.
                        outcomes[position] = {
                            **fallback_result,
                            "fallback_for_candidate_id": primary_result.get("candidate_id") or slot.get("candidate_id"),
                            "fallback_candidates_tried": tried,
                        }
                        break
                    if not _fallback_is_candidate_specific(fallback_result):
                        break
                if tried and outcomes[position] is primary_result:
                    outcomes[position] = {**primary_result, "fallback_candidates_tried": tried}

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
        source_verified=sum(result.get("source_page_verified") is True for result in results),
        credit_only_accepted=sum(
            result.get("status") in {"accepted", "ok"}
            and result.get("provenance_mode") == "credit_only"
            for result in results
        ),
        source_mismatch=sum("mismatch" in str(result.get("detail") or "").casefold() for result in results),
        missing_source_page=sum("origem" in str(result.get("detail") or "").casefold() for result in results if result.get("status") == "rejected"),
        relevance_match=sum(bool((item.get("evidence") or {}).get("verdict") == "deterministic_match") for item in plan),
        relevance_ambiguous=sum(bool((item.get("evidence") or {}).get("verdict") == "ambiguous") for item in plan),
        relevance_reject=(
            sum(row.get("reason_code") == "subject_mismatch" for row in v1_decisions)
            if v1_visual_fallback and config.media_source_policy == "credit_only"
            else sum(result.get("status") == "rejected" for result in results)
        ),
        **({
            "unique_candidates_visually_reviewed": len({
                (row["candidate_id"], (row.get("vision_evidence") or {}).get("image_sha256"))
                for row in v1_decisions if (row.get("vision_evidence") or {}).get("status")
            }),
        } if v1_visual_fallback and config.media_source_policy == "credit_only" else {}),
        vision_approved=(
            sum(row.get("status") == "passed" for row in preflight_vision.values()) + fallback_vision_counts["approved"]
        ),
        vision_rejected=(
            sum(row.get("status") == "rejected" for row in preflight_vision.values()) + fallback_vision_counts["rejected"]
        ),
        vision_input_unavailable=(
            sum(row.get("status") == "unavailable" for row in preflight_vision.values()) + fallback_vision_counts["unavailable"]
        ),
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
    approved_sha: str | None = None,
    progress: _V1MediaProgress | None = None,
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

    def report(reason_code: str, stage: str, *, outcome: str, exc: Exception | None = None) -> None:
        if root is None:
            return
        fields: dict[str, Any] = {
            "post_id": int(post.get("id") or 0) or None,
            "media_id": featured,
            "role": "featured",
            "stage": stage,
            "outcome": outcome,
            "reason_code": reason_code,
        }
        if exc is not None:
            fields["exception_type"] = type(exc).__name__
        try:
            append_telemetry(root, "featured_normalization", **fields)
        except Exception:  # noqa: BLE001 - diagnostics cannot change media decisions
            pass

    try:
        media = client.get_media(featured)
    except Exception as exc:
        report("wordpress_read_failed", "read_attachment", outcome="retryable_failure", exc=exc)
        return None
    if approved_sha and not isinstance(media, dict):
        report("attachment_identity_changed", "read_attachment", outcome="rejected")
        return None
    details = media.get("media_details") or {}
    width, height = details.get("width"), details.get("height")
    source_url = (media.get("source_url") or "").strip()
    if approved_sha:
        from .media.page_assets import is_noise_image_url

        if media.get("id") != featured or is_noise_image_url(source_url):
            report("attachment_identity_changed", "read_attachment", outcome="rejected")
            return None
    if editorial is not None and not approved_sha:
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
                report("existing_featured_not_relevant", "source_relevance", outcome="rejected")
                return None
    already_normalized = width == 1280 and height == 720 and source_url.lower().endswith(".webp")
    if already_normalized and not approved_sha:
        report("already_normalized", "complete", outcome="reused")
        return featured
    if not source_url:
        report("attachment_source_url_missing", "read_attachment", outcome="rejected")
        return None
    from .media.text import plain_text

    title = plain_text(
        str((media.get("title") or {}).get("rendered") or "")
    ) or "Imagem de destaque"
    alt = plain_text(str(media.get("alt_text") or ""))
    caption = plain_text(str((media.get("caption") or {}).get("rendered") or ""))
    filename = _featured_filename_from_source(source_url)
    if approved_sha and editorial is not None:
        title = "Imagem ilustrativa"
        alt = f"Imagem ilustrativa: {featured_vision_subject(editorial)}"[:160]
        caption = "Crédito: Imagem ilustrativa."
    with tempfile.TemporaryDirectory(prefix="unicornio-featured-") as directory:
        tmp = Path(directory)
        try:
            source = download_image(
                source_url,
                tmp / "featured_source.jpg",
                max_attempts=config.max_source_retries + 1,
                url_policy="enforce" if approved_sha else config.remote_url_policy,
                audit=(
                    lambda finding: append_telemetry(
                        root, "remote_url_audit", url_domain=diagnostic_url_domain(finding.url), reason=finding.reason
                    )
                    if root is not None
                    else None
                ),
            )
        except Exception as exc:
            report("source_download_failed", "download", outcome="retryable_failure", exc=exc)
            return None
        try:
            destination = tmp / "featured_1280x720.webp"
            webp = (_existing_v1_featured_pixels(source, destination) if approved_sha
                    else prepare_featured_webp(source, destination))
        except Exception as exc:
            report("featured_conversion_failed", "convert", outcome="rejected", exc=exc)
            return None
        retained_row = None
        if approved_sha:
            if hashlib.sha256(webp.read_bytes()).hexdigest() != approved_sha:
                report("approved_bytes_changed", "vision_readback", outcome="rejected")
                return None
            if progress is not None:
                from .media.visual_hash import phash_from_path

                phash = phash_from_path(str(webp))
                if phash:
                    retained_row = {
                        "status": "accepted", "featured": True, "width": 1280, "height": 720,
                        "alt_text": alt, "credit_text": caption, "sha256": approved_sha, "phash": phash,
                        "direct_image_url": source_url,
                        "visual_verification": {"decision": "INITIAL", "reason": "approved original cover"},
                        "vision_evidence": {"image_role": "featured", "verdict": "accept", "image_sha256": approved_sha},
                    }
            if already_normalized and webp == source:
                if retained_row and progress and media.get("mime_type") == "image/webp":
                    progress.remember({
                        **retained_row, "media_id": featured, "media_url": source_url,
                    })
                return featured
            if webp == source:
                destination.write_bytes(source.read_bytes())
                webp = destination
        token = progress.begin(retained_row) if retained_row and progress else None
        try:
            new_media = client.upload_media(
                str(webp),
                filename=filename,
                alt_text=alt,
                title=f"{title} [{token}]" if token else title,
                caption=caption,
            )
        except Exception as exc:
            report("wordpress_upload_failed", "upload", outcome="retryable_failure", exc=exc)
            if token:
                raise V1PartialResumeError("retained_media_upload_uncertain", post_id=int(post["id"])) from exc
            return None
        if token and retained_row and progress:
            if not isinstance(new_media, dict):
                raise V1PartialResumeError("retained_media_upload_uncertain", post_id=int(post["id"]))
            progress.accepted({
                **retained_row, "upload_token": token,
                "media_id": new_media.get("id"), "media_url": new_media.get("source_url"),
            })
    if not isinstance(new_media, dict):
        report("wordpress_upload_missing_media_id", "upload", outcome="retryable_failure")
        return None
    new_id = new_media.get("id")
    if not isinstance(new_id, int) or new_id <= 0:
        report("wordpress_upload_missing_media_id", "upload", outcome="retryable_failure")
        return None
    report("normalized", "complete", outcome="accepted")
    return new_id


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
    if "needs_trailer" not in resolved or resolved.get("needs_trailer") is None:
        resolved["needs_trailer"] = False
    if "trailer_url" not in resolved:
        resolved["trailer_url"] = None
    if resolved.get("needs_trailer") is False:
        resolved["trailer_url"] = None
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
    from .content_quality import _PT_STOPWORDS

    candidates: list[str] = []
    if game_name and game_name.strip():
        candidates.append(game_name.strip())
    title = (title or "").strip()
    tokens = re.findall(r"[\wÀ-ÿ]+", title)
    for size in range(min(5, len(tokens)), 1, -1):
        for start in range(0, len(tokens) - size + 1):
            phrase_tokens = tokens[start:start + size]
            while phrase_tokens and phrase_tokens[0].casefold() in _PT_STOPWORDS:
                phrase_tokens = phrase_tokens[1:]
            while phrase_tokens and phrase_tokens[-1].casefold() in _PT_STOPWORDS:
                phrase_tokens = phrase_tokens[:-1]
            if len([token for token in phrase_tokens if token.casefold() not in _PT_STOPWORDS]) >= 2:
                candidates.append(" ".join(phrase_tokens))
    if title:
        candidates.append(title)
    # Preserve order while avoiding duplicate candidates from overlapping ngrams.
    return list(dict.fromkeys(candidate.strip() for candidate in candidates if candidate.strip()))


def _resolve_seo_from_post(
    post: dict[str, Any], *, game_name: str | None = None
) -> dict[str, Any]:
    from .content_quality import _keyword_in_text, keyword_occurs_naturally
    from .editorial_schema import EditorialValidationError

    meta = post.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}
    post_title = _post_title(post) or ""
    body = clean_html(_raw_content(post), post_title=post_title)
    body_text = re.sub(r"<[^>]+>", " ", body)
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
        and keyword_occurs_naturally(keyword.strip(), title.strip(), body_text)
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
        if _keyword_in_text(candidate, derived_title) and _keyword_in_text(candidate, body_text):
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
    assert_post_not_quarantined(root, post_id, operation="publish")
    with _acquire_post_lock(root, config, post_id) as lock_handle:
        guarded_client = _guard_post_client(client, lock_handle, root=root, post_id=post_id)
        return _publish_post_unlocked(guarded_client, config, root, post_id)


def _rest_empty_v1_work_state(post: dict[str, Any]) -> bool:
    """WordPress exposes unset string meta as empty, even on explicit V1 posts."""
    meta = post.get("meta")
    return (
        isinstance(meta, dict)
        and meta.get("_hermes_work_state") == ""
        and meta.get(META_STATE) in (STATE_READY, STATE_PUBLISHED)
    )


def _read_v2_work_state(
    post: dict[str, Any], *, allow_rest_empty_v1: bool | None = None,
) -> dict[str, Any] | None:
    meta = post.get("meta")
    if not isinstance(meta, dict):
        meta = {}
    raw = meta.get("_hermes_work_state")
    if raw is None or (allow_rest_empty_v1 and _rest_empty_v1_work_state(post)):
        return None
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"state": "invalid"}
    return value if isinstance(value, dict) else {"state": "invalid"}


def _publish_v2_media_context(
    v2_state: dict[str, Any] | None,
    post: dict[str, Any],
    *,
    root: Path | None = None,
) -> dict[str, Any] | None:
    """Return fail-closed V2 media evidence for publisher checklist validation.

    ``inline.accepted`` is the verified working set, not necessarily the set
    written to the current WordPress body.  A valid normal-article layout
    reduction may preserve a bounded surplus as ``accepted_working_not_applied``;
    every applied asset still needs current HTML/readback and journal proof.
    """
    if v2_state is None:
        return None
    raw_media = v2_state.get("media")
    media = raw_media if isinstance(raw_media, dict) else {}
    raw_inline = media.get("inline")
    inline = raw_inline if isinstance(raw_inline, dict) else {}
    required = inline.get("required", 0)
    if not isinstance(required, int) or isinstance(required, bool) or required < 0:
        required = 0
    raw_accepted = inline.get("accepted")
    accepted_is_list = isinstance(raw_accepted, list)
    accepted = raw_accepted if accepted_is_list else []
    featured = media.get("featured")
    featured_is_mapping = isinstance(featured, dict)
    if not featured_is_mapping:
        featured = {}
    accepted = [item for item in accepted if isinstance(item, dict)]
    accepted_urls = {
        _canonical_image_url(str(item.get("media_url") or ""))
        for item in accepted
        if str(item.get("media_url") or "").strip()
        and isinstance(item.get("media_id"), int)
        and not isinstance(item.get("media_id"), bool)
        and item["media_id"] > 0
    }
    current_urls = {
        _canonical_image_url(str(item.get("src") or ""))
        for item in iter_content_images(_raw_content(post))
        if str(item.get("src") or "").strip()
    }
    current_featured_id = post.get("featured_media")
    featured_matches = (
        isinstance(current_featured_id, int)
        and not isinstance(current_featured_id, bool)
        and current_featured_id > 0
        and featured.get("status") == "valid"
        and featured.get("media_id") == current_featured_id
    )
    accepted_ids = {
        int(item["media_id"])
        for item in accepted
        if isinstance(item.get("media_id"), int)
        and not isinstance(item.get("media_id"), bool)
        and item["media_id"] > 0
    }
    applied_urls = accepted_urls & current_urls
    surplus_urls = accepted_urls - current_urls
    layout_policy = media.get("layout_policy")
    layout_policy = layout_policy if isinstance(layout_policy, dict) else {}
    target_ideal = layout_policy.get("target_ideal")
    target_effective = layout_policy.get("target_effective")
    try:
        from .pipeline_v2.media_layout import _layout_sha256

        layout_hash_valid = layout_policy.get("layout_sha256") == _layout_sha256(layout_policy)
    except (TypeError, ValueError, KeyError):
        layout_hash_valid = False
    reduction_authorized = (
        layout_policy.get("schema_version") == 2
        and layout_policy.get("policy") == "normal-article-layout-v2"
        and layout_policy.get("layout") == "normal_article"
        and isinstance(target_ideal, int)
        and not isinstance(target_ideal, bool)
        and isinstance(target_effective, int)
        and not isinstance(target_effective, bool)
        and target_ideal >= target_effective >= 1
        and target_effective == required
        and layout_policy.get("reduction_reason") == "layout_limited"
        and layout_hash_valid
    )
    reduction_limit = (
        max(0, int(target_ideal) - int(target_effective))
        if reduction_authorized else 0
    )
    surplus_authorized = (
        reduction_authorized
        and len(surplus_urls) <= reduction_limit
        and len(applied_urls) == int(target_effective)
    )
    journal_proof = False
    journal = None
    if root is not None:
        journal_path = Path(root) / "work" / "v2-journal" / f"{int(post.get('id') or 0)}.json"
        try:
            journal_value = json.loads(journal_path.read_text(encoding="utf-8"))
            journal = journal_value if isinstance(journal_value, dict) else None
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            journal = None
    if surplus_urls:
        apply_records = journal.get("apply_media_records") if isinstance(journal, dict) else None
        media_records = journal.get("media_records") if isinstance(journal, dict) else None
        confirmed_ids = {
            int(record.get("media_id"))
            for record in (apply_records or [])
            if isinstance(record, dict)
            and str(record.get("role") or "") == "inline"
            and record.get("readback_status") == "confirmed"
            and str(record.get("destination") or "") == "applied_readback_confirmed"
            and str(record.get("media_id") or "").isdigit()
        }
        unapplied_ids = {
            int(record.get("media_id"))
            for record in (media_records or [])
            if isinstance(record, dict)
            and str(record.get("role") or "") == "inline"
            and str(record.get("destination") or "") == "accepted_working_not_applied"
            and str(record.get("media_id") or "").isdigit()
        }
        surplus_ids = {
            int(item["media_id"])
            for item in accepted
            if _canonical_image_url(str(item.get("media_url") or "")) in surplus_urls
        }
        current_hash = hashlib.sha256(
            str((post.get("content") or {}).get("raw") or "").encode("utf-8")
        ).hexdigest()
        journal_proof = bool(
            journal
            and journal.get("status") == "committed"
            and journal.get("readback") is True
            and journal.get("media_application_status") == "confirmed"
            and journal.get("html_readback_status") == "confirmed"
            and confirmed_ids == accepted_ids - surplus_ids
            and unapplied_ids == surplus_ids
            and journal.get("candidate_hash") == current_hash
        )
    evidence_matches_current_media = (
        isinstance(raw_media, dict)
        and isinstance(raw_inline, dict)
        and accepted_is_list
        and isinstance(raw_inline.get("accepted"), list)
        and len(accepted) == len(raw_inline["accepted"])
        and len(accepted_urls) == len(accepted)
        and featured_is_mapping
        and bool(applied_urls) == bool(accepted_urls)
        and accepted_urls.issubset(current_urls | surplus_urls)
        and (not surplus_urls or surplus_authorized and journal_proof)
        and featured_matches
    )
    # Keep the exact persisted V2 media contract (including search and waiver
    # evidence) and add only the read-back fact that the publisher can know.
    # ProductionValidateStage passes this same serialized shape to the
    # checklist before READY is written; rebuilding a reduced mapping here was
    # a source of validation drift.
    context: dict[str, Any] = dict(media)
    context["visual_identity_policy"] = 4
    context["visual_identity_evidence_valid"] = evidence_matches_current_media
    context["accepted_media_ids"] = sorted(accepted_ids)
    context["applied_inline_ids"] = sorted(
        int(item["media_id"])
        for item in accepted
        if _canonical_image_url(str(item.get("media_url") or "")) in applied_urls
        and isinstance(item.get("media_id"), int)
        and not isinstance(item.get("media_id"), bool)
    )
    context["accepted_working_not_applied_ids"] = sorted(
        int(item["media_id"])
        for item in accepted
        if _canonical_image_url(str(item.get("media_url") or "")) in surplus_urls
        and isinstance(item.get("media_id"), int)
        and not isinstance(item.get("media_id"), bool)
    )
    context["media_application_contract"] = {
        "accepted": sorted(accepted_ids),
        "applied": context["applied_inline_ids"],
        "accepted_working_not_applied": context["accepted_working_not_applied_ids"],
        "layout_reduction_authorized": bool(reduction_authorized),
        "journal_readback_proven": bool(journal_proof),
    }
    context["inline"] = {**inline, "required": required, "accepted": accepted}
    context["featured"] = featured
    return context


def _uncertain_publication_path(root: Path, post_id: int) -> Path:
    return root / "work" / "v2-publication-uncertain" / f"{int(post_id)}.json"


def _persist_publish_intent(
    root: Path,
    post_id: int,
    *,
    ready_hash: str,
    ready_manifest: str | None,
    publication_operation_id: str,
    rest_empty_v1: bool | None = None,
) -> None:
    """Durably fence a publication before sending its remote write."""
    path = _uncertain_publication_path(root, post_id)
    directory_preexisted = path.parent.is_dir()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "post_id": int(post_id),
        "status": "intent",
        "intent_persisted": True,
        "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "ready_hash": str(ready_hash or ""),
        "ready_manifest_sha256": hashlib.sha256(
            ready_manifest.encode("utf-8")
        ).hexdigest() if ready_manifest is not None else "",
        "publication_operation_id": str(publication_operation_id),
        **({"rest_empty_v1": rest_empty_v1} if rest_empty_v1 is not None else {}),
    }
    atomic_write_text(
        path, json.dumps(payload, ensure_ascii=False, indent=2)
    )
    # atomic_write_text fsyncs the file before rename; fsync the directory too
    # so a sudden process/host interruption cannot lose the new fence entry.
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    if not directory_preexisted:
        parent_fd = os.open(
            path.parent.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)


class _PublishedReadbackConflict(WorkflowError):
    """An authenticated GET confirmed publish, but not our operation."""


def _mark_uncertain_publication(
    root: Path,
    post_id: int,
    error: Exception | str,
    *,
    ready_hash: str = "",
    ready_manifest: str | None = None,
    publication_operation_id: str = "",
    rest_empty_v1: bool | None = None,
    published_observed: bool = False,
) -> None:
    """Persist a local fence after a publish request with unknown outcome.

    A timeout can happen after WordPress commits the post but before the client
    receives the response.  The marker makes the next invocation reconcile the
    remote status first instead of issuing a blind second publish request.
    """
    try:
        atomic_write_text(
            _uncertain_publication_path(root, post_id),
            json.dumps(
                {
                    "post_id": int(post_id),
                    "status": "uncertain",
                    "error_type": type(error).__name__ if isinstance(error, Exception) else "Error",
                    "error": str(error)[:240],
                    "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                    "ready_hash": str(ready_hash or ""),
                    "ready_manifest_sha256": hashlib.sha256(
                        ready_manifest.encode("utf-8")
                    ).hexdigest() if ready_manifest is not None else "",
                    "publication_operation_id": str(publication_operation_id or ""),
                    **(
                        {"published_observed": True}
                        if published_observed or isinstance(error, _PublishedReadbackConflict)
                        else {}
                    ),
                    **({"rest_empty_v1": rest_empty_v1} if rest_empty_v1 is not None else {}),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    except OSError:
        # The remote read-back below remains the primary safety mechanism.  A
        # filesystem failure must not turn a known pending post into a publish.
        pass


def _clear_uncertain_publication(root: Path, post_id: int) -> None:
    try:
        _uncertain_publication_path(root, post_id).unlink(missing_ok=True)
    except OSError:
        pass


def _append_publish_telemetry(root: str | Path, event: str, **fields: Any) -> None:
    """Keep telemetry auxiliary to the WordPress publication transaction."""
    try:
        append_telemetry(root, event, **fields)
    except Exception as exc:  # noqa: BLE001 - never change a publish decision
        logging.getLogger(__name__).warning(
            "publication telemetry unavailable for %s: %s", event, type(exc).__name__
        )


def _publication_operation_outcome(observed: Any, expected: str) -> str:
    if observed == expected:
        return "matched"
    if observed is None or (isinstance(observed, str) and not observed.strip()):
        return "operation_id_missing"
    return "operation_id_mismatch"


def _publication_id_fingerprint(value: Any) -> str | None:
    # WordPress meta is not trusted log data. Only report a fingerprint of the
    # exact lowercase hexadecimal shape produced by uuid4().hex, never raw meta.
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value):
        return hashlib.sha256(value.encode("ascii")).hexdigest()
    return None


def _transient_publish_read_error(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    return isinstance(exc, WordPressError) and (
        exc.error_code == "wordpress_timeout" or exc.status_code in {408, 500, 502, 503, 504}
    )


def _sync_published_readback(
    client: WordPressClient,
    config: Config,
    post_id: int,
    *,
    root: Path | None,
    ready_hash: str,
    ready_manifest: str | None,
    expected_operation_id: str = "",
    rest_empty_v1: bool | None = None,
    initial_post: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Synchronize V2 state and verify an already-published WordPress post."""
    from dataclasses import replace as _replace
    from .pipeline_v2.model import LifecycleState as _LifecycleState, Phase as _Phase, WorkState
    from .pipeline_v2.operational import WordPressStateBackend as _V2Backend
    from .pipeline_v2.state_store import StateStore as _V2Store

    def observed_v2(post: dict[str, Any]) -> WorkState | None:
        meta = post.get("meta") if isinstance(post.get("meta"), dict) else {}
        if rest_empty_v1 is True:
            if not _rest_empty_v1_work_state(post):
                raise WorkflowError("V1 work state changed during publish read-back")
            return None
        if rest_empty_v1 is not False and meta.get("_hermes_work_state") is None:
            return None
        # Never load through StateStore's legacy fallback: corrupt, removed
        # or blank V2 metadata is not the authorized V2 READY transaction.
        raw = _read_v2_work_state(post)
        if not raw or raw.get("state") not in {"ready", "published"}:
            raise WorkflowError("V2 publish reconciliation requires READY or PUBLISHED state")
        return WorkState.from_dict(raw)

    def checked_snapshot(
        phase: str, first_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # One additional authenticated GET is allowed only at the first
        # confirmation boundary. A contradictory identity is not transient.
        max_reads = 2 if phase == "initial" else 1
        publish_seen = phase != "initial"
        for attempt in range(1, max_reads + 1):
            try:
                post = (
                    first_snapshot if attempt == 1 and first_snapshot is not None
                    else client.get_post(post_id)
                )
            except (WordPressError, OSError) as exc:
                transient = _transient_publish_read_error(exc)
                http_status = exc.status_code if isinstance(exc, WordPressError) else None
                _append_publish_telemetry(
                    root or Path("."), "publish_readback", post_id=post_id, phase=phase,
                    attempt=attempt, outcome="transient_read_error" if transient else "read_error",
                    http_status=http_status if type(http_status) is int and 100 <= http_status <= 599 else None,
                )
                if transient and attempt < max_reads:
                    continue
                error_type = _PublishedReadbackConflict if publish_seen else WorkflowError
                raise error_type("WordPress publish read-back unavailable") from exc
            if post.get("status") != "publish":
                _append_publish_telemetry(
                    root or Path("."), "publish_readback", post_id=post_id, phase=phase,
                    attempt=attempt, outcome="status_mismatch", wordpress_published=False,
                )
                error_type = _PublishedReadbackConflict if publish_seen else WorkflowError
                raise error_type("WordPress publish read-back mismatch")
            publish_seen = True
            if not expected_operation_id:
                return post
            meta = post.get("meta") if isinstance(post.get("meta"), dict) else {}
            observed = meta.get(META_PUBLISH_OPERATION_ID)
            outcome = _publication_operation_outcome(observed, expected_operation_id)
            if phase == "initial" or outcome != "matched":
                _append_publish_telemetry(
                    root or Path("."), "publish_readback", post_id=post_id, phase=phase,
                    attempt=attempt, outcome=outcome, wordpress_published=True,
                    expected_operation_id_sha256=_publication_id_fingerprint(expected_operation_id),
                    observed_operation_id_sha256=_publication_id_fingerprint(observed),
                )
            if outcome == "matched":
                return post
            if outcome == "operation_id_mismatch":
                raise _PublishedReadbackConflict(
                    "publication operation identity read-back mismatch" if phase == "initial"
                    else "publication operation identity changed during read-back"
                )
            if attempt == max_reads:
                raise _PublishedReadbackConflict("publication operation identity missing in read-back")
        raise AssertionError("bounded publish read-back exhausted without outcome")

    v2_post = checked_snapshot("initial", initial_post)
    initial_meta = v2_post.get("meta") if isinstance(v2_post.get("meta"), dict) else {}
    v2_current = observed_v2(v2_post)
    if rest_empty_v1 is not None:
        if ready_hash and initial_meta.get(META_READY_HASH) != ready_hash:
            raise WorkflowError("ready hash read-back mismatch after publish")
        if ready_manifest is not None and initial_meta.get(META_READY_MANIFEST) != ready_manifest:
            raise WorkflowError("ready manifest read-back mismatch after publish")
    legacy_state = read_state(v2_post).get("state")
    if legacy_state != STATE_PUBLISHED:
        # A publish request can commit the WordPress status while the legacy
        # marker is omitted by a partial/older REST meta response. The remote
        # status is already the write-side effect, so reconcile only the safe
        # READY/legacy-empty states instead of issuing a second publication.
        if legacy_state not in (None, STATE_READY):
            raise WorkflowError("legacy publish read-back mismatch")
        client.update_post(post_id, {"meta": {"_hermes_state": STATE_PUBLISHED}})
        v2_post = checked_snapshot("legacy_sync")
        if read_state(v2_post).get("state") != STATE_PUBLISHED:
            raise WorkflowError("legacy publish state reconciliation read-back mismatch")
        v2_current = observed_v2(v2_post)
    if v2_current is not None:
        _V2Store(_V2Backend(client)).mark_published(
            post_id,
            _replace(v2_current, state=_LifecycleState.PUBLISHED, phase=_Phase.PUBLISH, blocker=None),
        )
    verified_post = checked_snapshot("final")
    verified_meta = verified_post.get("meta") if isinstance(verified_post.get("meta"), dict) else {}
    if read_state(verified_post).get("state") != STATE_PUBLISHED:
        raise WorkflowError("publication read-back mismatch")
    v2_verified = observed_v2(verified_post)
    if v2_current is not None and (
        v2_verified is None
        or v2_verified.state is not _LifecycleState.PUBLISHED
        or v2_verified.phase is not _Phase.PUBLISH
    ):
        raise WorkflowError("V2 state read-back mismatch after publish")
    if ready_hash and verified_meta.get(META_READY_HASH) != ready_hash:
        raise WorkflowError("ready hash read-back mismatch after publish")
    if ready_manifest is not None and verified_meta.get(META_READY_MANIFEST) != ready_manifest:
        raise WorkflowError("ready manifest read-back mismatch after publish")
    return verified_post


def _reconcile_uncertain_publication(
    client: WordPressClient,
    config: Config,
    root: Path,
    post: dict[str, Any],
    post_id: int,
    *,
    ready_hash: str,
    ready_manifest: str | None,
) -> dict[str, Any] | None:
    """Resolve a previous ambiguous publish before allowing another write.

    Returns an outcome when the request must stop.  ``None`` means the remote
    post was read as pending and it is safe for the caller to try publishing.
    """
    marker = _uncertain_publication_path(root, post_id)
    if not marker.is_file():
        return None
    try:
        marker_data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "status": "retry",
            "reason": "marcador de publicação incerta ilegível; reconciliação bloqueada",
            "state": STATE_READY,
            "uncertain": True,
        }
    if not isinstance(marker_data, dict):
        marker_data = {}
    if post.get("status") == "pending":
        if marker_data.get("published_observed") is True:
            # A pending projection cannot disprove a publication that an
            # authenticated read already observed. Keep the original fence.
            return {
                "post_id": post_id,
                "wordpress_changed": False,
                "status": "retry",
                "reason": "status pending diverge de publicação anteriormente observada",
                "state": STATE_READY,
                "uncertain": True,
            }
        _clear_uncertain_publication(root, post_id)
        return None
    if post.get("status") != "publish":
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "status": "retry",
            "reason": "resultado anterior da publicação permanece incerto; status remoto não confirma publicação",
            "state": STATE_READY,
        }
    legacy_state = read_state(post).get("state")
    meta = post.get("meta") if isinstance(post.get("meta"), dict) else {}
    raw_mode = marker_data.get("rest_empty_v1")
    rest_empty_v1 = raw_mode if type(raw_mode) is bool else None
    v2_state = _read_v2_work_state(post, allow_rest_empty_v1=rest_empty_v1 is True)
    marker_has_identity = bool(
        marker_data.get("ready_hash") or marker_data.get("ready_manifest_sha256")
    )
    publication_operation_id = str(marker_data.get("publication_operation_id") or "")
    if not publication_operation_id:
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "status": "retry",
            "reason": "marcador antigo sem identidade de operação; autoria da publicação não comprovada",
            "state": STATE_READY,
            "uncertain": True,
        }
    observed_operation = meta.get(META_PUBLISH_OPERATION_ID)
    operation_outcome = _publication_operation_outcome(observed_operation, publication_operation_id)
    operation_identity_matches = operation_outcome == "matched"
    identity_matches = bool(
        marker_data.get("ready_hash")
        and marker_data.get("ready_hash") == ready_hash
        and marker_data.get("ready_manifest_sha256")
        and isinstance(ready_manifest, str)
        and marker_data.get("ready_manifest_sha256")
        == hashlib.sha256(ready_manifest.encode("utf-8")).hexdigest()
        and meta.get(META_READY_HASH) == ready_hash
        and meta.get(META_READY_MANIFEST) == ready_manifest
        and (
            v2_state is not None and v2_state.get("state") in {"ready", "published"}
            if rest_empty_v1 is False
            else v2_state is None or v2_state.get("state") in {"ready", "published"}
        )
    )
    # Legacy markers without identity retain the previous strict contract:
    # only an already-confirmed PUBLISHED projection is accepted. New markers
    # may reconcile READY/absent projections only when the exact READY payload
    # that preceded the ambiguous request still matches remotely.
    if not operation_identity_matches:
        _append_publish_telemetry(
            root, "publish_readback", post_id=post_id, phase="reconcile_fence", attempt=1,
            outcome=operation_outcome, wordpress_published=True,
            expected_operation_id_sha256=_publication_id_fingerprint(publication_operation_id),
            observed_operation_id_sha256=_publication_id_fingerprint(observed_operation),
        )
        reason = "operação remota não corresponde ao identificador de publicação autorizado"
        if operation_outcome == "operation_id_missing":
            reason += " (identificador ausente na leitura)"
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "status": "retry",
            "reason": reason,
            "state": STATE_READY,
            "uncertain": True,
        }
    if legacy_state != STATE_PUBLISHED and not (
        marker_has_identity and identity_matches and operation_identity_matches
    ):
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "status": "retry",
            "reason": "publicação remota sem identidade READY comprovada; reconciliação bloqueada",
            "state": STATE_READY,
            "uncertain": True,
        }
    if legacy_state not in {None, STATE_READY, STATE_PUBLISHED}:
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "status": "retry",
            "reason": "estado legado incompatível com reconciliação de publicação",
            "state": STATE_READY,
            "uncertain": True,
        }
    if marker_has_identity and not identity_matches:
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "status": "retry",
            "reason": "identidade READY diverge do marcador de publicação incerta",
            "state": STATE_READY,
            "uncertain": True,
        }
    try:
        _sync_published_readback(
            client, config, post_id, root=root,
            ready_hash=ready_hash,
            ready_manifest=ready_manifest,
            expected_operation_id=publication_operation_id,
            rest_empty_v1=rest_empty_v1,
        )
    except Exception as exc:  # noqa: BLE001 - preserve the no-blind-retry fence
        _mark_uncertain_publication(
            root, post_id, exc, ready_hash=ready_hash, ready_manifest=ready_manifest,
            publication_operation_id=publication_operation_id,
            rest_empty_v1=rest_empty_v1,
            published_observed=True,
        )
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "status": "retry",
            "reason": f"publicação já pode ter ocorrido, mas a reconciliação falhou: {exc}",
            "state": STATE_READY,
            "uncertain": True,
        }
    _clear_uncertain_publication(root, post_id)
    _append_publish_telemetry(root, "post_publication_reconciled", post_id=post_id)
    _append_publish_telemetry(root, "post_published_reconciled", post_id=post_id)
    _append_publish_telemetry(
        root,
        "v2_funnel",
        stage="published",
        post_id=int(post_id),
        integrity="reconciled_after_uncertain_write",
    )
    return {
        "post_id": post_id,
        "wordpress_changed": True,
        "status": "published",
        "status_after": "publish",
        "integrity": "reconciled_after_uncertain_write",
        "state": STATE_PUBLISHED,
        "reconciled": True,
        "publication_kind": "reconciled",
    }


def _reopen_v2_ready_after_publish_block(
    client: WordPressClient,
    config: Config,
    root: Path,
    post_id: int,
    post: dict[str, Any],
    checklist: dict[str, Any],
) -> bool:
    """Move a V2 READY post back to pending after a publish-gate failure.

    The publisher writes the legacy ``blocked`` marker for compatibility, but
    V2 is the queue's source of truth.  Leaving its state as READY makes the
    scheduler discard the post forever.  Reopen V2 first and verify the
    round-trip before writing the legacy marker, so a failed V2 write cannot
    create a second inconsistent terminal state.
    """
    if config.dry_run:
        return True
    raw_v2 = _read_v2_work_state(post)
    if not isinstance(raw_v2, dict) or raw_v2.get("state") != "ready":
        return True

    from .pipeline_v2.classifier import (
        EDITORIAL_BLOCKERS,
        MEDIA_BLOCKERS,
        OPERATIONAL_BLOCKERS,
        blocker_for_gate,
    )
    from .pipeline_v2.model import BlockerCode, LifecycleState, Phase, WorkState
    from .pipeline_v2.operational import WordPressStateBackend
    from .pipeline_v2.state_store import StateStore

    def _failure_target(item: dict[str, Any]) -> tuple[int, Phase, BlockerCode]:
        raw_blocker = str(item.get("blocker") or "")
        try:
            blocker = BlockerCode(raw_blocker) if raw_blocker else None
        except ValueError:
            blocker = None
        blocker = blocker or blocker_for_gate(str(item.get("name") or ""))
        blocker = blocker or BlockerCode.WORDPRESS_ERROR
        raw_phase = str(item.get("phase") or "")
        try:
            phase = Phase(raw_phase)
        except ValueError:
            phase = Phase.MEDIA if raw_phase.startswith("media") else None
        if phase is None:
            if blocker in MEDIA_BLOCKERS:
                phase = Phase.MEDIA
            elif blocker in EDITORIAL_BLOCKERS:
                phase = Phase.EDITORIAL
            elif blocker is BlockerCode.RELEVANCE_UNCERTAIN:
                phase = Phase.RELEVANCE
            else:
                phase = Phase.VALIDATE
        # Operational failures have precedence because they should be retried
        # before asking editorial/media rework to compensate for an unavailable
        # provider. Within a category, checklist order is the deterministic
        # tiebreaker; alphabetical gate names are never used.
        priority = (
            0 if blocker in OPERATIONAL_BLOCKERS else
            1 if blocker in MEDIA_BLOCKERS else
            2 if blocker in EDITORIAL_BLOCKERS else
            3 if blocker is BlockerCode.RELEVANCE_UNCERTAIN else
            4
        )
        return priority, phase, blocker

    failures = [
        item for item in (checklist.get("items") or [])
        if isinstance(item, dict) and item.get("status") in {"fail", "error"}
    ]
    if failures:
        _priority, phase, blocker, _index = min(
            (
                (*_failure_target(item), index)
                for index, item in enumerate(failures)
            ),
            key=lambda candidate: (candidate[0], candidate[3]),
        )
    else:
        phase, blocker = Phase.VALIDATE, BlockerCode.WORDPRESS_ERROR
    try:
        current = WorkState.from_dict(raw_v2)
        if current.state is not LifecycleState.READY:
            return True
        reopened = WorkState(
            state=LifecycleState.PENDING,
            phase=phase,
            blocker=blocker,
            retry=current.retry,
            relevance_approved=current.relevance_approved,
            media=current.media,
        )
        store = StateStore(WordPressStateBackend(client))
        store.commit(post_id, reopened)
        readback = client.get_post(post_id)
        verified = _read_v2_work_state(readback)
        if verified != reopened.to_dict():
            raise WorkflowError(f"V2 reabertura read-back failed for post {post_id}")
    except Exception as exc:  # noqa: BLE001 - publish remains fail-closed
        append_telemetry(
            root,
            "v2_reopen_after_publish_block_failed",
            post_id=post_id,
            error=str(exc)[:240],
        )
        return False
    append_telemetry(
        root,
        "v2_reopened_after_publish_block",
        post_id=post_id,
        phase=phase.value,
        blocker=blocker.value,
    )
    return True


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
    try:
        load_epoch_protection(root).assert_writable(post_id, effect="publisher")
    except EpochProtectionError as exc:
        return {"post_id": int(post_id), "wordpress_changed": False, "status": "blocked", "reason": str(exc), "epoch_protected": True}
    post = client.get_post(post_id)
    post_meta = post.get("meta") if isinstance(post.get("meta"), dict) else {}
    uncertain_outcome = _reconcile_uncertain_publication(
        client,
        config,
        root,
        post,
        post_id,
        ready_hash=str(post_meta.get(META_READY_HASH) or ""),
        ready_manifest=post_meta.get(META_READY_MANIFEST)
        if isinstance(post_meta.get(META_READY_MANIFEST), str)
        else None,
    )
    if uncertain_outcome is not None:
        return uncertain_outcome
    if post.get("status") != "pending":
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "status": "skipped",
            "reason": f"post status is {post.get('status')}, expected pending",
        }
    v2_state = _read_v2_work_state(post, allow_rest_empty_v1=True)
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
        v2_reopened = _reopen_v2_ready_after_publish_block(
            client,
            config,
            root,
            post_id,
            post,
            {"items": [{
                "status": "fail",
                "name": "conteudo_sem_metadados_operacionais",
                "phase": "editorial",
            }]},
        )
        if not v2_reopened:
            return {
                "post_id": post_id,
                "wordpress_changed": False,
                "status": "retry",
                "reason": "falha ao reabrir estado V2 após bloqueio de envelope operacional",
                "state": state,
                "retryable": True,
            }
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
        v2_reopened = _reopen_v2_ready_after_publish_block(
            client,
            config,
            root,
            post_id,
            post,
            {"items": [{"status": "fail", "name": "idioma_pt_br", "phase": "editorial"}]},
        )
        if not v2_reopened:
            return {
                "post_id": post_id,
                "wordpress_changed": False,
                "status": "retry",
                "reason": "falha ao reabrir estado V2 após bloqueio de idioma",
                "state": state,
                "retryable": True,
            }
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
    publish_media_context = _publish_v2_media_context(v2_state, post, root=root)
    if state == STATE_READY:
        raw_meta = post.get("meta")
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        stored = parse_manifest(meta.get(META_READY_MANIFEST))
        if (
            publish_media_context is None
            and manifest_matches(post, stored, state_info["ready_hash"], policy_version=config.policy_version)
        ):
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
                expected_post=post,
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
            media_source_policy=str(getattr(config, "media_source_policy", "strict")),
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
    checklist_media_context = publish_media_context
    checklist = run_pre_publish_checklist(
        post=post,
        editorial=editorial,
        content=_raw_content(post),
        backup_path=backup,
        config=config,
        client=client,
        media_context=checklist_media_context,
    )
    if checklist["failed"]:
        retryable_errors = [
            item for item in checklist.get("items", [])
            if isinstance(item, dict) and item.get("status") == "error" and item.get("retryable")
        ]
        if retryable_errors:
            v2_reopened = _reopen_v2_ready_after_publish_block(
                client, config, root, post_id, post, checklist
            )
            if not v2_reopened:
                return {
                    "post_id": post_id,
                    "wordpress_changed": False,
                    "status": "retry",
                    "reason": "falha ao reabrir estado V2 após erro técnico do checklist",
                    "checklist": checklist,
                    "retryable_errors": retryable_errors,
                    "state": state,
                    "retryable": True,
                }
            try:
                from .observability import append_telemetry

                append_telemetry(
                    root,
                    "publish_media_validation_retry",
                    post_id=post_id,
                    failed_checks=sorted({str(item.get("name") or "media_validation") for item in retryable_errors}),
                )
            except Exception:  # noqa: BLE001 - observability never changes publish safety
                pass
            return {
                "post_id": post_id,
                "wordpress_changed": False,
                "status": "retry",
                "reason": "validação técnica da mídia inconclusiva; publicação não realizada",
                "checklist": checklist,
                "retryable_errors": retryable_errors,
                "state": "pending" if v2_state is not None else state,
                "v2_reopened": v2_state is not None,
            }
        # V2 e a fonte de verdade da fila. Reabre-o antes do marcador legado;
        # sem isso o publisher deixa _hermes_work_state=ready e o scheduler
        # descarta o post para sempre, mesmo com _hermes_state=blocked.
        v2_reopened = _reopen_v2_ready_after_publish_block(
            client, config, root, post_id, post, checklist
        )
        if not v2_reopened:
            return {
                "post_id": post_id,
                "wordpress_changed": False,
                "status": "retry",
                "reason": "falha ao reabrir estado V2 após bloqueio do checklist",
                "checklist": checklist,
                "state": state,
                "retryable": True,
            }
        # Registra o bloqueio SEM remover editorial.latest.json: o post
        # continua candidato nas proximas janelas (o conteudo no WP pode ja
        # estar bom — remover o latest orfana o post e o publish nunca mais o
        # tenta). O agente ve o editorial.blocked.json nos cards, corrige
        # (re-apply) e a proxima janela publica.
        _record_blocked(root, post_id, checklist)
        legacy_state_persisted = _write_state_markers(
            client,
            config,
            post_id,
            STATE_BLOCKED,
            root=root,
            last_error="checklist pre-publicacao com falhas",
        )
        if not legacy_state_persisted:
            return {
                "post_id": post_id,
                "wordpress_changed": False,
                "status": "retry",
                "reason": "V2 reaberto, mas o marcador legado blocked não foi confirmado",
                "checklist": checklist,
                "state": "pending" if v2_state is not None else STATE_BLOCKED,
                "v2_reopened": v2_state is not None,
                "legacy_state_persisted": False,
                "retryable": True,
            }
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "status": "blocked",
            "reason": "checklist pre-publicacao com falhas (STALE/legado revalidado)",
            "checklist": checklist,
            "reopened_for_rework": True,
            "state": "pending" if v2_state is not None else STATE_BLOCKED,
            "v2_reopened": v2_state is not None,
            "legacy_state_persisted": legacy_state_persisted,
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
        expected_post=post,
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
    expected_post: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Publica de fato e marca PUBLISHED (gate PUBLISH_ENABLED já verificado)."""
    if not config.publish_enabled:
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "status": "blocked",
            "reason": "PUBLISH_ENABLED=false (gate de publicacao desligado)",
        }
    if expected_post is not None:
        try:
            current_post = client.get_post(post_id)
        except Exception as exc:  # noqa: BLE001 - no write was attempted
            return {
                "post_id": post_id,
                "wordpress_changed": False,
                "status": "retry",
                "reason": f"pre-publish concurrency read failed: {type(exc).__name__}",
                "retryable": True,
                "state": STATE_READY,
            }
        if _post_write_guard_signature(current_post) != _post_write_guard_signature(expected_post):
            return {
                "post_id": post_id,
                "wordpress_changed": False,
                "status": "retry",
                "reason": "WordPress post changed during publish preflight; refusing stale publish",
                "retryable": True,
                "state": STATE_READY,
            }
    # The same operation fence records one of three modes: exact V1 empty
    # sentinel (true), actual V2 READY (false), or neither / legacy (absent).
    rest_empty_v1: bool | None = None
    if expected_post is not None:
        if _rest_empty_v1_work_state(expected_post) and read_state(expected_post).get("state") == STATE_READY:
            rest_empty_v1 = True
        elif (_read_v2_work_state(expected_post) or {}).get("state") == "ready":
            rest_empty_v1 = False
    published_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    publication_operation_id = uuid.uuid4().hex
    publish_meta = {
        **build_state_markers(
            STATE_PUBLISHED,
            policy_version=config.policy_version,
            ready_hash=ready_hash,
        ),
    }
    publish_meta[META_PUBLISH_OPERATION_ID] = publication_operation_id
    if ready_manifest is not None:
        publish_meta[META_READY_MANIFEST] = ready_manifest
    try:
        _persist_publish_intent(
            root or Path("."),
            post_id,
            ready_hash=ready_hash,
            ready_manifest=ready_manifest,
            publication_operation_id=publication_operation_id,
            rest_empty_v1=rest_empty_v1,
        )
    except OSError as exc:
        return {
            "post_id": post_id,
            "wordpress_changed": False,
            "status": "retry",
            "reason": f"intenção de publicação não pôde ser persistida; write não enviado ({type(exc).__name__})",
            "retryable": True,
            "state": STATE_READY,
        }
    try:
        _append_publish_telemetry(
            root or Path("."),
            "v2_funnel",
            stage="publish_attempted",
            post_id=int(post_id),
            integrity=integrity,
            publication_operation_id=publication_operation_id,
        )
        result = client.publish(
            post_id,
            meta={"_ai_editor_published_at": published_at, **publish_meta},
            # Política do dono: post antigo em pending publica como data corrente
            # (não fica enterrado no passado do site).
            date_gmt=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        )
    except Exception as exc:  # noqa: BLE001 - POST outcome may be ambiguous
        try:
            observed = client.get_post(post_id)
        except Exception as read_exc:  # noqa: BLE001 - preserve the fence
            _mark_uncertain_publication(
                root or Path("."), post_id, exc,
                ready_hash=ready_hash, ready_manifest=ready_manifest,
                publication_operation_id=publication_operation_id,
                rest_empty_v1=rest_empty_v1,
            )
            _append_publish_telemetry(
                root or Path("."),
                "publish_outcome_uncertain",
                post_id=post_id,
                publish_error=type(exc).__name__,
                read_error=type(read_exc).__name__,
            )
            return {
                "post_id": post_id,
                "wordpress_changed": False,
                "status": "retry",
                "reason": "resultado da publicação incerto; leitura de reconciliação também falhou",
                "retryable": True,
                "uncertain": True,
                "state": STATE_READY,
            }
        if observed.get("status") == "pending":
            _clear_uncertain_publication(root or Path("."), post_id)
            _append_publish_telemetry(root or Path("."), "publish_write_not_applied", post_id=post_id)
            return {
                "post_id": post_id,
                "wordpress_changed": False,
                "status": "retry",
                "reason": f"publicação não confirmada; post continua pending ({type(exc).__name__})",
                "retryable": True,
                "state": STATE_READY,
            }
        try:
            reconciled_post = _sync_published_readback(
                client,
                config,
                post_id,
                root=root,
                ready_hash=ready_hash,
                ready_manifest=ready_manifest,
                expected_operation_id=publication_operation_id,
                rest_empty_v1=rest_empty_v1,
                initial_post=observed,
            )
        except Exception as sync_exc:  # noqa: BLE001 - do not retry blindly
            _mark_uncertain_publication(
                root or Path("."), post_id, sync_exc,
                ready_hash=ready_hash, ready_manifest=ready_manifest,
                publication_operation_id=publication_operation_id,
                rest_empty_v1=rest_empty_v1,
                published_observed=observed.get("status") == "publish",
            )
            _append_publish_telemetry(
                root or Path("."),
                "publish_outcome_uncertain",
                post_id=post_id,
                publish_error=type(exc).__name__,
                read_status=str(observed.get("status") or ""),
                reconciliation_error=type(sync_exc).__name__,
            )
            return {
                "post_id": post_id,
                "wordpress_changed": False,
                "status": "retry",
                "reason": f"post pode ter sido publicado, mas a reconciliação falhou: {sync_exc}",
                "retryable": True,
                "uncertain": True,
                "state": STATE_READY,
            }
        _clear_uncertain_publication(root or Path("."), post_id)
        _append_publish_telemetry(root or Path("."), "post_publication_reconciled", post_id=post_id)
        _append_publish_telemetry(root or Path("."), "post_published_reconciled", post_id=post_id)
        _append_publish_telemetry(
            root or Path("."),
            "v2_funnel",
            stage="published",
            post_id=int(post_id),
            integrity="reconciled_after_uncertain_write",
        )
        return {
            "post_id": post_id,
            "wordpress_changed": True,
            "status": "published",
            "status_after": reconciled_post.get("status"),
            "integrity": "reconciled_after_uncertain_write",
            "state": STATE_PUBLISHED,
            "reconciled": True,
            "publication_kind": "reconciled",
        }
    try:
        _sync_published_readback(
            client, config, post_id, root=root,
            ready_hash=ready_hash, ready_manifest=ready_manifest,
            expected_operation_id=publication_operation_id,
            rest_empty_v1=rest_empty_v1,
        )
    except Exception as exc:
        _mark_uncertain_publication(
            root or Path("."), post_id, exc,
            ready_hash=ready_hash, ready_manifest=ready_manifest,
            publication_operation_id=publication_operation_id,
            rest_empty_v1=rest_empty_v1,
        )
        try:
            _append_publish_telemetry(
                root or Path("."), "publish_v2_sync_failed",
                post_id=post_id, error=str(exc)[:200],
            )
        except Exception:  # noqa: BLE001 - defensive; helper is already fail-soft
            pass
        raise
    _clear_uncertain_publication(root or Path("."), post_id)
    _append_publish_telemetry(
        root or Path("."),
        "v2_funnel",
        stage="published",
        post_id=int(post_id),
        integrity=integrity,
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
        "publication_kind": "new",
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
    protection = load_epoch_protection(root)
    outcomes: list[dict[str, Any]] = _reconcile_persisted_uncertain_publications(
        client, config, root,
    )
    # The same ID can reappear in a contradictory pending projection. Its
    # unresolved fence is already counted above; do not offer it again here.
    still_uncertain = {outcome["post_id"] for outcome in outcomes if outcome.get("uncertain")}
    # Paginacao completa: a fila pode ter mais que 100 pending, e um post READY
    # alem da pagina 1 nao pode ficar invisivel na janela de publicacao.
    posts: list[dict[str, Any]] = []
    page = 1
    while True:
        chunk = client.list_pending(page=page, per_page=100)
        if page > 100:
            raise WorkflowError(
                f"pending publish queue exceeds 100 pages; resume scan at page {page}"
            )
        posts.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
    for candidate in posts:
        candidate_id = candidate.get("id")
        if not isinstance(candidate_id, int) or candidate_id in still_uncertain:
            continue
        if protection.is_protected(candidate_id):
            outcomes.append({"post_id": candidate_id, "wordpress_changed": False, "status": "blocked", "reason": "epoch_protected_transaction", "epoch_protected": True})
            continue
        v2_state = _read_v2_work_state(candidate, allow_rest_empty_v1=True)
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
        published_new = sum(
            1
            for outcome in outcomes
            if outcome.get("wordpress_changed")
            and outcome.get("publication_kind") != "reconciled"
        )
        if limit and published_new >= limit:
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
        if (
            outcome.get("status") == "published"
            and outcome.get("wordpress_changed")
            and outcome.get("publication_kind") != "reconciled"
        ):
            # Base durável do relatório: custo e publicações usam a mesma
            # janela móvel de 24h, sem inferir o denominador da janela atual.
            _append_publish_telemetry(root, "post_published", post_id=candidate_id)
    return outcomes


def _reconcile_persisted_uncertain_publications(
    client: WordPressClient,
    config: Config,
    root: Path,
) -> list[dict[str, Any]]:
    """Reconcile publish fences before scanning the pending queue.

    A post that was committed remotely can disappear from ``list_pending``.
    Its local fence is therefore an independent recovery queue and must be
    read by ID before the publisher considers new pending candidates.
    """
    marker_dir = Path(root) / "work" / "v2-publication-uncertain"
    if not marker_dir.is_dir():
        return []
    outcomes: list[dict[str, Any]] = []
    protection = load_epoch_protection(root)
    for marker in sorted(marker_dir.glob("*.json")):
        try:
            post_id = int(marker.stem)
        except ValueError:
            continue
        if protection.is_protected(post_id):
            outcomes.append({"post_id": post_id, "wordpress_changed": False, "status": "blocked", "reason": "epoch_protected_transaction", "epoch_protected": True})
            continue
        try:
            with _acquire_post_lock(root, config, post_id) as lock_handle:
                guarded_client = _guard_post_client(client, lock_handle, root=root, post_id=post_id)
                post = guarded_client.get_post(post_id)
                if config.dry_run:
                    outcomes.append({
                        "post_id": post_id,
                        "wordpress_changed": False,
                        "status": "skipped",
                        "reason": "dry-run: reconciliação remota não sincronizada",
                        "dry_run": True,
                        "uncertain": True,
                        "state": STATE_READY,
                    })
                    continue
                meta = post.get("meta") if isinstance(post.get("meta"), dict) else {}
                outcome = _reconcile_uncertain_publication(
                    guarded_client,
                    config,
                    root,
                    post,
                    post_id,
                    ready_hash=str(meta.get(META_READY_HASH) or ""),
                    ready_manifest=meta.get(META_READY_MANIFEST)
                    if isinstance(meta.get(META_READY_MANIFEST), str)
                    else None,
                )
        except Exception as exc:  # noqa: BLE001 - keep fence for next window
            _append_publish_telemetry(
                root,
                "publish_uncertain_read_failed",
                post_id=post_id,
                error=type(exc).__name__,
            )
            outcomes.append({
                "post_id": post_id,
                "wordpress_changed": False,
                "status": "retry",
                "reason": f"leitura do marcador de publicação incerta falhou: {exc}",
                "uncertain": True,
                "state": STATE_READY,
            })
            continue
        if outcome is not None:
            outcomes.append(outcome)
        else:
            _append_publish_telemetry(
                root,
                "publish_uncertain_cleared_pending",
                post_id=post_id,
            )
    return outcomes


def _discover_trailer(
    editorial: dict[str, Any], config: Config, *, root: Path | None = None
) -> tuple[dict[str, str] | None, str]:
    """Discover a YouTube trailer for game content; fail-closed to None."""
    game_names = split_game_names(editorial.get("game_name"))
    if not game_names:
        return None, "not_applicable"
    statuses: list[str] = []
    first_verified: dict[str, str] | None = None
    for game_name in game_names:
        try:
            trailer, status = find_cached_game_trailer_with_status(
                game_name, root=root, timeout=config.http_timeout,
                discover=find_game_trailer_with_status,
            )
        except TrailerError:
            statuses.append("search_failed")
            continue
        statuses.append(status)
        if trailer is not None and first_verified is None:
            first_verified = trailer
    if first_verified is not None:
        return first_verified, "found"
    if "search_failed" in statuses:
        return None, "search_failed"
    if "unverified_candidates" in statuses:
        return None, "unverified_candidates"
    if all(status == "official_not_found" for status in statuses):
        return None, "official_not_found"
    return None, "search_failed"


def attach_trailer_audit(
    editorial: dict[str, Any],
    trailer: dict[str, str] | None,
    *,
    search_status: str = "official_not_found",
    root: Path | None = None,
) -> dict[str, Any]:
    """Attach code-generated evidence when no official trailer is found."""
    result = dict(editorial)
    game_name = editorial.get("game_name")
    checked_at = (
        cached_game_trailer_checked_at(root, game_name)
        if isinstance(game_name, str) and game_name.strip()
        else None
    )
    if (
        isinstance(game_name, str)
        and game_name.strip()
        and trailer is None
        and search_status == "official_not_found"
        and checked_at is not None
    ):
        result["trailer_unavailable"] = True
        result["trailer_search_evidence"] = {
            "query": trailer_search_query(game_name),
            "provider": "youtube",
            "searched_at": checked_at,
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


def _post_write_guard_signature(post: dict[str, Any]) -> str:
    """Fingerprint fields whose concurrent change invalidates a long preflight."""
    meta = post.get("meta") if isinstance(post.get("meta"), dict) else {}
    guarded_meta = {
        key: meta.get(key)
        for key in sorted(meta)
        if key.startswith("_hermes") or key.startswith("rank_math_") or key == "original_link"
    }
    fields = {
        "id": post.get("id"),
        "status": post.get("status"),
        "title": post.get("title"),
        "content": post.get("content"),
        "excerpt": post.get("excerpt"),
        "featured_media": post.get("featured_media"),
        "categories": post.get("categories"),
        "tags": post.get("tags"),
        "modified_gmt": post.get("modified_gmt"),
        "meta": guarded_meta,
    }
    payload = json.dumps(fields, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()



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
    candidatos_escrita: list[dict[str, Any]] = []
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
                    candidatos_escrita.append({
                        "record": registro,
                        "state": destino,
                        "attempts": int(info.get("attempts") or 0),
                    })
                migrados.append(registro)
            if len(posts) < 100:
                break  # última página
            pagina += 1
            if pagina > 200:
                break  # limite de segurança
        if limit and vistos >= limit:
            break
    if apply and candidatos_escrita:
        # Preflight the complete write set before the first mutation, so a
        # quarantined legacy post cannot leave a partially-applied migration.
        for candidate in candidatos_escrita:
            assert_post_not_quarantined(
                root, candidate["record"]["post_id"], operation="migrate-state"
            )
        for candidate in candidatos_escrita:
            registro = candidate["record"]
            write_lock = RunSessionLock(Path(root) / "work" / "v2-run.lock")
            if not write_lock.acquire():
                gravou = False
            else:
                try:
                    gravou = _write_state_markers(
                        client, config, registro["post_id"], candidate["state"], root=root,
                        attempts=candidate["attempts"],
                    )
                finally:
                    write_lock.release()
            registro["written"] = bool(gravou)
            if gravou:
                aplicados += 1
            else:
                falhas += 1
                registro["write_error"] = "estado nao persistiu no WordPress"
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
    from .retry_policy import v1_deferral

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
    wordpress_pending_count = len(_pending_ids)
    for p in awaiting_wp:
        if isinstance(p.get("id"), int) and p["id"] not in _pending_ids:
            p["_wp_awaiting_human"] = True
            posts.append(p)
    posts = filter_quarantined_posts(root, posts)
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
    epoch = load_epoch_protection(root)
    for post in posts:
        post_id = post.get("id")
        if not isinstance(post_id, int):
            continue
        backups_dir = root / "backups" / str(post_id)
        eligibility = resolve_post_eligibility(post, root)
        state_info = read_state(post)
        state = eligibility.state
        v2_state = eligibility.v2_state
        if v2_state is not None:
            # V2 is authoritative whenever present. The legacy _hermes_state
            # marker is only a compatibility projection.
            state_info = {
                **state_info,
                "attempts": v2_state.retry.attempts,
                "next_retry_at": v2_state.retry.next_at or "",
                "no_progress_attempts": v2_state.retry.no_progress,
            }
        # Elegibilidade usa o estado RESOLVIDO (legado incluido): blocked sem
        # next_retry_at (ou com ele vencido) volta a agenda do monitor.
        effective_state = {**state_info, "state": state}
        deferred = (
            v1_deferral(root, post, digest=_post_write_guard_signature)
            if not eligibility.is_v2 and eligibility.eligible_for_v1 else {}
        )
        epoch_blocked = not eligibility.is_v2 and epoch.is_protected(post_id)
        if state == STATE_UNCERTAIN:
            uncertain_ids.append(post_id)
            if not deferred and not epoch_blocked and uncertain_second_pass_eligible(post):
                uncertain_second_pass_ids.append(post_id)
        elif state in {STATE_AWAITING_HUMAN, "human_required"} or post.get("_wp_awaiting_human"):
            awaiting_human_ids.append(post_id)
        elif state == STATE_SKIPPED:
            skipped_ids.append(post_id)
        elif state == STATE_READY:
            ready_ids.append(post_id)
        elif state == STATE_BLOCKED:
            blocked_ids.append(post_id)
            if not deferred and not epoch_blocked and retry_eligible(effective_state):
                eligible_rework.append(post_id)
            if _is_recent(post, cutoff):
                recent_blocked.append(post_id)
        elif state == STATE_PARTIAL:
            partial_ids.append(post_id)
            if not deferred and not epoch_blocked and retry_eligible(effective_state):
                eligible_rework.append(post_id)
        else:  # NEW / PROCESSING / V2 pending / desconhecido
            if not eligibility.is_v2 and not deferred and not epoch_blocked:
                unprocessed.append(post_id)
            if eligibility.is_v2 and state == "pending" and eligibility.cooldown_expired:
                v2_eligible.append(post_id)
            if not eligibility.is_v2 and not deferred and not epoch_blocked and _is_recent(post, cutoff):
                recent_unprocessed.append(post_id)
        title = (post.get("title") or {}).get("raw") or (post.get("title") or {}).get("rendered")
        rows.append(
            {
                "id": post_id,
                "date": post.get("date"),
                "date_gmt": post.get("date_gmt"),
                "word_count": word_count((post.get("content") or {}).get("rendered") or ""),
                "state": state,
                "state_source": eligibility.source,
                "eligibility_reason": eligibility.reason,
                "attempts": state_info["attempts"],
                "next_retry_at": deferred.get("not_before") or state_info["next_retry_at"],
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
                "v2": eligibility.is_v2,
                "phase": v2_state.phase.value if v2_state is not None else None,
                "blocker": v2_state.blocker.value if v2_state is not None and v2_state.blocker else None,
                "no_progress": v2_state.retry.no_progress if v2_state is not None else state_info.get("no_progress_attempts", 0),
                "eligible": (
                    eligibility.cooldown_expired and state == "pending"
                    if eligibility.is_v2 else eligibility.eligible_for_v1 and not bool(deferred) and not epoch_blocked
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
        # Keep ``pending`` aligned with the native WordPress status. The
        # combined queue still includes awaiting_human for diagnostics, but
        # it must not inflate the pending counter shown to operators.
        "pending": wordpress_pending_count,
        "wordpress_pending": wordpress_pending_count,
        "total_queue": len(rows),
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
    card, sem abrir blocked.json/checklist/logs/source. NEW e rework
    dividem a fila; a preparação concluída avança a rotação persistida.
    READY, adiamentos ativos e locks já observados nesta sessão não geram card.
    """
    from . import session_budget
    from .retry_policy import potential_v1_deferral, v1_deferral, v1_lane, v1_rotation
    from .content_quality import word_count
    from .html_cleaner import clean_html
    from .media.relevance import extract_entities

    selection_started_at = time.monotonic()
    selection_timeout = float(getattr(config, "cards_selection_timeout_seconds", 150.0))
    selection_deadline = selection_started_at + selection_timeout
    pages_scanned = 0
    details_fetched = 0
    featured_lookups = 0
    pending_scan_complete: bool | None = None

    def _remaining_timeout(stage: str) -> float:
        now = time.monotonic()
        remaining = selection_deadline - now
        if remaining <= 0:
            raise CardsSelectionTimeout(stage, pages_scanned, now - selection_started_at)
        return remaining

    per_page = per_page or config.max_posts_per_run
    candidate_reader: Callable[..., list[dict[str, Any]]] | None = None
    reader = getattr(client, "list_pending_card_candidates", None)
    if callable(reader):
        candidate_reader = cast(Callable[..., list[dict[str, Any]]], reader)
    lightweight_candidates = candidate_reader is not None

    def _list_candidates(
        *,
        page: int = 1,
        per_page: int,
        include: list[int] | None = None,
        timeout: float | None = None,
    ):
        if candidate_reader is not None:
            if isinstance(client, WordPressClient):
                return candidate_reader(
                    page=page, per_page=per_page, include=include, timeout=timeout
                )
            return candidate_reader(page=page, per_page=per_page, include=include)
        return client.list_pending(page=page, per_page=per_page, include=include)

    skipped = session_budget.skipped_locked_posts(root, config)
    if os.environ.get("HERMES_SESSION_ID"):
        skipped.update(session_budget.load_session(root, config)["posts_touched"])
    rotation = v1_rotation(root)
    epoch = load_epoch_protection(root)
    quarantined = quarantine_for_root(root)
    skipped.update(quarantined)
    rework_ids = [
        post_id for post_id in _rework_ids(root)
        if post_id not in skipped and not epoch.is_protected(post_id)
    ]
    # At most one include request, but give both artifact lanes representation
    # instead of filtering the first 100 IDs from one potentially large lane.
    included_lanes: dict[str, list[int]] = {"partial": [], "recovery": []}
    for post_id in rework_ids:
        lane = ("partial" if (root / "backups" / str(post_id) / "editorial.partial.json").is_file()
                else "recovery")
        included_lanes[lane].append(post_id)
    for lane, lane_ids in included_lanes.items():
        cursor = rotation.get(lane, {}).get("post_id", 0)
        lane_ids.sort(key=lambda post_id: (post_id <= cursor, post_id))
    partial_ids, recovery_ids = included_lanes["partial"], included_lanes["recovery"]
    rework_ids = (partial_ids[:50] + recovery_ids[:50] + partial_ids[50:] + recovery_ids[50:])[:100]
    posts: list[dict[str, Any]] = []
    if rework_ids:
        # Nem todo id com editorial.blocked.json é rework elegível: posts que
        # o humano devolveu ao status pending (ex.: awaiting_human) mantêm o
        # arquivo no filesystem, mas a meta _hermes_state manda. Resolve o
        # estado real de cada candidato e só conta os BLOCKED com cooldown
        # vencido — espelha o eligible_rework_ids do queue. (Sem isso, o
        # slice cego rework_ids[:per_page] saturava o lote com awaiting_human
        # e o trabalho real — rework elegível + posts novos — ficava invisível.)
        rework_started_at = time.monotonic()
        rework_timeout = _remaining_timeout("rework_candidates")
        try:
            candidates = _list_candidates(
                include=rework_ids[:100],
                per_page=min(len(rework_ids[:100]), 100),
                timeout=rework_timeout,
            )
        except WordPressError as exc:
            try:
                append_telemetry(
                    root,
                    "cards_selection_rest_failed",
                    stage="rework_candidates",
                    error_code=exc.error_code,
                    status_code=exc.status_code,
                )
            except Exception:  # noqa: BLE001 - metrics cannot mask provider error
                pass
            raise
        pages_scanned += 1
        try:
            append_telemetry(
                root,
                "cards_selection_rest",
                stage="rework_candidates",
                page=0,
                result_count=len(candidates),
                duration_ms=round((time.monotonic() - rework_started_at) * 1000, 1),
            )
        except Exception:  # noqa: BLE001 - metrics cannot block card selection
            pass
        for cand in candidates:
            post_id = cand.get("id")
            if not isinstance(post_id, int) or post_id in skipped:
                continue
            state_info = read_state(cand)
            eligibility = resolve_post_eligibility(cand, root)
            state = eligibility.state
            if eligibility.is_v2:
                continue
            if state in (STATE_BLOCKED, STATE_PARTIAL) and retry_eligible({**state_info, "state": state}):
                posts.append(cand)
    provisional = {lane: set() for lane in ("new", "partial", "recovery")}
    provisional_uncertain: set[int] = set()
    uncertain_quota = max(0, int(getattr(config, "uncertain_second_pass_limit", 5)))

    def available_rework() -> int:
        return (len(provisional["partial"]) + len(provisional["recovery"] - provisional_uncertain)
                + min(len(provisional_uncertain), uncertain_quota))

    def sample(rows):
        for post in rows:
            post_id = post.get("id")
            if type(post_id) is not int or post_id in skipped or epoch.is_protected(post_id):
                continue
            eligibility = resolve_post_eligibility(post, root)
            lane = v1_lane(eligibility.state)
            if (
                eligibility.eligible_for_v1 and not eligibility.is_v2 and lane
                and (eligibility.state != STATE_UNCERTAIN or uncertain_second_pass_eligible(post))
                # Only used to estimate depth, not to suppress a card.
                and not (
                    potential_v1_deferral(root, post) if lightweight_candidates
                    else v1_deferral(root, post, digest=_post_write_guard_signature)
                )
            ):
                provisional[lane].add(post_id)
                if eligibility.state == STATE_UNCERTAIN:
                    provisional_uncertain.add(post_id)

    sample(posts)
    remaining = max(1, per_page - len(posts))
    if per_page > 0:
        # Percorre a fila até encontrar candidatos elegíveis. Consultar só a
        # página 1 fazia o monitor acordar o agente para trabalho que cards
        # não conseguia enxergar quando os primeiros pending já eram READY.
        page_size = 100 if lightweight_candidates else max(remaining * 5, 20)
        pending_scan_complete = False
        for page in range(1, 101):
            if (
                page > 1 and len(provisional["new"]) + available_rework() >= per_page
                and time.monotonic() >= selection_started_at + selection_timeout * 0.6
            ):
                break  # keep the remaining budget for selected detail + featured reads
            page_timeout = _remaining_timeout("pending_candidates")
            page_started_at = time.monotonic()
            try:
                chunk = _list_candidates(
                    page=page, per_page=page_size, timeout=page_timeout
                )
            except WordPressError as exc:
                try:
                    append_telemetry(
                        root,
                        "cards_selection_rest_failed",
                        stage="pending_candidates",
                        page=page,
                        error_code=exc.error_code,
                        status_code=exc.status_code,
                    )
                except Exception:  # noqa: BLE001 - metrics cannot mask provider error
                    pass
                raise
            pages_scanned += 1
            page_elapsed = time.monotonic() - page_started_at
            try:
                append_telemetry(
                    root,
                    "cards_selection_rest",
                    stage="pending_candidates",
                    page=page,
                    result_count=len(chunk),
                    duration_ms=round(page_elapsed * 1000, 1),
                    elapsed_ms=round((time.monotonic() - selection_started_at) * 1000, 1),
                )
            except Exception:  # noqa: BLE001 - metrics cannot block card selection
                pass
            _remaining_timeout("pending_candidates")
            posts.extend(chunk)
            sample(chunk)
            if len(chunk) < page_size:
                pending_scan_complete = True
                break
            rework_available = available_rework()
            available = len(provisional["new"]) + rework_available
            fresh_goal = max(1, per_page - 2)
            rework_goal = min(2, max(1, per_page - 1))
            balanced = (
                len(provisional["new"]) >= fresh_goal
                and rework_available >= rework_goal
            )
            if (balanced and available >= per_page + 2) or (page >= 2 and available >= per_page):
                # Prefer two hydration alternatives when discovery is balanced.
                # Once fallback can fill the batch, two pages of look-ahead
                # suffice; don't sweep terminal backlog for a better mix.
                break
    candidate_filter_started_at = time.monotonic()
    posts = filter_quarantined_posts(root, posts)
    seen: set[int] = set()
    ordered: list[dict[str, Any]] = []
    for post in posts:
        post_id = post.get("id")
        if (
            not isinstance(post_id, int) or post_id in seen or post_id in skipped
            or epoch.is_protected(post_id)
        ):
            continue
        seen.add(post_id)
        eligibility = resolve_post_eligibility(post, root)
        if eligibility.is_v2 or not eligibility.eligible_for_v1:
            continue
        if not lightweight_candidates and v1_deferral(root, post, digest=_post_write_guard_signature):
            continue
        ordered.append(post)
    cards: list[dict[str, Any]] = []
    # Reserve deterministic capacity for UNCERTAIN second passes so NEW cannot
    # starve them, while keeping the independent quota bounded.
    def _state_for_card(post: dict[str, Any]) -> str | None:
        return resolve_post_eligibility(post, root).state

    eligible_uncertain = [
        post for post in ordered
        if _state_for_card(post) == STATE_UNCERTAIN
        and uncertain_second_pass_eligible(post)
    ]
    reserved_uncertain_ids = {
        int(post["id"])
        for post in sorted(eligible_uncertain, key=lambda p: int(p["id"]))[:uncertain_quota]
    }
    _remaining_timeout("uncertain_candidate_selection")
    lanes = {lane: [] for lane in ("new", "partial", "recovery")}
    for post in ordered:
        state = _state_for_card(post)
        lane = v1_lane(state)
        if lane and (state != STATE_UNCERTAIN or post["id"] in reserved_uncertain_ids):
            lanes[lane].append(post)
    for lane, rows in lanes.items():
        cursor = rotation.get(lane, {}).get("post_id", 0)
        def within(row):
            value = int(row["id"])
            info = read_state(row)
            featured_only = (
                lane == "partial" and _effective_partial_kind(info) in {"featured_missing", "featured_vision"}
                and info.get("partial_missing", 0) == 0 and not cursor
            )
            return (not featured_only, value <= cursor, value)
        rows.sort(key=within)
    rework = sorted(("partial", "recovery"), key=lambda lane: rotation.get(lane, {}).get("turn", 0))
    if per_page == 1:
        priority = sorted((*rework, "new"), key=lambda lane: rotation.get(lane, {}).get("turn", 0))
    else:
        first_rework = next((lane for lane in rework if lanes[lane]), rework[0])
        other = next(lane for lane in rework if lane != first_rework)
        priority = [first_rework, "new", other]
    candidate_lanes = {lane: iter(rows) for lane, rows in lanes.items()}
    exhausted: set[str] = set()

    def candidates_by_slot():
        # Fill a reserved slot from its own lane first, including after a
        # hydration miss. Transfer only when its sampled candidates run out.
        requested = priority[:per_page] + ["new"] * max(0, per_page - len(priority))
        while len(exhausted) < len(priority):
            for lane in requested:
                if lane in exhausted:
                    continue
                count_before = len(cards)
                while len(cards) == count_before:
                    row = next(candidate_lanes[lane], None)
                    if row is None:
                        exhausted.add(lane)
                        break
                    yield lane, row
            requested = priority
    _remaining_timeout("candidate_ordering")
    candidate_filter_elapsed_ms = round(
        (time.monotonic() - candidate_filter_started_at) * 1000, 1
    )
    uncertain_retry_mode = bool(reserved_uncertain_ids)

    def _card_gate(candidate: dict[str, Any], *, hydrated: bool):
        state_info = read_state(candidate)
        eligibility = resolve_post_eligibility(candidate, root)
        state = eligibility.state
        if eligibility.is_v2 or not eligibility.eligible_for_v1:
            return None
        if hydrated and v1_deferral(root, candidate, digest=_post_write_guard_signature):
            return None
        candidate_id = candidate.get("id")
        uncertain_retry_eligible = (
            state == STATE_UNCERTAIN
            and uncertain_retry_mode
            and candidate_id in reserved_uncertain_ids
            and uncertain_second_pass_eligible(candidate)
        )
        if state in (STATE_UNCERTAIN, STATE_AWAITING_HUMAN, STATE_SKIPPED, STATE_READY):
            if not uncertain_retry_eligible:
                return None
        blocked = state == STATE_BLOCKED
        if state in (STATE_BLOCKED, STATE_PARTIAL) and not retry_eligible(
            {**state_info, "state": state}
        ):
            return None
        return state_info, eligibility, state, uncertain_retry_eligible, blocked

    for lane, post in candidates_by_slot():
        _remaining_timeout("candidate_filter")
        post_id = post.get("id")
        if not isinstance(post_id, int):
            continue
        gate = _card_gate(post, hydrated=not lightweight_candidates)
        if gate is None:
            continue
        if lightweight_candidates:
            hydrator = getattr(client, "get_post", None)
            if not callable(hydrator):
                raise WorkflowError("WordPress client cannot hydrate selected card candidates")
            detail_timeout = _remaining_timeout("selected_post_details")
            detail_started_at = time.monotonic()
            try:
                post = (
                    hydrator(post_id, timeout=detail_timeout)
                    if isinstance(client, WordPressClient)
                    else hydrator(post_id)
                )
            except WordPressError as exc:
                try:
                    append_telemetry(
                        root,
                        "cards_selection_rest_failed",
                        stage="selected_post_details",
                        post_id=post_id,
                        error_code=exc.error_code,
                        status_code=exc.status_code,
                    )
                except Exception:  # noqa: BLE001 - metrics cannot mask provider error
                    pass
                if known_wordpress_entity_missing(exc):
                    continue
                raise
            details_fetched += 1
            try:
                append_telemetry(
                    root,
                    "cards_selection_detail",
                    post_id=post_id,
                    duration_ms=round((time.monotonic() - detail_started_at) * 1000, 1),
                    outcome="fetched",
                )
            except Exception:  # noqa: BLE001 - metrics cannot block card selection
                pass
            _remaining_timeout("selected_post_details")
            if not isinstance(post, dict) or post.get("id") != post_id:
                raise WorkflowError(f"WordPress returned invalid details for selected post {post_id}")
            if post.get("status") != "pending":
                continue
            gate = _card_gate(post, hydrated=True)
            if gate is None:
                continue
        state_info, eligibility, state, uncertain_retry_eligible, blocked = gate
        if v1_lane(state) != lane:
            continue  # A changed item must not consume the wrong slot.
        backups_dir = root / "backups" / str(post_id)
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
        featured_timeout = _remaining_timeout("featured_media") if isinstance(client, WordPressClient) else None
        featured_id = post.get("featured_media")
        if isinstance(featured_id, int) and featured_id > 0:
            featured_lookups += 1
        featured_started_at = time.monotonic()
        try:
            wordpress_featured = _featured_diagnosis(
                client, post, entities, timeout=featured_timeout, strict_lookup=True
            )
        except WordPressError as exc:
            try:
                append_telemetry(
                    root,
                    "cards_selection_rest_failed",
                    stage="featured_media",
                    post_id=post_id,
                    error_code=exc.error_code,
                    status_code=exc.status_code,
                )
            except Exception:  # noqa: BLE001 - metrics cannot mask provider error
                pass
            raise
        try:
            append_telemetry(
                root,
                "cards_selection_featured",
                post_id=post_id,
                duration_ms=round((time.monotonic() - featured_started_at) * 1000, 1),
                lookup=bool(isinstance(featured_id, int) and featured_id > 0),
                outcome=wordpress_featured.get("diagnostic_error_code") or "ok",
            )
        except Exception:  # noqa: BLE001 - metrics cannot block card selection
            pass
        _remaining_timeout("featured_media")
        partial_media_drift = None
        partial_manifest = _load_partial_manifest(root, post_id) if state == STATE_PARTIAL else {}
        featured = (
            _working_featured_diagnosis(
                client,
                post,
                entities,
                partial_manifest,
                timeout=featured_timeout,
                wordpress_fallback=wordpress_featured,
            )
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
        if len(cards) >= per_page:
            break
    selected = cards[:per_page]
    _remaining_timeout("result_assembly")
    elapsed_ms = round((time.monotonic() - selection_started_at) * 1000, 1)
    try:
        append_telemetry(
            root,
            "cards_selection_complete",
            elapsed_ms=elapsed_ms,
            candidate_filter_elapsed_ms=candidate_filter_elapsed_ms,
            pages_scanned=pages_scanned,
            candidates=len(ordered),
            selected=len(selected),
            details_fetched=details_fetched,
            featured_lookups=featured_lookups,
            pending_scan_complete=pending_scan_complete,
        )
    except Exception:  # noqa: BLE001 - metrics cannot block card output
        pass
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
        "cards_selection_elapsed_ms": elapsed_ms,
        "cards_selection_filter_elapsed_ms": candidate_filter_elapsed_ms,
        "cards_selection_pages_scanned": pages_scanned,
        "cards_selection_candidates": len(ordered),
        "cards_selection_details_fetched": details_fetched,
        "cards_selection_featured_lookups": featured_lookups,
        "pending_scan_complete": pending_scan_complete,
        "has_more_pending": False if pending_scan_complete is True else (
            True if pending_scan_complete is False else None
        ),
    }


def _featured_diagnosis(
    client: WordPressClient,
    post: dict[str, Any],
    entities: set[str],
    *,
    timeout: float | None = None,
    strict_lookup: bool = False,
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
    diagnostic_error_code = ""
    try:
        media = (
            client.get_media(featured_id, timeout=timeout)
            if isinstance(client, WordPressClient) and timeout is not None
            else client.get_media(featured_id)
        )
        if strict_lookup and (not isinstance(media, dict) or ("id" in media and media.get("id") != featured_id)):
            raise WorkflowError(f"WordPress returned invalid attachment details for {featured_id}")
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
    except WordPressError as exc:
        if strict_lookup:
            if not known_wordpress_entity_missing(exc):
                raise
            return {
                "exists": False, "relevant": None, "webp": None, "dimensions": None,
                "valid": False, "action": "provide", "diagnostic_error_code": "media_not_found",
            }
        if exc.error_code == "wordpress_timeout":
            raise
        diagnostic_error_code = exc.error_code
    except Exception:  # noqa: BLE001 - conservador, sem esconder motivo do diagnóstico
        if strict_lookup:
            raise
        diagnostic_error_code = "media_lookup_failed"
    valid = bool(relevant and webp and dimensions == "1280x720")
    if valid:
        action = "ok"
    elif relevant is False:
        action = "replace"
    else:
        action = "normalize"
    result = {
        "exists": True,
        "relevant": relevant,
        "webp": webp,
        "dimensions": dimensions,
        "valid": valid,
        "action": action,
    }
    if diagnostic_error_code:
        result["diagnostic_error_code"] = diagnostic_error_code
    return result


def _working_featured_diagnosis(
    client: WordPressClient,
    post: dict[str, Any],
    entities: set[str],
    manifest: dict[str, Any],
    *,
    timeout: float | None = None,
    wordpress_fallback: dict[str, Any] | None = None,
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
    if status == "verification_pending":
        return {
            "exists": True, "relevant": None, "webp": None, "dimensions": None,
            "valid": False, "action": "recheck", "media_id": featured.get("media_id"),
            "reason": featured.get("reason"), "source": "partial_manifest",
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
    wordpress = wordpress_fallback or _featured_diagnosis(
        client, post, entities, timeout=timeout
    )
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
    """Reopen a post while sharing the per-post writer lock with the pipeline."""
    with _acquire_post_lock(root, config, post_id) as lock_handle:
        guarded_client = _guard_post_client(client, lock_handle, root=root, post_id=post_id)
        return _retry_post_unlocked(guarded_client, config, root, post_id)


def _retry_post_unlocked(
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
    assert_post_not_quarantined(root, post_id, operation="retry")
    post = client.get_post(post_id)
    if post.get("status") not in ("pending", "awaiting_human"):
        raise WorkflowError(f"post {post_id} nao esta pending/awaiting_human ({post.get('status')})")
    try:
        eligibility = assert_v1_mutation_allowed(post, root, operation="retry")
    except RuntimeError as exc:
        raise WorkflowError(str(exc)) from exc
    if config.dry_run:
        raise WorkflowError("retry e uma operacao de escrita: exige write mode (EDITOR_DRY_RUN=false)")
    if post.get("status") == "awaiting_human":
        client.move_to_status(post_id, "pending")
        moved = client.get_post(post_id)
        if str(moved.get("status") or "") != "pending":
            raise WorkflowError(f"retry status read-back failed for post {post_id}")

    if not _write_state_markers(
        client,
        config,
        post_id,
        STATE_BLOCKED,
        root=root,
        attempts=0,
        last_error="reaberto por revisao humana (retry)",
    ):
        raise WorkflowError(f"estado legado BLOCKED nao persistiu no retry do post {post_id}")
    final = client.get_post(post_id)
    final_meta = final.get("meta") if isinstance(final.get("meta"), dict) else {}
    if final_meta.get("_hermes_state") != STATE_BLOCKED or str(final_meta.get("_hermes_attempts") or "") != "0":
        raise WorkflowError(f"retry legacy read-back failed for post {post_id}")
    return {
        "post_id": post_id,
        "status": "retried",
        "state": STATE_BLOCKED,
        "attempts": 0,
        "wordpress_changed": True,
        "readback": True,
    }


def discard_post(
    client: WordPressClient,
    config: Config,
    root: Path,
    post_id: int,
    reason: str = "",
) -> dict[str, Any]:
    """Discard a post under the same cross-process lock as pipeline writes."""
    with _acquire_post_lock(root, config, post_id) as lock_handle:
        guarded_client = _guard_post_client(client, lock_handle, root=root, post_id=post_id)
        return _discard_post_unlocked(guarded_client, config, root, post_id, reason=reason)


def _discard_post_unlocked(
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
    assert_post_not_quarantined(root, post_id, operation="discard")
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
    skipped_v2 = None
    had_v2_state = "_hermes_work_state" in meta
    if had_v2_state:
        try:
            from dataclasses import replace as _replace
            from .pipeline_v2.model import LifecycleState, WorkState

            raw_v2 = meta.get("_hermes_work_state")
            raw_v2 = json.loads(raw_v2) if isinstance(raw_v2, str) else raw_v2
            if not isinstance(raw_v2, dict):
                raise ValueError("V2 work state is not an object")
            current_v2 = WorkState.from_dict(raw_v2)
            skipped_v2 = _replace(current_v2, state=LifecycleState.SKIPPED)
            serialized_v2 = json.dumps(
                skipped_v2.to_dict(), ensure_ascii=False, separators=(",", ":")
            )
            client.update_post(post_id, {"meta": {"_hermes_work_state": serialized_v2}})
            v2_readback = client.get_post(post_id)
            v2_meta = v2_readback.get("meta") if isinstance(v2_readback.get("meta"), dict) else {}
            read_v2 = v2_meta.get("_hermes_work_state")
            read_v2 = json.loads(read_v2) if isinstance(read_v2, str) else read_v2
            if not isinstance(read_v2, dict) or WorkState.from_dict(read_v2) != skipped_v2:
                raise WorkflowError(f"V2 SKIPPED read-back failed for post {post_id}")
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise WorkflowError(f"estado V2 invalido para discard {post_id}: {exc}") from exc

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
    state_readback = client.get_post(post_id)
    state_meta = state_readback.get("meta") if isinstance(state_readback.get("meta"), dict) else {}
    if state_meta.get("_hermes_state") != STATE_SKIPPED:
        raise WorkflowError(f"legacy SKIPPED read-back failed for post {post_id}")
    if had_v2_state:
        from .pipeline_v2.model import WorkState
        raw_v2 = state_meta.get("_hermes_work_state")
        raw_v2 = json.loads(raw_v2) if isinstance(raw_v2, str) else raw_v2
        if not isinstance(raw_v2, dict) or WorkState.from_dict(raw_v2) != skipped_v2:
            raise WorkflowError(f"V2 SKIPPED projection read-back failed for post {post_id}")
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
    *,
    on_work_started: Callable[[], None] | None = None,
    lock_handle: Any | None = None,
) -> dict[str, Any]:
    assert_post_not_quarantined(root, post_id, operation="mark_uncertain")
    if lock_handle is not None:
        guarded_client = _guard_post_client(client, lock_handle, root=root, post_id=post_id)
        return _mark_uncertain_unlocked(
            guarded_client,
            config,
            root,
            post_id,
            reason,
            on_work_started=on_work_started,
        )
    with _acquire_post_lock(root, config, post_id) as lock_handle:
        guarded_client = _guard_post_client(client, lock_handle, root=root, post_id=post_id)
        return _mark_uncertain_unlocked(
            guarded_client,
            config,
            root,
            post_id,
            reason,
            on_work_started=on_work_started,
        )


def _mark_uncertain_unlocked(
    client: WordPressClient,
    config: Config,
    root: Path,
    post_id: int,
    reason: str,
    *,
    on_work_started: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Registra a decisão do agente de não processar o post agora.

    O agente usava ``uncertain.json`` direto no filesystem; este comando
    valida e persiste também o estado no WordPress (fonte de verdade única).
    """
    assert_post_not_quarantined(root, post_id, operation="mark_uncertain")
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
    if on_work_started is not None:
        on_work_started()
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
