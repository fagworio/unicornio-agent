"""Explicit, idempotent migrations for persisted V2 operational state."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .classifier import EDITORIAL_BLOCKERS, MEDIA_BLOCKERS
from ..editorial_schema import EditorialValidationError, validate_editorial
from .model import BlockerCode, CURRENT_RETRY_POLICY_VERSION, LifecycleState, Phase, RetryInfo, WorkState


HISTORICAL_MEDIA_HUMAN_REQUIRED_IDS = frozenset({
    114835, 114847, 114851, 114855, 114885,
    114937, 115025, 115027, 115035,
})
HISTORICAL_MEDIA_LOSS_IDS = frozenset({114949, 114984, 114987})
HISTORICAL_MEDIA_DUPLICATE_ID = 114840
HISTORICAL_EDITORIAL_QUALITY_IDS = frozenset({115002, 115004})
HISTORICAL_VISION_PROVIDER_ID = 115025
HISTORICAL_MEDIA_FUNNEL_INVARIANT_IDS = frozenset({115025})
VISION_PROVIDER_RETRY_RELEASE_ID = 115025
HISTORICAL_COMPOSE_RECOVERY_ID = 114987


def _as_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _is_future(value: str | None, now: datetime) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed > now


def _read_state(post: dict[str, Any]) -> WorkState | None:
    meta = post.get("meta") if isinstance(post, dict) else None
    raw = meta.get("_hermes_work_state") if isinstance(meta, dict) else None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    if not isinstance(raw, dict):
        return None
    try:
        return WorkState.from_dict(raw)
    except (TypeError, ValueError, KeyError):
        return None


def _migrate_state(state: WorkState, now: datetime) -> WorkState | None:
    if state.state is not LifecycleState.PENDING:
        return None
    if state.blocker not in MEDIA_BLOCKERS:
        return None
    if state.retry.policy_version >= CURRENT_RETRY_POLICY_VERSION:
        return None
    if not _is_future(state.retry.next_at, now):
        return None
    retry = RetryInfo(
        attempts=state.retry.attempts,
        no_progress=state.retry.no_progress,
        next_at=now.isoformat(timespec="seconds"),
        policy_version=CURRENT_RETRY_POLICY_VERSION,
    )
    return WorkState(
        state=state.state,
        phase=state.phase,
        blocker=state.blocker,
        retry=retry,
        relevance_approved=state.relevance_approved,
        media=state.media,
        version=state.version,
    )


def rebase_media_cooldowns(
    client: Any,
    *,
    apply: bool = False,
    limit: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Rebase only future media cooldowns created by an older policy.

    The migration is deliberately narrow: it scans pending posts, requires a
    V2 pending state with a media blocker and a future ``next_at``, and changes
    only the retry timestamp plus the persisted policy marker. Attempts,
    no-progress, phase, blocker, relevance and media progress remain intact.
    Without ``apply`` it is a read-only report.
    """
    current = _as_utc(now)
    scanned = 0
    candidates: list[dict[str, Any]] = []
    seen: set[int] = set()
    page = 1
    while True:
        posts = client.list_pending(page=page, per_page=100, status="pending")
        if not posts:
            break
        for post in posts:
            post_id = post.get("id") if isinstance(post, dict) else None
            if not isinstance(post_id, int) or post_id in seen:
                continue
            seen.add(post_id)
            scanned += 1
            state = _read_state(post)
            migrated = _migrate_state(state, current) if state is not None else None
            if migrated is None:
                continue
            candidates.append({
                "post_id": post_id,
                "state": state,
                "migrated": migrated,
            })
            if limit and len(candidates) >= limit:
                break
        if limit and len(candidates) >= limit:
            break
        if len(posts) < 100:
            break
        page += 1

    migrated_ids: list[int] = []
    if apply:
        for item in candidates:
            post_id = item["post_id"]
            post = client.get_post(post_id)
            current_state = _read_state(post)
            migrated = _migrate_state(current_state, current)
            if migrated is None:
                # The post changed after the scan; never overwrite a newer
                # pipeline decision with a stale migration candidate.
                continue
            meta = post.get("meta") if isinstance(post, dict) else None
            if not isinstance(meta, dict):
                raise RuntimeError(f"post {post_id} has no editable meta payload")
            updated_meta = dict(meta)
            updated_meta["_hermes_work_state"] = json.dumps(
                migrated.to_dict(), ensure_ascii=False, separators=(",", ":")
            )
            client.update_post(post_id, {"meta": updated_meta})
            verified = _read_state(client.get_post(post_id))
            if verified != migrated:
                raise RuntimeError(f"V2 retry migration read-back mismatch for post {post_id}")
            migrated_ids.append(post_id)

    return {
        "command": "v2-rebase-cooldowns",
        "apply": bool(apply),
        "policy_version": CURRENT_RETRY_POLICY_VERSION,
        "scanned": scanned,
        "candidates": len(candidates),
        "migrated": len(migrated_ids) if apply else 0,
        "post_ids": migrated_ids if apply else [item["post_id"] for item in candidates],
        "next_at": current.isoformat(timespec="seconds"),
    }


