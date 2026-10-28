"""Explicit, idempotent recovery of one already-uploaded V2 attachment."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ..backup import atomic_write_text
from ..media.downloader import download_image
from ..media.library_index import register as register_library_media
from ..media.relevance import extract_entities
from ..media.registry import record_v2_observation
from ..media.source_verify import validate_discovered_candidate, verify_downloaded_against_source
from ..media.visual_identity import fingerprint_path, verify_candidate_identity
from ..observability import append_telemetry
from ..workflow import validate_media_plan
from .media_accounting import account_visual_media
from .epoch_protection import load_epoch_protection
from .model import BlockerCode, FeaturedStatus, InlineMedia, LifecycleState, MediaProgress, Phase, WorkState


def _read_state(post: dict[str, Any]) -> WorkState:
    meta = post.get("meta")
    meta = meta if isinstance(meta, dict) else {}
    raw = meta.get("_hermes_work_state")
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise ValueError("post has no canonical V2 state")
    return WorkState.from_dict(raw)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid recovery evidence file: {path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError("recovery evidence must be a JSON object")
    return value


def _save_journal(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _wordpress_snapshot(post: dict[str, Any], state: WorkState) -> dict[str, Any]:
    meta = post.get("meta") if isinstance(post.get("meta"), dict) else {}
    return {
        "status": post.get("status"),
        "state": hashlib.sha256(
            json.dumps(state.to_dict(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "featured_media": post.get("featured_media"),
        "title": deepcopy(post.get("title")),
        "content": deepcopy(post.get("content")),
        "reopened_at": meta.get("_hermes_human_reopened_at"),
        "reopened_from": meta.get("_hermes_human_reopened_from"),
    }


def _safe_persisted_url(value: str) -> str:
    """Keep public URL identity while dropping credentials, queries and fragments."""
    parsed = urlsplit(str(value or ""))
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = f"{hostname}:{port}" if port else hostname
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _adoption_subject_matches_article(
    subject: str, *, title: str, body: str, editorial: dict[str, Any]
) -> bool:
    seo = editorial.get("seo") if isinstance(editorial.get("seo"), dict) else {}
    article_entities = extract_entities(
        title=title,
        content_html=body,
        focus_keyword=str(seo.get("focus_keyword") or ""),
        game_name=str(editorial.get("game_name") or ""),
    )
    subject_entities = extract_entities(title="", game_name=subject)
    return bool(article_entities & subject_entities)


def _verify_historical_linkage(root: Path, post_id: int, evidence: dict[str, Any]) -> None:
    """Bind a recovery bundle to the candidate, writer journal and registry decision."""
    run_id = str(evidence.get("run_id") or "").strip()
    candidate_id = str(evidence.get("candidate_id") or "").strip()
    directory = root / "backups" / str(post_id)
    candidate = _read_json(directory / "editorial.candidate.json")
    journal = _read_json(root / "work" / "v2-journal" / f"{post_id}.json")
    candidate_content = str(candidate.get("content") or "")
    candidate_hash = hashlib.sha256(candidate_content.encode()).hexdigest()
    journal_run = str(journal.get("run_id") or "").strip()
    candidate_run = str(candidate.get("_v2_run_id") or "").strip()
    if (
        not run_id
        or candidate_run != run_id
        or journal_run != run_id
        or str(journal.get("candidate_run_id") or "").strip() != run_id
        or str(journal.get("candidate_hash") or "").strip() != candidate_hash
    ):
        raise ValueError("recovery evidence does not match the historical V2 candidate and journal")
    registry_path = root / "work" / "media_registry.sqlite"
    if not registry_path.is_file():
        raise ValueError("historical media registry is unavailable")
    try:
        db = sqlite3.connect(f"{registry_path.resolve().as_uri()}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """SELECT role, subject, disposition, evidence_reference
                 FROM media_decisions
                WHERE post_id=? AND run_id=? AND candidate_id=? AND scope='candidate'""",
            (post_id, run_id, candidate_id),
        ).fetchall()
    except sqlite3.Error as exc:
        raise ValueError("historical candidate decision is unavailable in MediaRegistry") from exc
    finally:
        if "db" in locals():
            db.close()
    expected_domain = (urlsplit(str(evidence.get("source_page_url") or "")).netloc or "").casefold()
    direct_url_hash = hashlib.sha256(str(evidence.get("direct_image_url") or "").strip().encode()).hexdigest()
    source_url_hash = hashlib.sha256(str(evidence.get("source_page_url") or "").strip().encode()).hexdigest()
    expected_verdict = str((evidence.get("evidence") or {}).get("verdict") or "")
    linked = False
    for row in rows:
        if (
            row["role"] != "inline"
            or row["subject"] != str(evidence.get("subject") or "")
            or row["disposition"] != "accepted"
        ):
            continue
        try:
            reference = json.loads(row["evidence_reference"] or "{}")
        except (TypeError, ValueError):
            continue
        if (
            isinstance(reference, dict)
            and reference.get("verdict") == expected_verdict == "deterministic_match"
            and str(reference.get("source_domain") or "").casefold() == expected_domain
            and reference.get("direct_image_url_sha256") == direct_url_hash
            and reference.get("source_page_url_sha256") == source_url_hash
        ):
            linked = True
            break
    if not linked:
        raise ValueError("candidate_id has no matching accepted V2 decision in MediaRegistry")


def recover_historical_attachment(
    client: Any,
    config: Any,
    root: str | Path,
    *,
    post_id: int,
    media_id: int,
    evidence_path: str | Path,
    apply: bool = False,
) -> dict[str, Any]:
    """Reconcile one existing WordPress attachment; preview is the default.

    Apply updates only V2 state meta and local evidence files. It never changes
    status, content, featured media, counters, cooldown, or publication state.
    """
    root = Path(root)
    if post_id < 1 or media_id < 1:
        raise ValueError("post_id and media_id must be positive")
    if apply and bool(getattr(config, "dry_run", True)):
        raise ValueError("recovery apply requires EDITOR_DRY_RUN=false")
    if apply:
        load_epoch_protection(root).assert_writable(post_id, effect="media_recovery")
        from .quarantine import assert_post_not_quarantined

        assert_post_not_quarantined(root, post_id, operation="media_recovery")
        with _V2RecoveryLock(root / "work" / "v2-run.lock"):
            return _recover_locked(
                client, config, root, post_id=post_id, media_id=media_id,
                evidence_path=Path(evidence_path), apply=True,
            )
    else:
        return _recover_locked(
            client, config, root, post_id=post_id, media_id=media_id,
            evidence_path=Path(evidence_path), apply=False,
        )


def adopt_existing_attachment(
    client: Any,
    config: Any,
    root: str | Path,
    *,
    post_id: int,
    media_id: int,
    evidence_path: str | Path,
    authorization_id: str,
    authorized_by: str,
    authorization_reason: str,
    confirm_adoption: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    """Run a new editorial adoption, explicitly distinct from historic recovery."""
    if not confirm_adoption:
        raise ValueError("editorial adoption requires --confirm-adoption")
    authorization = {
        "authorization_id": str(authorization_id or "").strip(),
        "authorized_by": str(authorized_by or "").strip(),
        "reason": str(authorization_reason or "").strip(),
    }
    if not all(authorization.values()):
        raise ValueError("adoption requires authorization_id, authorized_by and reason")
    if apply:
        raise ValueError(
            "editorial adoption apply is disabled until authorization can be independently verified"
        )
    root = Path(root)
    if post_id < 1 or media_id < 1:
        raise ValueError("post_id and media_id must be positive")
    return _recover_locked(
        client, config, root, post_id=post_id, media_id=media_id,
        evidence_path=Path(evidence_path), apply=False,
        adoption_authorization=authorization,
    )


class _V2RecoveryLock:
    """Use the same exclusive session lock as v2-run while reconciling state."""

    def __init__(self, path: Path):
        from .lock import RunSessionLock

        self._lock = RunSessionLock(path)

    def __enter__(self):
        if not self._lock.acquire():
            raise RuntimeError("V2 run is active; media recovery was not started")
        return self

    def __exit__(self, *_exc: object) -> None:
        self._lock.release()


def _recover_locked(
    client: Any,
    config: Any,
    root: Path,
    *,
    post_id: int,
    media_id: int,
    evidence_path: Path,
    apply: bool,
    adoption_authorization: dict[str, str] | None = None,
) -> dict[str, Any]:
    evidence = _read_json(evidence_path)
    is_adoption = adoption_authorization is not None
    if evidence.get("operation") == "editorial_adoption" and not is_adoption:
        raise ValueError("adoption evidence must use the separate v2-adopt-media command")
    if (
        evidence.get("version") != 1
        or int(evidence.get("post_id") or 0) != post_id
        or int(evidence.get("media_id") or 0) != media_id
    ):
        raise ValueError("evidence version/post_id/media_id do not match requested recovery")
    original_run_id = str(evidence.get("run_id") or "").strip()
    candidate_id = str(evidence.get("candidate_id") or "").strip()
    operation_id = (
        adoption_authorization["authorization_id"] if is_adoption else original_run_id
    )
    if is_adoption:
        candidate_id = f"editorial-adoption:{operation_id}"
    source_page = str(evidence.get("source_page_url") or "").strip()
    direct_url = str(evidence.get("direct_image_url") or "").strip()
    subject = str(evidence.get("subject") or "").strip()
    credit = str(evidence.get("credit_text") or "").strip()
    relevance = evidence.get("evidence")
    historical_identity = evidence.get("visual_verification")
    if (
        (not operation_id or (not is_adoption and not candidate_id))
        or not subject or not source_page or not direct_url
        or not isinstance(relevance, dict)
        or relevance.get("verdict") != "deterministic_match"
        or not relevance.get("matched")
        or (not is_adoption and (
            not isinstance(historical_identity, dict)
            or historical_identity.get("decision") != "DIFFERENT"
        ))
    ):
        label = "adoption" if is_adoption else "historical recovery"
        raise ValueError(f"evidence lacks required {label} provenance or relevance")
    if is_adoption:
        evidence["operation"] = "editorial_adoption"
        evidence["authorization"] = dict(adoption_authorization)
    else:
        for field in ("sha256", "phash", "visual_group_id"):
            if not str(evidence.get(field) or "").strip():
                raise ValueError(f"evidence is missing {field}")
        _verify_historical_linkage(root, post_id, evidence)

    post = client.get_post(post_id)
    if not isinstance(post, dict) or int(post.get("id") or 0) != post_id:
        raise ValueError("WordPress post readback did not match post_id")
    if post.get("status") not in {"pending", "awaiting_human"}:
        raise ValueError("post status is outside the human-review lifecycle")
    previous = _read_state(post)
    if previous.state is not LifecycleState.HUMAN_REQUIRED:
        operation_name = "editorial adoption" if is_adoption else "historical recovery"
        raise ValueError(f"{operation_name} only accepts a HUMAN_REQUIRED post")
    initial_snapshot = _wordpress_snapshot(post, previous)

    attachment = client.get_media(media_id)
    attachment_url = str((attachment or {}).get("source_url") or "").strip()
    if not attachment_url:
        raise ValueError("WordPress attachment has no source_url")
    details = (attachment or {}).get("media_details")
    details = details if isinstance(details, dict) else {}
    width, height = int(details.get("width") or 0), int(details.get("height") or 0)
    from ..media.converter import MAX_INLINE_WIDTH, MIN_INLINE_WIDTH
    if (attachment or {}).get("mime_type") != "image/webp":
        raise ValueError("historical attachment is not WebP")
    if not MIN_INLINE_WIDTH <= width <= MAX_INLINE_WIDTH or height < 1:
        raise ValueError("historical attachment dimensions fail the inline-media gate")
    directory = root / "backups" / str(post_id)
    draft_path = directory / "editorial.draft.json"
    if not draft_path.is_file():
        draft_path = directory / "editorial.latest.json"
    editorial = _read_json(draft_path)
    post_title = post.get("title")
    title = str(post_title.get("raw") or post_title.get("rendered") or "") if isinstance(post_title, dict) else str(post_title or "")
    post_content = post.get("content")
    post_body = post_content.get("raw") if isinstance(post_content, dict) else post_content
    body = str(editorial.get("cleaned_html") or post_body or "")
    if not title or not body:
        raise ValueError("current WordPress title and persisted editorial body are required")
    if is_adoption and not _adoption_subject_matches_article(
        subject, title=title, body=body, editorial=editorial
    ):
        raise ValueError("adoption subject is not grounded in the current article title, SEO or content")

    existing_item = next((item for item in previous.media.inline if item.media_id == media_id), None)
    already_recovered = existing_item is not None
    if media_id == previous.media.featured.media_id:
        raise ValueError("attachment is already the featured image, not an inline recovery")
    if already_recovered and (
        (not is_adoption and existing_item.sha256 != evidence.get("sha256"))
        or existing_item.media_url != attachment_url
    ):
        raise ValueError("attachment id is already present with conflicting state")
    if previous.media.required < 1:
        raise ValueError("V2 state has no inline quota to recover")
    current_accounting = account_visual_media(previous.media)
    if not already_recovered and current_accounting.effective_missing < 1:
        raise ValueError("no inline deficit remains; recovery would exceed the current quota")

    journal_id = hashlib.sha256(
        f"v1:{post_id}:{media_id}:{operation_id}:{evidence.get('sha256') or ''}".encode()
    ).hexdigest()
    journal_path = root / "work" / "v2-media-recovery" / f"{journal_id}.json"
    old_state_hash = hashlib.sha256(json.dumps(previous.to_dict(), sort_keys=True).encode()).hexdigest()

    with tempfile.TemporaryDirectory(prefix="unicornio-media-recovery-") as temp_dir:
        temp_root = Path(temp_dir)
        attachment_path = download_image(
            attachment_url, temp_root / f"attachment-{media_id}.img",
            url_policy=str(getattr(config, "remote_url_policy", "audit")),
            timeout=float(getattr(config, "http_timeout", 30)),
        )
        identity = fingerprint_path(attachment_path)
        if not is_adoption and (
            identity.sha256 != str(evidence["sha256"])
            or identity.phash != str(evidence["phash"])
            or identity.visual_group_id != str(evidence["visual_group_id"])
        ):
            raise ValueError("attachment bytes do not match historical SHA-256/pHash evidence")
        if already_recovered and (
            existing_item.sha256 != identity.sha256 or existing_item.media_url != attachment_url
        ):
            raise ValueError("attachment id is already present with conflicting state")
        source_check = validate_discovered_candidate(
            {"direct_image_url": direct_url, "source_page_url": source_page},
            subject=subject,
            url_policy=str(getattr(config, "remote_url_policy", "audit")),
        )
        if not source_check.get("valid") or not source_check.get("source_verified"):
            raise ValueError("source page no longer verifies the historical image URL")
        byte_match, byte_reason = verify_downloaded_against_source(
            source_page_url=source_page, downloaded=attachment_path,
            direct_image_url=direct_url,
            url_policy=str(getattr(config, "remote_url_policy", "audit")),
        )
        if not byte_match:
            raise ValueError("attachment bytes do not match the verified source image")

        baseline: list[dict[str, Any]] = []
        attachments_by_id: dict[int, dict[str, Any]] = {}
        for item in previous.media.inline:
            attachments_by_id[item.media_id] = item.to_dict()
        featured = previous.media.featured
        if featured.status is FeaturedStatus.VALID and featured.media_id:
            attachments_by_id[int(featured.media_id)] = featured.to_dict()
        for existing_id, old in attachments_by_id.items():
            if existing_id == media_id:
                continue
            current_attachment = client.get_media(existing_id)
            url = str((current_attachment or {}).get("source_url") or "").strip()
            if not url:
                raise ValueError(f"baseline attachment {existing_id} is unavailable")
            path = download_image(
                url, temp_root / f"baseline-{existing_id}.img",
                url_policy=str(getattr(config, "remote_url_policy", "audit")),
                timeout=float(getattr(config, "http_timeout", 30)),
            )
            actual = fingerprint_path(path)
            if old.get("sha256") and actual.sha256 != old["sha256"]:
                raise ValueError(f"baseline attachment {existing_id} SHA-256 changed")
            baseline.append({**actual.to_dict(), "media_id": existing_id, "local_path": str(path)})

        fresh_identity, decision = verify_candidate_identity(
            attachment_path, candidate_id=candidate_id, baseline=baseline,
            config=config, root=temp_root if not apply else root, comparison_budget=[0],
        )
        if not decision.verified or decision.decision != "DIFFERENT":
            raise ValueError("attachment is not verified distinct from the post's current media")
        confidence = max(
            (float(row.get("confidence") or 0) for row in decision.comparisons), default=1.0,
        )
        if confidence < float(getattr(config, "visual_comparison_min_confidence", 0.85)):
            raise ValueError("visual comparison confidence is below the configured threshold")

    occupied = {item.slot for item in previous.media.inline}
    slot = existing_item.slot if existing_item else 0
    while not existing_item and slot in occupied:
        slot += 3
    item = existing_item or InlineMedia(
        media_id=media_id, media_url=attachment_url, slot=slot,
        alt_text=str(evidence.get("alt_text") or ""), credit_text=credit, subject=subject,
        item_number=int(evidence["item_number"]) if evidence.get("item_number") is not None else None,
        section_heading=str(evidence.get("section_heading") or ""),
        section_slot=int(evidence["section_slot"]) if evidence.get("section_slot") is not None else None,
        width=width, height=height,
        phash=fresh_identity.phash, sha256=fresh_identity.sha256,
        visual_group_id=fresh_identity.visual_group_id,
        visual_verification={
            "decision": "DIFFERENT", "reason": decision.reason,
            "comparisons": list(decision.comparisons), "baseline_media_ids": sorted(attachments_by_id),
            "recovery": {
                "operation": "editorial_adoption" if is_adoption else "historical_recovery",
                **({"authorization": dict(adoption_authorization)} if is_adoption else {
                    "source_run_id": original_run_id, "candidate_id": candidate_id,
                }),
                "source_page_url": _safe_persisted_url(source_page),
                "direct_image_url": _safe_persisted_url(direct_url),
                "source_page_url_sha256": hashlib.sha256(source_page.encode()).hexdigest(),
                "direct_image_url_sha256": hashlib.sha256(direct_url.encode()).hexdigest(),
            },
        },
    )
    candidate_media = (
        previous.media if already_recovered
        else replace(previous.media, inline=(*previous.media.inline, item), accepted_count=None)
    )
    final_accounting = account_visual_media(candidate_media)
    expected_count = current_accounting.valid_distinct_inline_count + (0 if already_recovered else 1)
    if final_accounting.valid_distinct_inline_count != expected_count:
        raise ValueError("recovered attachment did not add exactly one distinct inline image")

    plan_item = {
        "candidate_id": candidate_id, "direct_image_url": direct_url,
        "source_page_url": source_page, "subject": subject,
        "paragraph_index": slot, "alt_text": item.alt_text, "credit_text": credit,
        "evidence": relevance,
    }
    with tempfile.TemporaryDirectory(
        prefix="unicornio-media-recovery-preflight-" if not apply else None
    ) as preflight_temp:
        preflight = validate_media_plan(
            client, {**editorial, "cleaned_html": body, "media_plan": [plan_item]},
            config=config, root=Path(preflight_temp) if not apply else root, post_title=title,
            existing_featured_id=featured.media_id if featured.status is FeaturedStatus.VALID else None,
            post_id=post_id,
        )
    if preflight.get("rejected") or int(preflight.get("valid") or 0) != 1:
        raise ValueError("recovered media failed the current V2 provenance/relevance preflight")

    proposed = replace(previous, media=candidate_media)
    report = {
        "mode": "apply" if apply else "preview", "post_id": post_id, "media_id": media_id,
        "operation": "editorial_adoption" if is_adoption else "historical_recovery",
        "source_run_id": original_run_id or None, "recovery_id": journal_id,
        **({"authorization": dict(adoption_authorization)} if is_adoption else {}),
        "source_verification": source_check.get("verification_level"),
        "source_bytes": byte_reason, "visual_decision": decision.decision,
        "visual_confidence": confidence,
        "slot": slot, "required": final_accounting.required_inline,
        "valid_distinct_inline": final_accounting.valid_distinct_inline_count,
        "missing": final_accounting.effective_missing,
        "status_preserved": post.get("status"), "lifecycle_preserved": previous.state.value,
        "phase_preserved": previous.phase.value, "attempts_preserved": previous.retry.attempts,
        "no_progress_preserved": previous.retry.no_progress,
        "apply_requested": apply,
    }
    if not apply:
        return report | {"writes": 0, "already_recovered": already_recovered,
                         "proposed_state": proposed.to_dict()}

    prior_journal = _read_json(journal_path) if journal_path.is_file() else {}
    if already_recovered and prior_journal.get("status") == "complete":
        if prior_journal.get("proposed_state") != proposed.to_dict():
            raise ValueError("completed recovery journal conflicts with current V2 state")
        return report | {"writes": 0, "already_recovered": True,
                         "registry_status": "accepted", "media_application": "not_attempted"}
    journal = prior_journal or {
        **report, "schema_version": 1, "old_state_sha256": old_state_hash,
        "proposed_state": proposed.to_dict(),
        "checkpoint": f"backups/{post_id}/editorial.partial.json",
        "registry_status": "accepted", "media_application": "not_attempted",
    }
    journal.update({**report, "status": "prepared"})
    _save_journal(journal_path, journal)
    if already_recovered:
        readback = post
    else:
        latest_post = client.get_post(post_id)
        if not isinstance(latest_post, dict) or int(latest_post.get("id") or 0) != post_id:
            raise RuntimeError("WordPress concurrency readback did not match post_id")
        latest_state = _read_state(latest_post)
        if _wordpress_snapshot(latest_post, latest_state) != initial_snapshot:
            raise RuntimeError("WordPress post changed during media recovery; no write was attempted")
        serialized_state = json.dumps(proposed.to_dict(), ensure_ascii=False, separators=(",", ":"))
        client.update_post(post_id, {"meta": {"_hermes_work_state": serialized_state}})
        readback = client.get_post(post_id)
    readback_state = _read_state(readback)
    if readback_state.to_dict() != proposed.to_dict() or readback.get("status") != post.get("status"):
        raise RuntimeError("WordPress state/status readback failed during media recovery")
    journal["status"] = "wordpress_confirmed"
    _save_journal(journal_path, journal)

    checkpoint = root / "backups" / str(post_id) / "editorial.partial.json"
    atomic_write_text(checkpoint, json.dumps(candidate_media.to_dict(), ensure_ascii=False, sort_keys=True, indent=2))
    register_library_media(
        root, phash=item.phash, source_url=_safe_persisted_url(direct_url),
        source_page=_safe_persisted_url(source_page),
        subject=subject, media_id=media_id, article_id=post_id,
        sha256=item.sha256, visual_group_id=item.visual_group_id,
    )
    record_v2_observation(
        root, post_id=post_id,
        run_id=f"{'editorial-adoption' if is_adoption else 'historical-recovery'}:{journal_id}",
        state=proposed, phase="media_recovery", outcome="accepted",
        checkpoint_reference=f"work/v2-media-recovery/{journal_id}.json", applied=False,
    )
    journal["status"] = "complete"
    _save_journal(journal_path, journal)
    try:
        append_telemetry(
            root, "v2_media_adoption_completed" if is_adoption else "v2_media_recovery_completed",
            post_id=post_id, media_id=media_id,
            run_id=journal_id,
            **({"authorization_id": operation_id, "authorized_by": adoption_authorization["authorized_by"]}
               if is_adoption else {"source_run_id": original_run_id}),
            lifecycle=previous.state.value, applied=False,
        )
    except Exception:
        pass
    return report | {"writes": 1, "registry_status": "accepted", "media_application": "not_attempted"}
