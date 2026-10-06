"""Minimal production bridge: buffered runner, writer, journal and v2-run."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from ..checklist import required_image_count
from ..content_quality import word_count
from ..manifest import build_ready_manifest, manifest_hash, serialize_manifest
from ..media.evidence import item_query, post_subjects
from ..list_quality import detect_list_format
from ..seo.rank_math import build_meta
from ..state import STATE_READY, build_state_markers
from ..workflow import _execute_media_plan, validate_media_plan
from .lock import RunSessionLock
from .model import FeaturedProgress, FeaturedStatus, InlineMedia, MediaProgress
from .production import ProductionCandidateReader
from .production_stages import ProductionComposeStage, ProductionEditorialStage, ProductionMediaStage, ProductionValidateStage
from .runner import PipelineRunner
from .scheduler import _cooldown_expired, select


class BufferedStateStore:
    def __init__(self, initial):
        self.state = initial

    def load(self, post_id: int):
        return self.state

    def commit(self, post_id: int, state):
        self.state = state


def _write_v2_media_checkpoint(root: Path, post_id: int, media: MediaProgress) -> None:
    """Persist only the reconciled V2 media state for crash recovery."""
    target = Path(root) / "backups" / str(post_id) / "editorial.partial.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(media.to_dict(), ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    temporary.replace(target)


def _write_v2_journal_checkpoint(root: Path, post_id: int, status: str, state) -> None:
    target = Path(root) / "work" / "v2-journal" / f"{post_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {"status": status, "post_id": post_id, "state": state.to_dict()},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _normalize_executable_candidate(candidate: dict[str, Any], subject: str) -> dict[str, Any]:
    result = dict(candidate)
    source = str(result.get("source_page_url") or "").strip()
    host = (urlparse(source).hostname or "").strip()
    result["author"] = result.get("author") or result.get("publisher") or host or "Fonte original"
    result["license"] = result.get("license") or "Uso com crédito"
    result["license_url"] = result.get("license_url") or source
    result["captured_at"] = result.get("captured_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")
    result["credit_text"] = result.get("credit_text") or f"Crédito da imagem: {host or 'fonte original'}"
    result["alt_text"] = result.get("alt_text") or subject
    return result


class ProductionMediaResolver:
    """Reuse the existing media-resolve core and execute only the deficit."""
    def __init__(self, client, config, root: Path):
        self.client, self.config, self.root = client, config, Path(root)

    def __call__(self, context, state, editorial, previous):
        html = str(editorial.get("cleaned_html") or "")
        title = str(context.get("title") or "")
        focus_keyword = str((editorial.get("seo") or {}).get("focus_keyword") or "")
        total_required = required_image_count(word_count(html), title=title, content=html)
        inline_needed = max(0, total_required - previous.accepted)
        featured_needed = 0 if previous.featured.status is FeaturedStatus.VALID else 1

        from ..cli import _resolve_media_batch

        subject_rows = post_subjects(
            title=title,
            content_html=html,
            focus_keyword=focus_keyword,
            game_name=editorial.get("game_name"),
        )
        is_listicle = detect_list_format(title, html) is not None
        covered_items = {
            str(item.item_number)
            for item in previous.inline
            if item.item_number is not None
        }
        covered_subjects = {
            item.subject.casefold().strip()
            for item in previous.inline
            if item.subject.strip()
        }
        missing_rows = [
            (index, row) for index, row in enumerate(subject_rows)
            if not is_listicle
            or (
                str(row.get("item")) not in covered_items
                and str(row.get("subject") or "").casefold().strip() not in covered_subjects
            )
        ]
        if is_listicle:
            # A migrated listicle may have accepted media without item_number.
            # Its deficit is the number of uncovered subjects, not the global
            # article quota minus a positional count.
            inline_needed = len(missing_rows)
        if inline_needed == 0 and featured_needed == 0:
            return MediaProgress(
                required=total_required,
                inline=previous.inline,
                featured=previous.featured,
            )
        subject_queries: list[tuple[str, str]] = []
        seen_queries: set[str] = set()
        subject_meta: dict[str, dict[str, Any]] = {}

        def add_query(subject: str, query: str) -> None:
            subject = " ".join(str(subject or "").split()).strip()
            query = " ".join(str(query or "").split()).strip()
            if query and query.casefold() not in seen_queries:
                subject_queries.append((subject or query, query))
                seen_queries.add(query.casefold())

        for index, row in missing_rows:
            subject = str(row.get("subject") or "").strip()
            if subject:
                subject_meta[subject] = {**row, "section_slot": row.get("section_slot", index)}
            add_query(subject, item_query(subject, title, extra=focus_keyword))
        if focus_keyword and not is_listicle:
            main_subject = str((subject_rows[0] if subject_rows else {}).get("subject") or focus_keyword)
            add_query(main_subject, item_query(focus_keyword, title))
        if not subject_queries and not is_listicle:
            add_query(title, item_query(title, title, extra=focus_keyword))

        def resolve_query(subject: str, query: str, needed: int, role: str) -> list[dict[str, Any]]:
            if needed <= 0:
                return []
            digest = hashlib.sha1(query.encode("utf-8", "ignore")).hexdigest()[:10]
            batch = {
                "schema_version": 1,
                "batch_id": f"v2-{context['post_id']}-{role}-{digest}",
                "posts": [{
                    "post_id": int(context["post_id"]),
                    "subject": subject,
                    "query": query,
                    "needed": needed,
                    "limit": max(needed, 3),
                    "engine": "auto",
                    "size": "xga",
                    "ratio": "w",
                }],
            }
            resolved = _resolve_media_batch(
                self.client, self.config, self.root, batch, full=True, allow_reuse=True
            )
            row = (resolved.get("posts") or [{}])[0]
            candidates: list[dict[str, Any]] = []
            for reused in row.get("reuse") or []:
                if not isinstance(reused, dict):
                    continue
                candidates.append({
                    **reused,
                    "direct_image_url": reused.get("direct_image_url") or reused.get("url"),
                    "source_page_url": reused.get("source_page_url") or reused.get("source"),
                    "media_library_id": reused.get("media_library_id") or reused.get("media_id"),
                    "subject": subject,
                    "search_query": query,
                    "role": role,
                    "evidence": reused.get("evidence") or {"verdict": "deterministic_match"},
                    "item_number": (subject_meta.get(subject) or {}).get("item"),
                    "section_heading": (subject_meta.get(subject) or {}).get("heading") or subject,
                    "section_slot": (subject_meta.get(subject) or {}).get("section_slot"),
                })
            for candidate in row.get("audit_candidates") or []:
                if not isinstance(candidate, dict):
                    continue
                candidate = dict(candidate)
                candidate["subject"] = subject
                candidate["search_query"] = query
                candidate["role"] = role
                metadata = subject_meta.get(subject) or {}
                candidate["item_number"] = metadata.get("item")
                candidate["section_heading"] = metadata.get("heading") or subject
                candidate["section_slot"] = metadata.get("section_slot")
                candidates.append(candidate)
            return candidates

        featured_candidates: list[dict[str, Any]] = []
        if featured_needed:
            featured_row = subject_rows[0] if subject_rows else {}
            featured_subject = str(featured_row.get("subject") or title)
            featured_query = item_query(featured_subject, title, extra=focus_keyword)
            # Key-art is a separate search role.  If the contextual query has
            # no usable result, the explicit key-art query provides a second
            # pool without ever turning an inline candidate into the featured.
            featured_queries = [
                (featured_subject, f"{featured_query} key art"),
                (featured_subject, featured_query),
            ]
            for subject, query in featured_queries:
                featured_candidates.extend(resolve_query(subject, query, 1, "featured"))
                if any(self._is_approved(candidate) for candidate in featured_candidates):
                    break

        inline_candidates: list[dict[str, Any]] = []
        if inline_needed:
            for subject, query in subject_queries:
                inline_candidates.extend(
                    resolve_query(subject, query, 1 if is_listicle else inline_needed, "inline")
                )
                # A listicle has an explicit subject identity per numbered
                # section. Search every item independently; do not let the
                # first item consume the whole article quota.
                if not is_listicle and len(self._approved_unique(inline_candidates)) >= inline_needed:
                    break

        all_candidates = self._unique_candidates(featured_candidates + inline_candidates)
        self._resolve_ambiguous_vision(all_candidates)
        featured_approved = [
            candidate for candidate in self._approved_unique(featured_candidates)
            if candidate.get("direct_image_url")
        ]
        featured_selected = featured_approved[0] if featured_approved else None
        featured_keys = {
            str(featured_selected.get("media_library_id") or featured_selected.get("direct_image_url") or "")
        } if featured_selected else set()
        inline_approved = [
            candidate for candidate in self._approved_unique(inline_candidates)
            if candidate.get("direct_image_url")
            and str(candidate.get("media_library_id") or candidate.get("direct_image_url") or "") not in featured_keys
        ]
        if is_listicle:
            per_item: list[dict[str, Any]] = []
            seen_items: set[str] = set()
            for candidate in inline_approved:
                item_key = str(candidate.get("item_number") or candidate.get("subject") or "")
                if item_key and item_key not in seen_items:
                    seen_items.add(item_key)
                    per_item.append(candidate)
            inline_approved = per_item + [
                candidate for candidate in inline_approved if candidate not in per_item
            ]

        plan: list[dict[str, Any]] = []
        if featured_needed and featured_selected:
            plan.append(self._plan_item(featured_selected, featured_selected.get("subject") or title, 0, True))
        used_slots = {item.slot for item in previous.inline}
        for candidate in inline_approved[:inline_needed]:
            if is_listicle:
                slot = int(candidate.get("section_slot", 0))
            else:
                slot = 0
                while slot in used_slots:
                    slot += 3
                used_slots.add(slot)
            plan.append(self._plan_item(candidate, candidate.get("subject") or title, slot, False))

        checked = validate_media_plan(
            self.client,
            {**editorial, "media_plan": plan},
            config=self.config,
            root=self.root,
            post_title=title,
            post_id=int(context["post_id"]),
            existing_featured_id=previous.featured.media_id if previous.featured.status is FeaturedStatus.VALID else None,
        )
        results, _featured_id, _featured_credit = _execute_media_plan(
            {**editorial, "media_plan": plan},
            self.config,
            self.client,
            self.root,
            preflight=checked,
            post_id=int(context["post_id"]),
        )
        inline = list(previous.inline)
        featured = previous.featured
        for result in results:
            status = result.get("status")
            if status and status not in {"accepted", "ok"}:
                continue
            if result.get("media_id") and result.get("media_url") and not result.get("featured"):
                plan_item = next(
                    (item for item in plan if int(item.get("paragraph_index", -1)) == int(result.get("paragraph_index", -2))),
                    {},
                )
                inline.append(InlineMedia(
                    int(result["media_id"]),
                    str(result["media_url"]),
                    int(result.get("paragraph_index", 0)),
                    str(result.get("alt_text", "")),
                    str(result.get("credit_text", "")),
                    str(plan_item.get("subject") or ""),
                    plan_item.get("item_number"),
                    str(plan_item.get("section_heading") or ""),
                    plan_item.get("section_slot"),
                    int(result.get("width") or 1200),
                    int(result.get("height") or 800),
                ))
            if result.get("featured") and result.get("media_id"):
                featured = FeaturedProgress(
                    FeaturedStatus.VALID,
                    int(result["media_id"]),
                    str(result.get("media_url") or ""),
                )
        return MediaProgress(
            required=total_required,
            inline=tuple({item.media_id: item for item in inline}.values()),
            featured=featured,
        )

    @staticmethod
    def _has_provenance(candidate: dict[str, Any]) -> bool:
        return all(candidate.get(key) for key in (
            "source_page_url", "direct_image_url", "author", "license",
            "license_url", "captured_at", "credit_text", "alt_text",
        ))

    @classmethod
    def _is_approved(cls, candidate: dict[str, Any]) -> bool:
        return (
            (candidate.get("evidence") or {}).get("verdict") == "deterministic_match"
            and cls._has_provenance(candidate)
        )

    @classmethod
    def _approved_unique(cls, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in candidates:
            candidate = _normalize_executable_candidate(candidate, str(candidate.get("subject") or ""))
            key = str(candidate.get("media_library_id") or candidate.get("direct_image_url") or "")
            if key and key not in seen and cls._is_approved(candidate):
                seen.add(key)
                result.append(candidate)
        return result

    @staticmethod
    def _unique_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate.get("media_library_id") or candidate.get("direct_image_url") or "")
            if key and key not in seen:
                seen.add(key)
                result.append(candidate)
        return result

    def _resolve_ambiguous_vision(self, candidates: list[dict[str, Any]]) -> None:
        vision_candidates = []
        for index, candidate in enumerate(candidates):
            url = candidate.get("direct_image_url") or candidate.get("image_url")
            verdict = (candidate.get("evidence") or {}).get("verdict")
            if url and verdict in {"ambiguous", "needs_vision", "inconclusive"}:
                vision_candidates.append({
                    "candidate_id": str(candidate.get("candidate_id") or f"v2-{index}"),
                    "image_url": url,
                    "subject": str(candidate.get("subject") or ""),
                    "require_key_art": bool(candidate.get("role") == "featured"),
                })
        if not vision_candidates or not getattr(self.config, "vision_enabled", False) or not getattr(self.config, "vision_api_key", ""):
            return
        from ..media.vision_gate import verify_image_subject_batch
        from ..observability import append_telemetry

        budget = max(0, int(getattr(self.config, "vision_max_low", 0)))
        low_items = vision_candidates[:budget]
        skipped = max(0, len(vision_candidates) - len(low_items))
        if skipped:
            append_telemetry(
                self.root,
                "vision_budget_exhausted",
                candidates=len(vision_candidates),
                low_budget=budget,
                skipped=skipped,
            )
        if not low_items:
            return

        low_decisions: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(low_items), 20):
            low_decisions.update(verify_image_subject_batch(
                items=low_items[offset:offset + 20],
                api_key=self.config.vision_api_key,
                base_url=self.config.vision_base_url,
                model=self.config.vision_model,
                timeout=self.config.http_timeout,
                detail=self.config.vision_detail,
                allow_high=False,
                root=self.root,
            ))
        high_items = [
            item for item in low_items
            if item.get("require_key_art")
            and low_decisions.get(item["candidate_id"], {}).get("verdict") == "inconclusive"
        ]
        high_decisions: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(high_items), 20):
            high_decisions.update(verify_image_subject_batch(
                items=high_items[offset:offset + 20],
                api_key=self.config.vision_api_key,
                base_url=self.config.vision_base_url,
                model=self.config.vision_model,
                timeout=self.config.http_timeout,
                detail="high",
                allow_high=False,
                root=self.root,
            ))
        from ..media.vision_cache import set_cached_decision
        by_url = {str(item["image_url"]): item for item in low_items}
        for candidate in candidates:
            url = str(candidate.get("direct_image_url") or "")
            item = by_url.get(url)
            decision = low_decisions.get(item["candidate_id"]) if item else None
            if item and item["candidate_id"] in high_decisions:
                decision = high_decisions[item["candidate_id"]]
            if decision and decision.get("verdict") == "accept":
                set_cached_decision(
                    self.root,
                    url,
                    str(candidate.get("subject") or ""),
                    {"status": "MATCH", "confidence": float(decision.get("confidence") or 0), "visual_type": decision.get("visual_type") or "other"},
                )
                candidate.setdefault("evidence", {})["verdict"] = "deterministic_match"
                candidate["needs_vision"] = False

    @staticmethod
    def _plan_item(candidate: dict[str, Any], subject: str, slot: int, featured: bool) -> dict[str, Any]:
        return {
            **candidate,
            "paragraph_index": slot,
            "is_featured": featured,
            "alt_text": candidate.get("alt_text") or subject,
            "credit_text": candidate.get("credit_text") or f"Crédito da imagem: {subject}",
            "width": 1200,
            "height": 800,
        }


class WordPressWriterV2:
    def __init__(self, client, root: Path, *, policy_version: int = 1):
        self.client, self.root, self.policy_version = client, Path(root), policy_version

    def commit(self, post_id: int, context: dict[str, Any], proposed_state, outcome):
        journal_dir = self.root / "work" / "v2-journal"
        journal_dir.mkdir(parents=True, exist_ok=True)
        candidate_path = self.root / "backups" / str(post_id) / "editorial.candidate.json"
        candidate = json.loads(candidate_path.read_text(encoding="utf-8")) if candidate_path.is_file() else {}
        content = str(candidate.get("content") or "")
        seo = candidate.get("seo") or {}
        post = context.get("post") or self.client.get_post(post_id)
        meta = dict(post.get("meta") or {})
        state_json = json.dumps(proposed_state.to_dict(), ensure_ascii=False, separators=(",", ":"))
        payload_meta: dict[str, Any] = {"_hermes_work_state": state_json}
        ready_hash = ""
        if outcome.type.value == "ready" and content:
            seo_meta = build_meta({key: str(seo.get(key) or "") for key in ("title", "meta_description", "focus_keyword")}, existing=meta)
            meta.update(seo_meta)
            manifest = build_ready_manifest(post_id=post_id, content=content, featured_media=candidate.get("featured_media"), seo=seo, original_link=context.get("original_link"), editorial=candidate.get("editorial"), policy_version=self.policy_version)
            ready_hash = manifest_hash(manifest)
            payload_meta.update({"_hermes_ready_manifest": serialize_manifest(manifest), "_hermes_ready_hash": ready_hash})
            payload_meta.update(build_state_markers(STATE_READY, ready_hash=ready_hash, policy_version=self.policy_version))
            latest = self.root / "backups" / str(post_id) / "editorial.latest.json"
            latest.parent.mkdir(parents=True, exist_ok=True)
            latest.write_text(json.dumps(candidate.get("editorial") or {}, ensure_ascii=False, indent=2), encoding="utf-8")
        journal = journal_dir / f"{post_id}.json"
        intent = {"status": "prepared", "post_id": post_id, "state": proposed_state.to_dict(), "ready_hash": ready_hash, "candidate_hash": hashlib.sha256(content.encode()).hexdigest(), "detail": outcome.detail or context.get("provider_reason")}
        journal.write_text(json.dumps(intent, ensure_ascii=False, indent=2), encoding="utf-8")
        update: dict[str, Any] = {"meta": {**meta, **payload_meta}}
        if outcome.type.value == "ready" and content:
            update["content"] = {"raw": content}
            update["featured_media"] = candidate.get("featured_media")
        current = post.get("content") or {}
        if update.get("content", {}).get("raw") == current.get("raw") and meta.get("_hermes_work_state") == state_json:
            readback = post
            changed = False
        else:
            journal.write_text(
                json.dumps({**intent, "status": "committing"}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.client.update_post(post_id, update)
            readback = self.client.get_post(post_id)
            changed = True
        if not isinstance(readback, dict):
            raise RuntimeError("WordPress read-back missing")
        readback_meta = readback.get("meta") or {}
        if readback_meta.get("_hermes_work_state") != state_json:
            raise RuntimeError("V2 state read-back mismatch")
        if outcome.type.value == "ready":
            if (readback.get("content") or {}).get("raw") != content:
                raise RuntimeError("candidate content read-back mismatch")
            if readback_meta.get("_hermes_ready_hash") != ready_hash:
                raise RuntimeError("ready hash read-back mismatch")
        journal.write_text(json.dumps({**intent, "status": "committed", "readback": True}, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"wordpress_changed": changed, "readback": True, "ready_hash": ready_hash}


def run_v2(client, config, root: Path, *, limit: int = 1) -> dict[str, Any]:
    if limit < 1:
        raise ValueError("limit must be positive")
    lock = RunSessionLock(Path(root) / "work" / "v2-run.lock")
    if not lock.acquire():
        return {"selected": 0, "locked": True, "details": []}
    try:
        reader = ProductionCandidateReader(client, root)
        snapshot = reader.snapshot(page_size=max(10, limit))
        now = datetime.now(timezone.utc)
        candidates = [
            (post_id, context) for post_id, context in snapshot
            if context["v2_state"].state.value == "pending"
            and _cooldown_expired(context["v2_state"].retry.next_at, now)
        ]

        class _SnapshotStore:
            def __init__(self, items):
                self.states = {post_id: context["v2_state"] for post_id, context in items}

            def load(self, post_id):
                return self.states[post_id]

        selected = select(candidates, _SnapshotStore(candidates), limit=limit)
        pending = [
            context for _post_id, context in snapshot
            if context["v2_state"].state.value == "pending"
        ]
        eligible_ids = {post_id for post_id, _context in candidates}
        cooldown = [
            (post_id, context) for post_id, context in snapshot
            if context["v2_state"].state.value == "pending"
            and post_id not in eligible_ids
        ]
        blockers: dict[str, int] = {}
        for context in pending:
            blocker = context["v2_state"].blocker.value if context["v2_state"].blocker else "none"
            blockers[blocker] = blockers.get(blocker, 0) + 1
        queue = {
            "scanned": len(snapshot),
            "pending": len(pending),
            "eligible": len(candidates),
            "cooldown": len(cooldown),
            "by_blocker": blockers,
            "next_eligible_at": min(
                (
                    context["v2_state"].retry.next_at
                    for _post_id, context in cooldown
                    if context["v2_state"].retry.next_at
                ),
                default=None,
            ),
            "no_progress": {
                str(post_id): context["v2_state"].retry.no_progress
                for post_id, context in snapshot
                if context["v2_state"].retry.no_progress
            },
            "media": {
                str(post_id): {
                    "required": context["v2_state"].media.required,
                    "accepted": context["v2_state"].media.accepted,
                    "missing": context["v2_state"].media.missing,
                    "featured": context["v2_state"].media.featured.status.value,
                }
                for post_id, context in snapshot
                if context["v2_state"].state.value == "pending"
            },
        }
        details = []
        completed = 0
        failed = 0
        stages = {
            "editorial": ProductionEditorialStage(client, config, root),
            "media": ProductionMediaStage(
                client, config, root,
                resolver=ProductionMediaResolver(client, config, root),
            ),
            "compose": ProductionComposeStage(config, root),
            "validate": ProductionValidateStage(client, config, root),
        }
        for post_id, context in selected:
            initial = context["v2_state"]
            buffered = BufferedStateStore(initial)
            try:
                # Mark the transaction before any provider/media work. If the
                # process dies after an upload, recovery must not mistake the
                # previous committed journal for a completed run.
                _write_v2_journal_checkpoint(root, post_id, "running", initial)
                outcome = PipelineRunner(buffered, stages, config=config).run_one(post_id, context)
                _write_v2_media_checkpoint(root, post_id, buffered.state.media)
                _write_v2_journal_checkpoint(
                    root, post_id, "validated", buffered.state
                )
                error_path = root / "backups" / str(post_id) / "editorial.error.json"
                if outcome.blocker is not None and outcome.blocker.value == "provider_error" and error_path.is_file():
                    try:
                        context["provider_reason"] = json.loads(error_path.read_text(encoding="utf-8")).get("reason")
                    except (OSError, ValueError):
                        context["provider_reason"] = "provider_error"
                write = WordPressWriterV2(
                    client, root, policy_version=config.policy_version
                ).commit(post_id, context, buffered.state, outcome)
                completed += 1
                details.append({
                    "post_id": post_id,
                    "initial_state": initial.state.value,
                    "phase": buffered.state.phase.value,
                    "outcome": outcome.type.value,
                    "blocker": outcome.blocker.value if outcome.blocker else None,
                    "detail": outcome.detail or context.get("provider_reason"),
                    **write,
                })
            except Exception as exc:  # noqa: BLE001 - isolate one post
                failed += 1
                details.append({
                    "post_id": post_id,
                    "initial_state": initial.state.value,
                    "outcome": "error",
                    "blocker": "internal_error",
                    "detail": str(exc)[:400],
                })
                continue
        return {
            "selected": len(selected),
            "completed": completed,
            "failed": failed,
            "locked": False,
            "queue": queue,
            "details": details,
        }
    finally:
        lock.release()