def _migrate_editorial_state(state: WorkState, now: datetime) -> WorkState | None:
    if state.state is not LifecycleState.PENDING:
        return None
    if state.phase is not Phase.EDITORIAL:
        return None
    if state.blocker not in EDITORIAL_BLOCKERS:
        return None
    if state.retry.policy_version >= CURRENT_RETRY_POLICY_VERSION:
        return None
    if not _is_future(state.retry.next_at, now):
        return None
    retry = RetryInfo(
        attempts=state.retry.attempts,
        no_progress=state.retry.no_progress,
        next_at=now.isoformat(timespec="seconds"),
        policy_version=CURRENT_RETRY_POLICY_VERSION,
        phase_attempts=0,
    )
    return WorkState(
        state=state.state,
        phase=state.phase,
        blocker=state.blocker,
        retry=retry,
        relevance_approved=state.relevance_approved,
        media=state.media,
        version=state.version,
    )


def rebase_editorial_retries(
    client: Any,
    *,
    apply: bool = False,
    limit: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Release future editorial cooldowns created by the global retry budget."""
    current = _as_utc(now)
    scanned = 0
    candidates: list[dict[str, Any]] = []
    seen: set[int] = set()
    page = 1
    while True:
        posts = client.list_pending(page=page, per_page=100, status="pending")
        if not posts:
            break
        for post in posts:
            post_id = post.get("id") if isinstance(post, dict) else None
            if not isinstance(post_id, int) or post_id in seen:
                continue
            seen.add(post_id)
            scanned += 1
            state = _read_state(post)
            migrated = _migrate_editorial_state(state, current) if state is not None else None
            if migrated is None:
                continue
            candidates.append({"post_id": post_id})
            if limit and len(candidates) >= limit:
                break
        if limit and len(candidates) >= limit:
            break
        if len(posts) < 100:
            break
        page += 1

    migrated_ids: list[int] = []
    if apply:
        for item in candidates:
            post_id = item["post_id"]
            post = client.get_post(post_id)
            current_state = _read_state(post)
            migrated = _migrate_editorial_state(current_state, current)
            if migrated is None:
                continue
            meta = post.get("meta") if isinstance(post, dict) else None
            if not isinstance(meta, dict):
                raise RuntimeError(f"post {post_id} has no editable meta payload")
            updated_meta = dict(meta)
            updated_meta["_hermes_work_state"] = json.dumps(
                migrated.to_dict(), ensure_ascii=False, separators=(",", ":")
            )
            client.update_post(post_id, {"meta": updated_meta})
            verified = _read_state(client.get_post(post_id))
            if verified != migrated:
                raise RuntimeError(f"V2 editorial retry migration read-back mismatch for post {post_id}")
            migrated_ids.append(post_id)

    return {
        "command": "v2-rebase-editorial-retries",
        "apply": bool(apply),
        "policy_version": CURRENT_RETRY_POLICY_VERSION,
        "scanned": scanned,
        "candidates": len(candidates),
        "migrated": len(migrated_ids) if apply else 0,
        "post_ids": migrated_ids if apply else [item["post_id"] for item in candidates],
        "next_at": current.isoformat(timespec="seconds"),
    }


def _repair_historical_media_state(state: WorkState, now: datetime) -> WorkState | None:
    """Reopen only the known false terminal states from the v3 drain."""
    if state.state is not LifecycleState.HUMAN_REQUIRED:
        return None
    if state.phase is not Phase.MEDIA or state.blocker not in MEDIA_BLOCKERS:
        return None
    if state.retry.policy_version != CURRENT_RETRY_POLICY_VERSION:
        return None
    if state.retry.phase_attempts != 1 or state.retry.no_progress < 2:
        return None
    retry = RetryInfo(
        attempts=state.retry.attempts,
        no_progress=1,
        next_at=now.isoformat(timespec="seconds"),
        policy_version=CURRENT_RETRY_POLICY_VERSION,
        phase_attempts=state.retry.phase_attempts,
    )
    return WorkState(
        state=LifecycleState.PENDING,
        phase=Phase.MEDIA,
        blocker=state.blocker,
        retry=retry,
        relevance_approved=state.relevance_approved,
        media=state.media,
        version=state.version,
    )


def repair_compose_114987(
    client: Any,
    config: Any,
    root: Path | str,
    *,
    apply: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Dry-run-first recovery for the exact 114987 stale-COMPOSE signature.

    This validates the four already accepted attachments and reconstructs the
    candidate locally.  Applying the repair changes only the V2 work-state
    meta to reopen COMPOSE; the next normal V2 run must compose, validate,
    write and read back the post again.
    """
    from .lock import RunSessionLock
    from .production_stages import ProductionComposeStage
    from .runtime import _canonical_embedded_media_url

    post_id = HISTORICAL_COMPOSE_RECOVERY_ID
    root_path = Path(root)
    current = _as_utc(now)
    lock = RunSessionLock(root_path / "work" / "v2-run.lock")
    if not lock.acquire():
        return {
            "command": "v2-repair-compose-114987",
            "apply": bool(apply),
            "post_id": post_id,
            "eligible": False,
            "reason": "v2_run_locked",
            "read_only": not apply,
        }
    try:
        post = client.get_post(post_id)
        state = _read_state(post)
        directory = root_path / "backups" / str(post_id)
        journal_path = root_path / "work" / "v2-journal" / f"{post_id}.json"
        journal: dict[str, Any] = {}
        if journal_path.is_file():
            try:
                value = json.loads(journal_path.read_text(encoding="utf-8"))
                journal = value if isinstance(value, dict) else {}
            except (OSError, ValueError, json.JSONDecodeError):
                journal = {}

        checks: dict[str, Any] = {
            "wordpress_pending": post.get("status") == "pending",
            "v2_signature": bool(
                state is not None
                and state.state is LifecycleState.HUMAN_REQUIRED
                and state.phase is Phase.COMPOSE
                and state.blocker is BlockerCode.MANIFEST_INVALID
            ),
            "journal_committing": journal.get("status") == "committing",
            "journal_post_id": journal.get("post_id") in {None, post_id},
        }
        expected_ids = (115134, 115135, 115136, 115137)
        media_checks: list[dict[str, Any]] = []
        if state is not None:
            actual_ids = tuple(item.media_id for item in state.media.inline)
            checks["accepted_media_signature"] = (
                state.media.required == 4
                and len(actual_ids) == 4
                and set(actual_ids) == set(expected_ids)
                and state.media.featured.status.value == "valid"
            )
            checks["accepted_media_ids"] = list(actual_ids)
        else:
            checks["accepted_media_signature"] = False
            checks["accepted_media_ids"] = []

        for media_id in expected_ids:
            try:
                attachment = client.get_media(media_id)
                source_url = str((attachment or {}).get("source_url") or "").strip()
                details = (attachment or {}).get("media_details") or {}
                mime = str(details.get("mime_type") or (attachment or {}).get("mime_type") or "").lower()
                media_checks.append({
                    "media_id": media_id,
                    "valid": bool(source_url)
                    and (source_url.lower().split("?", 1)[0].endswith(".webp") or mime == "image/webp"),
                    "source_url": source_url,
                    "mime_type": mime or None,
                    "provenance": "attachment_source_url_present" if source_url else "missing_source_url",
                })
            except Exception as exc:  # noqa: BLE001 - report per attachment
                media_checks.append({"media_id": media_id, "valid": False, "reason": str(exc)})
        checks["attachments_valid"] = bool(media_checks) and all(item.get("valid") for item in media_checks)
        checks["attachment_identity"] = bool(state is not None) and all(
            _canonical_embedded_media_url(str(item.get("source_url") or "")) == next(
                (
                    _canonical_embedded_media_url(str(media.media_url or ""))
                    for media in state.media.inline
                    if media.media_id == item["media_id"]
                ),
                None,
            )
            for item in media_checks
            if item.get("valid")
        )
        checks["accepted_media_evidence"] = bool(state is not None) and all(
            bool(item.media_url and item.subject and item.credit_text)
            for item in state.media.inline
        )

        partial: dict[str, Any] = {}
        draft: dict[str, Any] = {}
        try:
            partial_value = json.loads((directory / "editorial.partial.json").read_text(encoding="utf-8"))
            partial = partial_value if isinstance(partial_value, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        for name in ("editorial.draft.json", "editorial.latest.json"):
            try:
                draft_value = json.loads((directory / name).read_text(encoding="utf-8"))
                if isinstance(draft_value, dict):
                    draft = draft_value
                    break
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        checks["partial_checkpoint"] = False
        if state is not None and partial:
            try:
                from .model import MediaProgress

                partial_media = MediaProgress.from_dict(partial)
                checks["partial_checkpoint"] = (
                    partial_media.required == state.media.required
                    and {item.media_id for item in partial_media.inline}
                    == {item.media_id for item in state.media.inline}
                    and partial_media.featured.media_id == state.media.featured.media_id
                )
            except (TypeError, ValueError, KeyError):
                checks["partial_checkpoint"] = False
        checks["editorial_draft"] = bool(draft)
        try:
            validate_editorial(draft, min_confidence=float(getattr(config, "min_relevance_confidence", 0.8)))
            checks["editorial_contract"] = True
        except (EditorialValidationError, TypeError, ValueError):
            checks["editorial_contract"] = False
        current_html = str((post.get("content") or {}).get("raw") or "")
        from ..media.relevance import iter_content_images

        embedded_urls = {
            _canonical_embedded_media_url(str(item.get("src") or ""))
            for item in iter_content_images(current_html)
        }
        checks["accepted_media_absent_from_html"] = bool(state is not None) and all(
            _canonical_embedded_media_url(str(item.media_url or "")) not in embedded_urls
            for item in state.media.inline
        )
        checks["concurrent_run"] = True
        required_checks = (
            "wordpress_pending",
            "v2_signature",
            "journal_committing",
            "journal_post_id",
            "accepted_media_signature",
            "attachments_valid",
            "attachment_identity",
            "accepted_media_evidence",
            "partial_checkpoint",
            "editorial_draft",
            "editorial_contract",
            "accepted_media_absent_from_html",
            "concurrent_run",
        )
        checks["safe_signature"] = all(bool(checks.get(name)) for name in required_checks)

        result: dict[str, Any] = {
            "command": "v2-repair-compose-114987",
            "post_id": post_id,
            "apply": bool(apply),
            "read_only": not apply,
            "eligible": False,
            "checks": checks,
            "attachments": media_checks,
            "journal": {
                "path": str(journal_path),
                "status": journal.get("status"),
                "run_id": journal.get("run_id"),
            },
            "preserve": {
                "attempts": state.retry.attempts if state else None,
                "phase_attempts": state.retry.phase_attempts if state else None,
                "media_ids": list(checks.get("accepted_media_ids") or []),
            },
        }
        if not checks["safe_signature"] or state is None or not draft:
            result["reason"] = "signature_or_checkpoints_not_verified"
            return result

        context = {
            "post_id": post_id,
            "post": post,
            "title": (post.get("title") or {}).get("raw") or (post.get("title") or {}).get("rendered") or "",
            "original_link": (post.get("meta") or {}).get("original_link"),
        }
        try:
            candidate = ProductionComposeStage(config, root_path).compose_candidate(
                context, draft, state.media, persist=False
            )
        except Exception as exc:  # noqa: BLE001 - dry-run must report, never mutate
            result["reason"] = "composition_not_reconstructable"
            result["composition_error"] = str(exc)
            result["eligible"] = False
            return result
        result["reconstructed_content"] = candidate.get("content")
        result["media_placements"] = candidate.get("media_placements") or []
        reconstructed_urls = {
            _canonical_embedded_media_url(str(image.get("src") or ""))
            for image in iter_content_images(str(candidate.get("content") or ""))
        }
        reconstructed_ids = [
            media_id
            for media_id in checks["accepted_media_ids"]
            if any(
                _canonical_embedded_media_url(str(item.media_url or "")) in reconstructed_urls
                for item in state.media.inline
                if item.media_id == media_id
            )
        ]
        result["validation"] = {
            "accepted_media_in_reconstructed_content": reconstructed_ids,
            "all_accepted_media_reconstructed": set(reconstructed_ids) == set(checks["accepted_media_ids"]),
            "ready_written": False,
            "new_uploads": 0,
            "next_stage": "v2-run COMPOSE/checklist/writer/readback",
        }
        if not result["validation"]["all_accepted_media_reconstructed"]:
            result["reason"] = "reconstructed_content_missing_accepted_media"
            return result
        result["eligible"] = True
        if apply:
            reopened = replace(
                state,
                state=LifecycleState.PENDING,
                phase=Phase.COMPOSE,
                blocker=BlockerCode.MANIFEST_INVALID,
                retry=replace(state.retry, next_at=current.isoformat(timespec="seconds")),
            )
            meta = post.get("meta") if isinstance(post.get("meta"), dict) else None
            if meta is None:
                raise RuntimeError("post 114987 has no editable meta payload")
            updated_meta = dict(meta)
            updated_meta["_hermes_work_state"] = json.dumps(
                reopened.to_dict(), ensure_ascii=False, separators=(",", ":")
            )
            client.update_post(post_id, {"meta": updated_meta})
            verified = _read_state(client.get_post(post_id))
            if verified != reopened:
                raise RuntimeError("114987 compose recovery read-back mismatch")
            result["reopened_state"] = reopened.to_dict()
            result["readback"] = True
        return result
    finally:
        lock.release()


def repair_historical_media_human_required(
    client: Any,
    *,
    apply: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Safely reopen the nine audited false HUMAN_REQUIRED media states.

    This is intentionally allowlisted and idempotent. It refuses any post
    outside the audited drain or whose current state no longer matches the
    exact bug signature, so it cannot become a generic HUMAN_REQUIRED reset.
    The three known media-loss posts are reported separately and are never
    restored without an independently verified media identity.
    """
    current = _as_utc(now)
    candidates: list[int] = []
    skipped: list[dict[str, Any]] = []
    for post_id in sorted(HISTORICAL_MEDIA_HUMAN_REQUIRED_IDS):
        try:
            post = client.get_post(post_id)
        except Exception as exc:  # noqa: BLE001 - report one unavailable post
            skipped.append({"post_id": post_id, "reason": f"read_error: {exc}"})
            continue
        state = _read_state(post)
        migrated = _repair_historical_media_state(state, current) if state is not None else None
        if migrated is None:
            skipped.append({"post_id": post_id, "reason": "state_signature_mismatch"})
            continue
        candidates.append(post_id)

    migrated_ids: list[int] = []
    if apply:
        for post_id in candidates:
            post = client.get_post(post_id)
            state = _read_state(post)
            migrated = _repair_historical_media_state(state, current) if state is not None else None
            if migrated is None:
                skipped.append({"post_id": post_id, "reason": "changed_after_scan"})
                continue
            meta = post.get("meta") if isinstance(post, dict) else None
            if not isinstance(meta, dict):
                raise RuntimeError(f"post {post_id} has no editable meta payload")
            updated_meta = dict(meta)
            updated_meta["_hermes_work_state"] = json.dumps(
                migrated.to_dict(), ensure_ascii=False, separators=(",", ":")
            )
            client.update_post(post_id, {"meta": updated_meta})
            verified = _read_state(client.get_post(post_id))
            if verified != migrated:
                raise RuntimeError(f"historical media repair read-back mismatch for post {post_id}")
            migrated_ids.append(post_id)

    return {
        "command": "v2-repair-historical-media",
        "apply": bool(apply),
        "policy_version": CURRENT_RETRY_POLICY_VERSION,
        "audited_human_required_ids": sorted(HISTORICAL_MEDIA_HUMAN_REQUIRED_IDS),
        "candidates": len(candidates),
        "migrated": len(migrated_ids) if apply else 0,
        "post_ids": migrated_ids if apply else candidates,
        "skipped": skipped,
        "media_loss_requires_evidence": sorted(HISTORICAL_MEDIA_LOSS_IDS),
        "next_at": current.isoformat(timespec="seconds"),
    }


def _repair_known_terminal_state(
    post_id: int,
    state: WorkState,
    now: datetime,
    *,
    max_rework_attempts: int = 3,
) -> WorkState | None:
    """Reopen only the three terminal states caused by the identified bugs."""
    if state.state is not LifecycleState.HUMAN_REQUIRED:
        return None
    if state.retry.policy_version != CURRENT_RETRY_POLICY_VERSION:
        return None
    if post_id == HISTORICAL_MEDIA_DUPLICATE_ID:
        if (
            state.phase is not Phase.MEDIA
            or state.blocker is not BlockerCode.MEDIA_DUPLICATE
            or state.media.required != 4
            or state.media.accepted != 3
            or state.retry.no_progress != 2
        ):
            return None
        retry = RetryInfo(
            attempts=state.retry.attempts,
            no_progress=1,
            next_at=now.isoformat(timespec="seconds"),
            policy_version=CURRENT_RETRY_POLICY_VERSION,
            phase_attempts=state.retry.phase_attempts,
        )
        return WorkState(
            state=LifecycleState.PENDING,
            phase=Phase.MEDIA,
            blocker=state.blocker,
            retry=retry,
            relevance_approved=state.relevance_approved,
            media=state.media,
            version=state.version,
        )
    if post_id in HISTORICAL_EDITORIAL_QUALITY_IDS:
        if (
            state.phase is not Phase.EDITORIAL
            or state.blocker is not BlockerCode.TEXT_QUALITY
            or state.retry.phase_attempts != max_rework_attempts
        ):
            return None
        retry = RetryInfo(
            attempts=state.retry.attempts,
            no_progress=state.retry.no_progress,
            next_at=now.isoformat(timespec="seconds"),
            policy_version=CURRENT_RETRY_POLICY_VERSION,
            phase_attempts=max(0, max_rework_attempts - 1),
        )
        return WorkState(
            state=LifecycleState.PENDING,
            phase=Phase.EDITORIAL,
            blocker=state.blocker,
            retry=retry,
            relevance_approved=state.relevance_approved,
            media=state.media,
            version=state.version,
        )
    return None


def repair_known_terminal_states(
    client: Any,
    *,
    apply: bool = False,
    now: datetime | None = None,
    max_rework_attempts: int = 3,
) -> dict[str, Any]:
    """Reopen the exact MEDIA_DUPLICATE/TEXT_QUALITY regressions once."""
    current = _as_utc(now)
    post_ids = (HISTORICAL_MEDIA_DUPLICATE_ID, *sorted(HISTORICAL_EDITORIAL_QUALITY_IDS))
    candidates: list[int] = []
    skipped: list[dict[str, Any]] = []
    for post_id in post_ids:
        try:
            post = client.get_post(post_id)
        except Exception as exc:  # noqa: BLE001 - preserve per-post audit
            skipped.append({"post_id": post_id, "reason": f"read_error: {exc}"})
            continue
        state = _read_state(post)
        migrated = _repair_known_terminal_state(
            post_id, state, current, max_rework_attempts=max_rework_attempts
        ) if state is not None else None
        if migrated is None:
            skipped.append({"post_id": post_id, "reason": "state_signature_mismatch"})
            continue
        candidates.append(post_id)

    migrated_ids: list[int] = []
    if apply:
        for post_id in candidates:
            post = client.get_post(post_id)
            state = _read_state(post)
            migrated = _repair_known_terminal_state(
                post_id, state, current, max_rework_attempts=max_rework_attempts
            ) if state is not None else None
            if migrated is None:
                skipped.append({"post_id": post_id, "reason": "changed_after_scan"})
                continue
            meta = post.get("meta") if isinstance(post, dict) else None
            if not isinstance(meta, dict):
                raise RuntimeError(f"post {post_id} has no editable meta payload")
            updated_meta = dict(meta)
            updated_meta["_hermes_work_state"] = json.dumps(
                migrated.to_dict(), ensure_ascii=False, separators=(",", ":")
            )
            client.update_post(post_id, {"meta": updated_meta})
            if _read_state(client.get_post(post_id)) != migrated:
                raise RuntimeError(f"known terminal repair read-back mismatch for post {post_id}")
            migrated_ids.append(post_id)

    return {
        "command": "v2-repair-known-terminal-states",
        "apply": bool(apply),
        "policy_version": CURRENT_RETRY_POLICY_VERSION,
        "allowlisted_post_ids": list(post_ids),
        "candidates": len(candidates),
        "migrated": len(migrated_ids) if apply else 0,
        "post_ids": migrated_ids if apply else candidates,
        "skipped": skipped,
        "next_at": current.isoformat(timespec="seconds"),
    }


def repair_media_provider_terminal_states(
    client: Any,
    *,
    apply: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reopen only the audited duplicate and Vision-provider terminal states."""
    current = _as_utc(now)
    allowlist = (HISTORICAL_MEDIA_DUPLICATE_ID, HISTORICAL_VISION_PROVIDER_ID)
    candidates: list[int] = []
    skipped: list[dict[str, Any]] = []

    prepared: dict[int, WorkState] = {}
    original: dict[int, WorkState] = {}
    for post_id in allowlist:
        try:
            post = client.get_post(post_id)
        except Exception as exc:  # noqa: BLE001 - preserve per-post audit
            skipped.append({"post_id": post_id, "reason": f"read_error: {exc}"})
            continue
        state = _read_state(post)
        if state is None or state.phase is not Phase.MEDIA or state.retry.policy_version != CURRENT_RETRY_POLICY_VERSION:
            skipped.append({"post_id": post_id, "reason": "state_signature_mismatch"})
            continue
        original[post_id] = state
        if post_id == HISTORICAL_MEDIA_DUPLICATE_ID:
            if (
                state.state is not LifecycleState.HUMAN_REQUIRED
                or state.blocker is not BlockerCode.MEDIA_DUPLICATE
                or state.retry.no_progress != 2
                or state.media.required != 4
                or state.media.accepted != 3
            ):
                skipped.append({"post_id": post_id, "reason": "state_signature_mismatch"})
                continue
            from ..media.visual_hash import image_hashes

            urls = [item.media_url for item in state.media.inline]
            hashes = image_hashes(urls)
            if any(not hashes.get(url) for url in urls):
                skipped.append({"post_id": post_id, "reason": "baseline_phash_unavailable"})
                continue
            inline = tuple(
                replace(item, phash=str(hashes[item.media_url]))
                for item in state.media.inline
            )
            media = replace(state.media, inline=inline)
            retry = replace(
                state.retry,
                no_progress=1,
                next_at=current.isoformat(timespec="seconds"),
            )
            prepared[post_id] = replace(
                state,
                state=LifecycleState.PENDING,
                retry=retry,
                media=media,
            )
            candidates.append(post_id)
            continue
        if (
            state.state is not LifecycleState.PENDING
            or state.blocker is not BlockerCode.MEDIA_INVALID
            or state.retry.attempts != 6
            or state.retry.phase_attempts != 1
            or state.retry.no_progress != 1
            or state.media.required != 4
            or state.media.accepted != 0
        ):
            skipped.append({"post_id": post_id, "reason": "state_signature_mismatch"})
            continue
        prepared[post_id] = replace(
            state,
            blocker=BlockerCode.PROVIDER_ERROR,
            retry=replace(
                state.retry,
                no_progress=0,
                next_at=current.isoformat(timespec="seconds"),
            ),
        )
        candidates.append(post_id)

    migrated_ids: list[int] = []
    if apply:
        for post_id in candidates:
            post = client.get_post(post_id)
            migrated = prepared.get(post_id)
            state = _read_state(post)
            if migrated is None or state is None or state != original.get(post_id):
                skipped.append({"post_id": post_id, "reason": "changed_after_scan"})
                continue
            meta = post.get("meta") if isinstance(post, dict) else None
            if not isinstance(meta, dict):
                raise RuntimeError(f"post {post_id} has no editable meta payload")
            updated_meta = dict(meta)
            updated_meta["_hermes_work_state"] = json.dumps(
                migrated.to_dict(), ensure_ascii=False, separators=(",", ":")
            )
            client.update_post(post_id, {"meta": updated_meta})
            if _read_state(client.get_post(post_id)) != migrated:
                raise RuntimeError(f"media provider repair read-back mismatch for post {post_id}")
            migrated_ids.append(post_id)

    return {
        "command": "v2-repair-media-provider-states",
        "apply": bool(apply),
        "policy_version": CURRENT_RETRY_POLICY_VERSION,
        "allowlisted_post_ids": list(allowlist),
        "candidates": len(candidates),
        "migrated": len(migrated_ids) if apply else 0,
        "post_ids": migrated_ids if apply else candidates,
        "skipped": skipped,
        "next_at": current.isoformat(timespec="seconds"),
    }
def repair_media_funnel_invariant_state(
    client: Any,
    *,
    root: Path | str = ".",
    apply: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reopen only the audited 115025 funnel-invariant terminalization."""
    current = _as_utc(now)
    post_id = 115025
    root_path = Path(root)
    skipped: list[dict[str, Any]] = []
    try:
        post = client.get_post(post_id)
    except Exception:  # noqa: BLE001 - absent allowlisted post is not a candidate
        return {
            "command": "v2-repair-media-funnel-invariant",
            "apply": bool(apply),
            "policy_version": CURRENT_RETRY_POLICY_VERSION,
            "allowlisted_post_ids": sorted(HISTORICAL_MEDIA_FUNNEL_INVARIANT_IDS),
            "candidates": 0,
            "migrated": 0,
            "post_ids": [],
            "skipped": [],
            "next_at": current.isoformat(timespec="seconds"),
        }
    state = _read_state(post)
    signature_ok = (
        state is not None
        and state.state is LifecycleState.PENDING
        and state.phase is Phase.MEDIA
        and state.blocker is BlockerCode.MEDIA_INVALID
        and state.retry.policy_version == CURRENT_RETRY_POLICY_VERSION
        and state.retry.attempts == 7
        and state.retry.phase_attempts == 2
        and state.retry.no_progress == 1
        and state.media.required == 4
        and state.media.accepted == 0
    )
    evidence: list[str] = []
    journal_path = root_path / "work" / "v2-journal" / f"{post_id}.json"
    if journal_path.exists():
        try:
            payload = json.loads(journal_path.read_text(encoding="utf-8"))
            evidence.append(str(payload.get("detail") or ""))
        except (OSError, ValueError):
            evidence.append("")
    if signature_ok and evidence and evidence[0] and evidence[0] != "media candidate conservation violated":
        signature_ok = False
        skipped.append({"post_id": post_id, "reason": "historical_detail_mismatch"})
    elif not signature_ok:
        skipped.append({"post_id": post_id, "reason": "state_signature_mismatch"})
    prepared = None
    if signature_ok and state is not None:
        prepared = replace(
            state,
            blocker=BlockerCode.INTERNAL_ERROR,
            retry=replace(state.retry, no_progress=0, next_at=current.isoformat(timespec="seconds")),
        )
    migrated_ids: list[int] = []
    if apply and prepared is not None:
        reread = _read_state(client.get_post(post_id))
        if reread != state:
            skipped.append({"post_id": post_id, "reason": "changed_after_scan"})
        else:
            meta = post.get("meta") if isinstance(post, dict) else None
            if not isinstance(meta, dict):
                raise RuntimeError(f"post {post_id} has no editable meta payload")
            updated_meta = dict(meta)
            updated_meta["_hermes_work_state"] = json.dumps(
                prepared.to_dict(), ensure_ascii=False, separators=(",", ":")
            )
            client.update_post(post_id, {"meta": updated_meta})
            if _read_state(client.get_post(post_id)) != prepared:
                raise RuntimeError(f"media funnel invariant repair read-back mismatch for post {post_id}")
            migrated_ids.append(post_id)
    return {
        "command": "v2-repair-media-funnel-invariant",
        "apply": bool(apply),
        "policy_version": CURRENT_RETRY_POLICY_VERSION,
        "allowlisted_post_ids": sorted(HISTORICAL_MEDIA_FUNNEL_INVARIANT_IDS),
        "candidates": 1 if prepared is not None else 0,
        "migrated": len(migrated_ids),
        "post_ids": migrated_ids if apply else ([post_id] if prepared is not None else []),
        "skipped": skipped,
        "next_at": current.isoformat(timespec="seconds"),
    }
def release_vision_provider_retry(
    client: Any,
    *,
    apply: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Advance only the audited 115025 provider retry without resetting state."""
    current = _as_utc(now)
    post_id = VISION_PROVIDER_RETRY_RELEASE_ID
    skipped: list[dict[str, Any]] = []
    prepared: WorkState | None = None
    original: WorkState | None = None
    try:
        post = client.get_post(post_id)
    except Exception as exc:  # noqa: BLE001 - preserve audit output
        skipped.append({"post_id": post_id, "reason": f"read_error: {exc}"})
    else:
        state = _read_state(post)
        if (
            state is None
            or state.state is not LifecycleState.PENDING
            or state.phase is not Phase.MEDIA
            or state.blocker is not BlockerCode.PROVIDER_ERROR
            or state.retry.policy_version != CURRENT_RETRY_POLICY_VERSION
            or state.retry.attempts != 8
            or state.retry.phase_attempts != 3
            or state.retry.no_progress != 0
            or state.media.required != 4
            or state.media.accepted != 0
            or not _is_future(state.retry.next_at, current)
        ):
            skipped.append({"post_id": post_id, "reason": "state_signature_mismatch"})
        else:
            original = state
            prepared = replace(
                state,
                retry=replace(state.retry, next_at=current.isoformat(timespec="seconds")),
            )

    migrated = False
    if apply and prepared is not None and original is not None:
        post = client.get_post(post_id)
        state = _read_state(post)
        if state != original:
            skipped.append({"post_id": post_id, "reason": "changed_after_scan"})
        else:
            meta = post.get("meta") if isinstance(post, dict) else None
            if not isinstance(meta, dict):
                raise RuntimeError(f"post {post_id} has no editable meta payload")
            updated_meta = dict(meta)
            updated_meta["_hermes_work_state"] = json.dumps(
                prepared.to_dict(), ensure_ascii=False, separators=(",", ":")
            )
            client.update_post(post_id, {"meta": updated_meta})
            if _read_state(client.get_post(post_id)) != prepared:
                raise RuntimeError(f"Vision provider retry release read-back mismatch for post {post_id}")
            migrated = True

    return {
        "command": "v2-release-vision-provider-retry",
        "apply": bool(apply),
        "policy_version": CURRENT_RETRY_POLICY_VERSION,
        "allowlisted_post_ids": [post_id],
        "candidates": 1 if prepared is not None else 0,
        "migrated": 1 if migrated else 0,
        "post_ids": [post_id] if prepared is not None else [],
        "skipped": skipped,
        "next_at": current.isoformat(timespec="seconds"),
    }
