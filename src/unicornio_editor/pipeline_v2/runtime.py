"""Minimal production bridge: buffered runner, writer, journal and v2-run."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..checklist import required_image_count
from ..content_quality import word_count
from ..manifest import build_ready_manifest, manifest_hash, serialize_manifest
from ..media.evidence import post_subjects
from ..seo.rank_math import build_meta
from ..workflow import _execute_media_plan, validate_media_plan
from .lock import RunSessionLock
from .model import FeaturedProgress, FeaturedStatus, InlineMedia, MediaProgress
from .production import ProductionCandidateReader
from .production_stages import ProductionComposeStage, ProductionEditorialStage, ProductionMediaStage, ProductionValidateStage
from .runner import PipelineRunner
from .scheduler import select


class BufferedStateStore:
    def __init__(self, initial):
        self.state = initial

    def load(self, post_id: int):
        return self.state

    def commit(self, post_id: int, state):
        self.state = state


class ProductionMediaResolver:
    """Reuse the existing media-resolve core and execute only the deficit."""
    def __init__(self, client, config, root: Path):
        self.client, self.config, self.root = client, config, Path(root)

    def __call__(self, context, state, editorial, previous):
        html = str(editorial.get("cleaned_html") or "")
        total_required = required_image_count(word_count(html), title=str(context.get("title") or ""), content=html)
        needed = max(0, total_required - previous.accepted)
        if needed == 0 and previous.featured.status is FeaturedStatus.VALID:
            return previous
        from ..cli import _resolve_media_batch
        subject_rows = post_subjects(title=str(context.get("title") or ""), content_html=html, focus_keyword=str((editorial.get("seo") or {}).get("focus_keyword") or ""), game_name=editorial.get("game_name"))
        subject = str((subject_rows[0] if subject_rows else {}).get("subject") or context.get("title") or "")
        search_needed = needed + (0 if previous.featured.status is FeaturedStatus.VALID else 1)
        batch = {"schema_version": 1, "batch_id": f"v2-{context['post_id']}", "posts": [{"post_id": int(context["post_id"]), "subject": subject, "query": subject, "needed": search_needed, "limit": max(search_needed, 1), "engine": "auto", "size": "xga", "ratio": "w"}]}
        resolved = _resolve_media_batch(self.client, self.config, self.root, batch, full=True)
        row = (resolved.get("posts") or [{}])[0]
        plan = []
        approved = list(row.get("reuse") or [])
        approved.extend(candidate for candidate in (row.get("audit_candidates") or []) if (candidate.get("evidence") or {}).get("verdict") == "deterministic_match")
        for index, candidate in enumerate(approved):
            if len(plan) >= search_needed:
                break
            if not candidate.get("direct_image_url"):
                continue
            is_featured = previous.featured.status is not FeaturedStatus.VALID and len(plan) == 0
            slot = 0 if is_featured else (max(0, len(plan) - (1 if previous.featured.status is not FeaturedStatus.VALID else 0)) * 3)
            plan.append({**candidate, "paragraph_index": slot, "is_featured": is_featured, "alt_text": candidate.get("alt_text") or subject, "credit_text": candidate.get("credit_text") or f"Crédito da imagem: {subject}", "width": 1200, "height": 800})
        checked = validate_media_plan(self.client, {**editorial, "media_plan": plan}, config=self.config, root=self.root, post_title=str(context.get("title") or ""))
        results, featured_id, featured_credit = _execute_media_plan({**editorial, "media_plan": plan}, self.config, self.client, self.root, preflight=checked, post_id=int(context["post_id"]))
        inline = list(previous.inline)
        featured = previous.featured
        for row in results:
            if row.get("media_id") and row.get("media_url") and not row.get("featured"):
                inline.append(InlineMedia(int(row["media_id"]), str(row["media_url"]), int(row.get("paragraph_index", 0)), str(row.get("alt_text", "")), str(row.get("credit_text", ""))))
            if row.get("featured") and row.get("media_id"):
                featured = FeaturedProgress(FeaturedStatus.VALID, int(row["media_id"]), str(row.get("media_url") or ""))
        total_required = required_image_count(word_count(html), title=str(context.get("title") or ""), content=html)
        return MediaProgress(required=max(previous.required, total_required), inline=tuple({item.media_id: item for item in inline}.values()), featured=featured)


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
            draft = self.root / "backups" / str(post_id) / "editorial.draft.json"
            latest = self.root / "backups" / str(post_id) / "editorial.latest.json"
            if draft.is_file(): latest.write_text(draft.read_text(encoding="utf-8"), encoding="utf-8")
        journal = journal_dir / f"{post_id}.json"
        intent = {"status": "prepared", "post_id": post_id, "state": proposed_state.to_dict(), "ready_hash": ready_hash, "candidate_hash": hashlib.sha256(content.encode()).hexdigest(), "detail": context.get("provider_reason")}
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
        candidates = reader.read(page_size=max(10, limit))
        selected = select(candidates, reader.state_store, limit=limit)
        details = []
        for post_id, context in selected:
            initial = context["v2_state"]
            buffered = BufferedStateStore(initial)
            stages = {"editorial": ProductionEditorialStage(client, config, root), "media": ProductionMediaStage(client, config, root, resolver=ProductionMediaResolver(client, config, root)), "compose": ProductionComposeStage(config, root), "validate": ProductionValidateStage(client, config, root)}
            outcome = PipelineRunner(buffered, stages).run_one(post_id, context)
            error_path = root / "backups" / str(post_id) / "editorial.error.json"
            if outcome.blocker is not None and outcome.blocker.value == "provider_error" and error_path.is_file():
                try:
                    context["provider_reason"] = json.loads(error_path.read_text(encoding="utf-8")).get("reason")
                except (OSError, ValueError):
                    context["provider_reason"] = "provider_error"
            write = WordPressWriterV2(client, root, policy_version=config.policy_version).commit(post_id, context, buffered.state, outcome)
            details.append({"post_id": post_id, "initial_state": initial.state.value, "phase": buffered.state.phase.value, "outcome": outcome.type.value, "blocker": outcome.blocker.value if outcome.blocker else None, "detail": context.get("provider_reason"), **write})
        return {"selected": len(selected), "locked": False, "details": details}
    finally:
        lock.release()
