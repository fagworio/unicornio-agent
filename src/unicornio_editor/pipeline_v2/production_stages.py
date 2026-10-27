"""Production stage adapters for the V2 runner.

These adapters deliberately reuse the mature V1 functions.  They do not call
``apply_editorial``; orchestration and lifecycle decisions remain in V2.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from ..batch import BatchError, load_editorial_batch, prepare_batch, wordpress_text
from ..builder import strip_canonical_footer
from ..checklist import run_pre_publish_checklist
# Preserve the module-level seam used by older integrations while all stage
# calculations use the canonical HTML-aware helper.
from ..checklist import required_image_count, required_image_count_for_content
from ..content_quality import _PT_STOPWORDS, _keyword_in_text, keyword_occurs_naturally, normalize_editorial_dashes
from ..editorial_provider import EditorialProviderError, generate_editorial_batch
from ..editorial_schema import validate_editorial
from ..language import editorial_language_report
from ..html_cleaner import normalize_h1_headings
from ..list_quality import detect_list_format
from ..media.evidence import post_subjects
from ..media.inserter import (
    MediaInsertionError,
    insert_media,
    minimum_normal_article_paragraphs,
    normal_media_urls_to_insert,
    normalize_normal_article_paragraphs,
    plan_normal_media_insertions,
    remove_empty_paragraphs,
    remove_media_urls,
    select_normal_media_slots,
)
from ..media.vision_gate import VisionGateError
from ..wordpress import WordPressError
from ..workflow import (
    _execute_media_plan,
    _seo_keyword_candidates,
    compose_final_content,
    attach_trailer_audit,
    resolve_editorial_defaults,
    validate_media_plan,)
from .errors import StageError
from .production import editorial_has_substantive_text
from .media_accounting import reconcile_working_visual_media
from .model import BlockerCode, FeaturedProgress, FeaturedStatus, InlineMedia, MediaProgress, MediaSearchProgress, Phase
from .model import WorkState
from .media_layout import (
    build_normal_article_layout_policy,
    editorial_body_sha256,
    validate_normal_article_layout_policy,
)
from ..observability import _redact_text, record_unexpected_error


def _write_json(root: Path, post_id: int, name: str, value: dict[str, Any]) -> Path:
    target = root / "backups" / str(post_id) / name
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)
    return target


def _prepare_editorial_media_structure(
    editorial: dict[str, Any],
    state: Any,
    *,
    title: str,
    categories: Any = (),
) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    """Prepare the exact editorial body used by media preflight and compose.

    Listicle item structure is preserved. Normal articles may be split only at
    safe sentence boundaries; the helper's text-preservation assertion remains
    authoritative. A failed capacity check is returned to the caller as an
    editorial rework reason before any media provider or upload is invoked.
    """
    working = dict(editorial)
    original_html = normalize_h1_headings(
        str(working.get("cleaned_html") or ""), post_title=title,
    )
    body_without_footer = strip_canonical_footer(original_html)
    empty_result = remove_empty_paragraphs(body_without_footer)
    body = str(empty_result["html"])
    audit: dict[str, Any] = {
        "version": 2,
        "status": "prepared",
        "canonical_footer_stripped": body_without_footer != original_html,
        "empty_paragraphs_removed": int(empty_result["audit"]["removed"]),
        "paragraphs_before": len(re.findall(r"<p\b[^>]*>.*?</p>", original_html, re.IGNORECASE | re.DOTALL)),
        "paragraphs_after_cleanup": len(re.findall(r"<p\b[^>]*>.*?</p>", body, re.IGNORECASE | re.DOTALL)),
    }
    decision = str(
        working.get("decision")
        or (working.get("site_relevance") or {}).get("decision")
        or ""
    ).casefold()
    if decision in {"skip", "uncertain"}:
        working["cleaned_html"] = body
        audit.update({
            "status": "not_applicable",
            "layout": "not_applicable",
            "decision": decision,
            "prepared_body_sha256": editorial_body_sha256(body),
            "paragraphs_after": audit["paragraphs_after_cleanup"],
        })
        audit["structure_sha256"] = _editorial_structure_sha256(body, audit)
        return working, audit, None

    categories = working.get("categories") or categories or ()
    listicle_subjects = post_subjects(
        title=title,
        content_html=body,
        focus_keyword=str((working.get("seo") or {}).get("focus_keyword") or ""),
        game_name=working.get("game_name"),
        categories=categories,
    )
    if detect_list_format(title, body, categories) is not None or len(listicle_subjects) > 1:
        working["cleaned_html"] = body
        audit.update({
            "layout": "listicle_preserved",
            "prepared_body_sha256": editorial_body_sha256(body),
            "paragraphs_after": audit["paragraphs_after_cleanup"],
        })
        audit["structure_sha256"] = _editorial_structure_sha256(body, audit)
        return working, audit, None

    audit["layout"] = "normal_article"
    required = required_image_count_for_content(body, title=title)
    state_media = getattr(state, "media", None)
    reconciliation = reconcile_working_visual_media(
        state_media,
        required_inline=required,
        featured=getattr(state_media, "featured", None),
    )
    featured = getattr(state_media, "featured", None)
    featured_url = (
        str(getattr(featured, "media_url", "") or "")
        if getattr(featured, "status", None) is FeaturedStatus.VALID
        else ""
    )
    eligible_inline = tuple(reconciliation.eligible_inline)

    def planned_urls(source_body: str, target: int) -> tuple[list[str], list[str]]:
        from ..media.relevance import iter_content_images

        embedded = {
            _canonical_media_ref(str(row.get("src") or ""))
            for row in iter_content_images(source_body)
            if str(row.get("src") or "").strip()
        }
        present = [
            item for item in eligible_inline
            if _canonical_media_ref(str(getattr(item, "media_url", "") or "")) in embedded
        ]
        selected: list[Any] = list(present)
        remaining = max(0, target - len(selected))
        present_ids = {int(getattr(item, "media_id", 0) or 0) for item in present}
        for item in eligible_inline:
            if remaining <= 0:
                break
            if int(getattr(item, "media_id", 0) or 0) in present_ids:
                continue
            selected.append(item)
            present_ids.add(int(getattr(item, "media_id", 0) or 0))
            remaining -= 1
        urls = [str(getattr(item, "media_url", "") or "") for item in selected]
        urls.extend(f"__v2_pending_media_{index}__" for index in range(remaining))
        return urls, [str(getattr(item, "media_url", "") or "") for item in selected]

    ideal_urls, _ = planned_urls(body, required)
    initial_insertions = normal_media_urls_to_insert(body, ideal_urls, featured_url)
    normalization_audit: dict[str, Any] = {
        "original_paragraphs": len(re.findall(r"<p\b[^>]*>.*?</p>", body, re.IGNORECASE | re.DOTALL)),
        "final_paragraphs": len(re.findall(r"<p\b[^>]*>.*?</p>", body, re.IGNORECASE | re.DOTALL)),
        "changed": False,
        "reason": None,
        "splits": [],
    }
    if initial_insertions:
        from ..media.inserter import normal_media_slot_capacity

        layout = normal_media_slot_capacity(body)
        target_paragraphs = minimum_normal_article_paragraphs(
            len(initial_insertions),
            allow_after_body=bool(layout.get("body_end_available")),
        )
        normalization = normalize_normal_article_paragraphs(
            body,
            required_paragraphs=target_paragraphs,
        )
        body = str(normalization["html"])
        normalization_audit = dict(normalization["audit"])

    featured_state = getattr(getattr(state, "media", None), "featured", None)
    featured_media_id = int(getattr(featured_state, "media_id", 0) or 0) or None
    layout_policy = build_normal_article_layout_policy(
        body,
        target_ideal=required,
        verified_inline=eligible_inline,
        featured_media_id=featured_media_id,
        featured_repeat_evidence=featured_state,
    )
    target_effective = (
        int(layout_policy["target_effective"])
        if layout_policy is not None else required
    )
    effective_urls, selected_urls = planned_urls(body, target_effective)
    insertions = normal_media_urls_to_insert(body, effective_urls, featured_url)
    audit.update({
        "required_inline": required,
        "target_ideal": required,
        "capacity_verified": (
            int(layout_policy["capacity_verified"])
            if layout_policy is not None else None
        ),
        "target_effective": target_effective,
        "reduction_reason": (
            str(layout_policy.get("reduction_reason") or "")
            if layout_policy is not None else ""
        ),
        "valid_existing_inline": reconciliation.accounting.valid_distinct_inline_count,
        "verified_inline_in_body": (
            int(layout_policy["verified_inline_in_body"])
            if layout_policy is not None else 0
        ),
        "selected_verified_inline": len(selected_urls),
        "media_positions_needed": len(insertions),
        "paragraph_normalization": normalization_audit,
    })
    if layout_policy is not None:
        audit["media_layout_policy"] = layout_policy

    if insertions:
        try:
            plan_normal_media_insertions(body, [{"media_url": url} for url in insertions])
        except MediaInsertionError as exc:
            audit["status"] = "rework_required"
            audit["failure_code"] = exc.code
            audit["failure"] = str(exc)
            audit["paragraphs_after"] = len(re.findall(r"<p\b[^>]*>.*?</p>", body, re.IGNORECASE | re.DOTALL))
            working["cleaned_html"] = body
            return working, audit, str(exc)

    working["cleaned_html"] = body
    audit["prepared_body_sha256"] = editorial_body_sha256(body)
    audit["paragraphs_after"] = len(re.findall(r"<p\b[^>]*>.*?</p>", body, re.IGNORECASE | re.DOTALL))
    audit["structure_sha256"] = _editorial_structure_sha256(body, audit)
    return working, audit, None


def _editorial_structure_sha256(body: str, audit: dict[str, Any]) -> str:
    from ..media.inserter import normal_media_slot_capacity

    layout_facts: dict[str, Any] = {"layout": str(audit.get("layout") or "")}
    if audit.get("layout") == "normal_article":
        layout = normal_media_slot_capacity(body)
        layout_facts.update({
            key: layout.get(key)
            for key in (
                "verified", "paragraph_count", "substantive_paragraphs", "first_slot",
                "last_slot", "body_end_available", "existing_media_slots", "capacity_slots",
                "minimum_spacing",
            )
        })
    policy = audit.get("media_layout_policy")
    payload = {
        "body_sha256": editorial_body_sha256(body),
        "layout_facts": layout_facts,
        "layout_policy_sha256": (
            str(policy.get("layout_sha256") or "") if isinstance(policy, dict) else ""
        ),
        "empty_paragraphs_removed": int(audit.get("empty_paragraphs_removed") or 0),
        "paragraph_normalization": audit.get("paragraph_normalization") or {},
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _ensure_prepared_media_structure(
    root: Path,
    post_id: int,
    editorial: dict[str, Any],
    state: Any,
    *,
    title: str,
    categories: Any = (),
    persist: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Reconcile legacy/resumed drafts with the exact prepared-body contract."""
    if str(editorial.get("cleaned_html") or ""):
        editorial["cleaned_html"] = normalize_editorial_dashes(
            str(editorial.get("cleaned_html") or "")
        )
    structure_path = Path(root) / "backups" / str(post_id) / "editorial.structure.json"
    body = str(editorial.get("cleaned_html") or "")
    body_hash = editorial_body_sha256(body)
    try:
        audit = _json_object(structure_path) if structure_path.is_file() else {}
    except BatchError:
        audit = {}
    current = (
        audit.get("version") == 2
        and audit.get("status") in {"prepared", "not_applicable"}
        and audit.get("prepared_body_sha256") == body_hash
        and audit.get("structure_sha256") == _editorial_structure_sha256(body, audit)
    )
    if not current:
        prepared, audit, error = _prepare_editorial_media_structure(
            editorial,
            state,
            title=title,
            categories=categories,
        )
        if persist:
            _write_json(Path(root), post_id, "editorial.structure.json", audit)
        if error:
            if persist and str(prepared.get("cleaned_html") or "").strip():
                _write_json(Path(root), post_id, "editorial.draft.json", prepared)
            raise StageError(
                BlockerCode.STRUCTURE,
                Phase.EDITORIAL,
                f"media_structure:{audit.get('failure_code') or 'invalid'}: {error}",
            )
        editorial.clear()
        editorial.update(prepared)
        if persist:
            _write_json(Path(root), post_id, "editorial.draft.json", prepared)

    policy = audit.get("media_layout_policy")
    if policy is not None:
        ideal = required_image_count_for_content(
            str(editorial.get("cleaned_html") or ""),
            title=title,
        )
        validation = validate_normal_article_layout_policy(
            str(editorial.get("cleaned_html") or ""),
            policy,
            target_ideal=ideal,
            media=getattr(state, "media", None),
            listicle=audit.get("layout") == "listicle_preserved",
        )
        if not validation.get("valid"):
            raise StageError(
                BlockerCode.STRUCTURE,
                Phase.EDITORIAL,
                f"media_layout_policy:{validation.get('reason') or 'invalid'}",
            )
        return editorial, audit, dict(policy)
    return editorial, audit, {}


def _canonical_media_ref(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return str(value or "").strip().split("?", 1)[0]
    path = re.sub(r"-\d+x\d+(?=\.[^./]+$)", "", parsed.path)
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), path, "", ""))


def _visual_reconcile_urls(root: Path, post_id: int, media: MediaProgress) -> list[str]:
    """Explicit URLs invalidated by a completed local visual reconciliation."""
    path = Path(root) / "work" / "v2-visual-reconcile" / f"{post_id}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(value, dict) or value.get("status") != "confirmed":
        return []
    proposed = value.get("proposed")
    proposed_media = proposed.get("media") if isinstance(proposed, dict) else None
    # Do not consume a prepared/stale journal. The current state must retain
    # the reconciled media shape (it may have gained new valid inline assets).
    if not isinstance(proposed_media, dict):
        return []
    expected_ids = {
        int(item.get("media_id"))
        for item in ((proposed_media.get("inline") or {}).get("accepted") or [])
        if isinstance(item, dict) and item.get("media_id") is not None
    }
    current_ids = {item.media_id for item in media.inline}
    invalid_ids = {
        int(row.get("media_id"))
        for row in (value.get("duplicates") or [])
        if isinstance(row, dict) and row.get("media_id") is not None
    }
    if not expected_ids.issubset(current_ids) or invalid_ids.intersection(current_ids):
        return []
    rows = value.get("duplicates")
    return [str(row.get("media_url") or "") for row in rows if isinstance(row, dict) and row.get("media_url")]


def _persist_draft_repairs(
    root: Path,
    post_id: int,
    *,
    focus_keyword: str | None = None,
    normalize_dashes: bool = False,
) -> None:
    """Persist deterministic repairs without copying runtime enrichments.

    ``editorial.candidate.json`` contains inserted media, CTA, links and
    trailer data.  It is a derived artifact, so only the repaired fields are
    merged into the canonical editorial draft used by the next retry.
    """
    path = Path(root) / "backups" / str(post_id) / "editorial.draft.json"
    if not path.is_file():
        return
    try:
        draft = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(draft, dict):
        return
    if normalize_dashes:
        draft["cleaned_html"] = normalize_editorial_dashes(draft.get("cleaned_html", ""))
    if focus_keyword:
        seo = dict(draft.get("seo") or {})
        seo["focus_keyword"] = focus_keyword
        draft["seo"] = seo
    _write_json(Path(root), post_id, "editorial.draft.json", draft)


def _post(context: dict[str, Any]) -> dict[str, Any]:
    post = context.get("post")
    if not isinstance(post, dict):
        raise StageError(BlockerCode.MANIFEST_INVALID, Phase.EDITORIAL, "context post is missing")
    return post


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BatchError(f"JSON invalido em {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BatchError(f"JSON em {path} precisa ser objeto")
    return value


def _repair_focus_keyword_draft(
    editorial: dict[str, Any],
    post: dict[str, Any],
) -> dict[str, Any] | None:
    """Repair a keyword mismatch using only phrases already in the article."""
    seo = dict(editorial.get("seo") or {})
    title = str(seo.get("title") or editorial.get("title") or wordpress_text(post.get("title"), field="title"))
    content = str(editorial.get("cleaned_html") or "")
    current = str(seo.get("focus_keyword") or "").strip()
    if current and keyword_occurs_naturally(current, title, content):
        return None
    subjects = post_subjects(
        title=title,
        content_html=content,
        focus_keyword=current,
        game_name=editorial.get("game_name"),
    )
    candidates = [
        str(row.get("subject") or "").strip()
        for row in subjects
        if isinstance(row, dict)
    ]
    candidates.extend(
        _seo_keyword_candidates(title, game_name=editorial.get("game_name"))
    )
    replacement = next(
        (
            candidate
            for candidate in candidates
            if candidate and candidate.casefold() != current.casefold()
            and keyword_occurs_naturally(candidate, title, content)
        ),
        "",
    )
    if not replacement:
        return None
    repaired = dict(editorial)
    repaired["seo"] = {**seo, "title": title, "focus_keyword": replacement}
    repaired.pop("decision", None)
    return repaired


class ProductionEditorialStage:
    def __init__(self, client: Any, config: Any, root: Path):
        self.client, self.config, self.root = client, config, Path(root)

    def __call__(self, context: dict[str, Any], state: Any) -> dict[str, Any]:
        post_id = int(context.get("post_id") or _post(context)["id"])
        try:
            prepared = prepare_batch(self.client, self.config, self.root, [post_id])
            if prepared.get("prepared") != 1:
                raise StageError(BlockerCode.MANIFEST_INVALID, Phase.EDITORIAL, "post preparation failed")
            input_path = Path(prepared["editorial_input"])
            input_payload = _json_object(input_path)
            posts = input_payload.get("posts")
            if not isinstance(posts, list) or not posts or not all(isinstance(item, dict) for item in posts):
                raise BatchError(f"posts invalido em {input_path}")
            input_post = next(
                (item for item in posts
                 if int(item.get("post_id") or 0) == post_id),
                {},
            )
            if not input_post:
                raise BatchError(f"post_id {post_id} ausente em {input_path}")
            post_title = wordpress_text(input_post.get("title"), field="title")
            localization = input_post.get("localization") if isinstance(input_post, dict) else {}
            must_localize = bool(isinstance(localization, dict) and localization.get("required"))
            draft_path = self.root / "backups" / str(post_id) / "editorial.draft.json"
            previous_editorial = _json_object(draft_path) if draft_path.is_file() else None
            if not editorial_has_substantive_text(previous_editorial):
                recovered_editorial = context.get("editorial")
                if editorial_has_substantive_text(recovered_editorial):
                    if isinstance(previous_editorial, dict):
                        _write_json(self.root, post_id, "editorial.draft.invalid.json", previous_editorial)
                    previous_editorial = dict(recovered_editorial)
                    recovery = context.get("editorial_recovery")
                    if isinstance(recovery, dict):
                        _write_json(self.root, post_id, "editorial.recovery.json", recovery)
                    _write_json(self.root, post_id, "editorial.draft.json", previous_editorial)
            if (
                not must_localize
                and
                state.phase is Phase.EDITORIAL
                and state.blocker is BlockerCode.TEXT_QUALITY
                and isinstance(previous_editorial, dict)
            ):
                repaired = _repair_focus_keyword_draft(previous_editorial, _post(context))
                if repaired is not None:
                    repaired["cleaned_html"] = normalize_h1_headings(
                        normalize_editorial_dashes(repaired.get("cleaned_html", "")),
                        post_title=post_title,
                    )
                    repaired, structure_audit, structure_error = _prepare_editorial_media_structure(
                        repaired,
                        state,
                        title=post_title,
                        categories=context.get("categories") or (),
                    )
                    _write_json(self.root, post_id, "editorial.structure.json", structure_audit)
                    if structure_error:
                        # Never replace a usable draft with an empty structure
                        # artifact; an empty body causes a false media-structure
                        # failure on the next retry and destroys recovery input.
                        if str(repaired.get("cleaned_html") or "").strip():
                            _write_json(self.root, post_id, "editorial.draft.json", repaired)
                        raise StageError(
                            BlockerCode.STRUCTURE,
                            Phase.EDITORIAL,
                            f"media_structure:insufficient_paragraph_slots: {structure_error}",
                        )
                    _write_json(self.root, post_id, "editorial.draft.json", repaired)
                    return repaired
            validation_path = self.root / "backups" / str(post_id) / "editorial.validation.json"
            structure_path = self.root / "backups" / str(post_id) / "editorial.structure.json"
            structure_audit = _json_object(structure_path) if structure_path.is_file() else {}
            has_structure_failure = (
                state.blocker is BlockerCode.STRUCTURE
                and structure_audit.get("status") == "rework_required"
            )
            if state.phase is Phase.EDITORIAL and (validation_path.is_file() or has_structure_failure):
                validation = _json_object(validation_path) if validation_path.is_file() else {}
                editorial_gates = {"qualidade_texto", "seo", "estrutura", "fonte", "schema_editorial", "conteudo_nao_vazio", "conteudo_sem_metadados_operacionais", "estrutura_lista", "cta_canonico", "html_semantics"}
                failures = [failure for failure in validation.get("failures", []) if failure.get("gate") in editorial_gates]
                if has_structure_failure:
                    failures.append({
                        "gate": "estrutura",
                        "detail": str(structure_audit.get("failure") or "paragraph capacity is insufficient"),
                    })
                input_post["rework"] = {"blocker": state.blocker.value if state.blocker else "editorial", "failed_gates": failures, "previous_editorial": previous_editorial or {}}
                input_path.write_text(json.dumps(input_payload, ensure_ascii=False), encoding="utf-8")
            generated = generate_editorial_batch(
                prepared["editorial_input"],
                api_key=self.config.editorial_api_key,
                base_url=self.config.editorial_base_url,
                model=self.config.editorial_model,
                timeout=self.config.http_timeout,
                min_confidence=self.config.min_relevance_confidence,
                root=self.root,
                output_path=self.root / "work" / "v2-editorial" / f"{post_id}.json",
            )
            envelope = load_editorial_batch(generated["output"])
            item = next((item for item in envelope["items"] if int(item["post_id"]) == post_id), None)
            if not item or item.get("status") == "needs_retry":
                retry_kind = str((item or {}).get("retry_kind") or "none")
                reason = str((item or {}).get("reason") or "editorial provider requested retry")
                failure_class = str(
                    (item or {}).get("failure_class") or "editorial_content_retry"
                )
                retryable = bool((item or {}).get("retryable", True))
                http_status = (item or {}).get("http_status")
                context.update({
                    "provider_failure_phase": Phase.EDITORIAL.value,
                    "provider_failure_class": failure_class,
                    "provider_retryable": retryable,
                    "provider_http_status": http_status,
                })
                if retry_kind == "facts":
                    _write_json(self.root, post_id, "editorial.error.json", {
                        "reason": reason,
                        "status": (item or {}).get("status", "needs_retry"),
                        "blocker": BlockerCode.TEXT_QUALITY.value,
                        "failure_class": failure_class,
                        "retryable": retryable,
                        "http_status": http_status,
                        "phase": Phase.EDITORIAL.value,
                    })
                    raise StageError(BlockerCode.TEXT_QUALITY, Phase.EDITORIAL, reason, human_required=True)
                if retry_kind == "seo":
                    blocker, phase = BlockerCode.SEO, Phase.EDITORIAL
                elif retry_kind == "relevance":
                    blocker, phase = BlockerCode.RELEVANCE_UNCERTAIN, Phase.RELEVANCE
                elif retry_kind == "text":
                    blocker, phase = BlockerCode.TEXT_QUALITY, Phase.EDITORIAL
                else:
                    reason_lower = reason.casefold()
                    if "meta_description" in reason_lower or "seo" in reason_lower:
                        blocker, phase = BlockerCode.SEO, Phase.EDITORIAL
                    elif "matched_topics" in reason_lower or "relevan" in reason_lower:
                        blocker, phase = BlockerCode.RELEVANCE_UNCERTAIN, Phase.RELEVANCE
                    elif "quality" in reason_lower or "keyword" in reason_lower or "content" in reason_lower:
                        blocker, phase = BlockerCode.TEXT_QUALITY, Phase.EDITORIAL
                    else:
                        blocker, phase = BlockerCode.PROVIDER_ERROR, Phase.EDITORIAL
                _write_json(self.root, post_id, "editorial.error.json", {
                    "reason": reason,
                    "status": (item or {}).get("status", "needs_retry"),
                    "blocker": blocker.value,
                    "failure_class": failure_class,
                    "retryable": retryable,
                    "http_status": http_status,
                    "phase": phase.value,
                })
                raise StageError(blocker, phase, reason)
            raw_editorial = dict(item["editorial"])
            if must_localize:
                missing = []
                if not str(raw_editorial.get("title") or "").strip():
                    missing.append("title")
                if not str(raw_editorial.get("cleaned_html") or "").strip():
                    missing.append("cleaned_html")
                seo_payload = raw_editorial.get("seo")
                if not isinstance(seo_payload, dict) or any(
                    not str(seo_payload.get(name) or "").strip()
                    for name in ("title", "meta_description", "focus_keyword")
                ):
                    missing.append("seo")
                if missing:
                    raise StageError(
                        BlockerCode.TEXT_QUALITY,
                        Phase.EDITORIAL,
                        "localizacao_pt_br_incompleta: " + ", ".join(missing),
                    )
            if state.phase is Phase.EDITORIAL and isinstance(previous_editorial, dict):
                previous_clean = dict(previous_editorial)
                previous_clean.pop("decision", None)
                previous_clean.pop("localization", None)
                merged = dict(previous_clean)
                merged.update({key: value for key, value in raw_editorial.items() if value is not None})
                if isinstance(previous_clean.get("seo"), dict) and isinstance(raw_editorial.get("seo"), dict):
                    merged["seo"] = {**previous_clean["seo"], **raw_editorial["seo"]}
                if must_localize:
                    for field in ("title", "cleaned_html", "seo"):
                        if raw_editorial.get(field) is None:
                            merged.pop(field, None)
                editorial = resolve_editorial_defaults(merged, _post(context))
            else:
                editorial = resolve_editorial_defaults(raw_editorial, _post(context))
            repaired = _repair_focus_keyword_draft(editorial, _post(context))
            if repaired is not None:
                editorial = repaired
            editorial["cleaned_html"] = normalize_h1_headings(
                normalize_editorial_dashes(editorial.get("cleaned_html", "")),
                post_title=post_title,
            )
            editorial = validate_editorial(editorial, min_confidence=self.config.min_relevance_confidence)
            editorial["decision"] = (editorial.get("site_relevance") or {}).get("decision")
            if isinstance(localization, dict):
                _write_json(self.root, post_id, "editorial.localization.json", {
                    "required": must_localize,
                    "reason": str(localization.get("reason") or ""),
                    "source_language": input_post.get("source_language") or {},
                })
            editorial, structure_audit, structure_error = _prepare_editorial_media_structure(
                editorial,
                state,
                title=post_title,
                categories=context.get("categories") or (),
            )
            _write_json(self.root, post_id, "editorial.structure.json", structure_audit)
            if structure_error:
                if str(editorial.get("cleaned_html") or "").strip():
                    _write_json(self.root, post_id, "editorial.draft.json", editorial)
                raise StageError(
                    BlockerCode.STRUCTURE,
                    Phase.EDITORIAL,
                    f"media_structure:insufficient_paragraph_slots: {structure_error}",
                )
            _write_json(self.root, post_id, "editorial.draft.json", editorial)
            return editorial
        except StageError:
            raise
        except EditorialProviderError as exc:
            context.update({
                "provider_failure_phase": Phase.EDITORIAL.value,
                "provider_failure_class": exc.failure_class,
                "provider_retryable": exc.retryable,
                "provider_http_status": exc.http_status,
            })
            _write_json(self.root, post_id, "editorial.error.json", {
                "reason": str(exc),
                "status": "provider_error",
                "failure_class": exc.failure_class,
                "retryable": exc.retryable,
                "http_status": exc.http_status,
                "phase": Phase.EDITORIAL.value,
            })
            raise StageError(BlockerCode.PROVIDER_ERROR, Phase.EDITORIAL, str(exc)) from exc
        except BatchError as exc:
            raise StageError(BlockerCode.MANIFEST_INVALID, Phase.EDITORIAL, str(exc)) from exc
        except Exception as exc:
            record_unexpected_error(
                self.root, exc, post_id=post_id,
                run_id=str(context.get("run_id") or context.get("batch_id") or ""),
                file="pipeline_v2/production_stages.py", field="ProductionEditorialStage",
            )
            raise StageError(BlockerCode.MANIFEST_INVALID, Phase.EDITORIAL, str(exc)) from exc


class ProductionMediaStage:
    def __init__(self, client: Any, config: Any, root: Path, resolver: Callable[..., Any] | None = None):
        self.client, self.config, self.root, self.resolver = client, config, Path(root), resolver

    def __call__(self, context: dict[str, Any], state: Any, editorial: dict[str, Any]) -> MediaProgress:
        # Only snapshots produced by this invocation may survive its failure.
        context.pop("_v2_media_progress", None)
        previous = state.media
        try:
            editorial, _structure_audit, layout_policy = _ensure_prepared_media_structure(
                self.root,
                int(context["post_id"]),
                editorial,
                state,
                title=str(context.get("title") or ""),
                categories=context.get("categories") or (),
            )
            ideal_required = required_image_count_for_content(
                str(editorial.get("cleaned_html") or ""),
                title=str(context.get("title") or ""),
            )
            required = int(layout_policy.get("target_effective") or ideal_required)
            previous = replace(
                previous,
                required=required,
                layout_policy=layout_policy,
            )
            state = replace(state, media=previous) if isinstance(state, WorkState) else SimpleNamespace(
                media=previous,
                phase=getattr(state, "phase", None),
            )
            accepted = {item.media_id: item for item in previous.inline}
            featured = previous.featured
            if self.resolver is not None:
                result = self.resolver(context, state, editorial, previous)
                if isinstance(result, MediaProgress):
                    media = replace(result, required=required, layout_policy=layout_policy)
                else:
                    media = self._from_result(result, required, accepted, featured, layout_policy)
            else:
                plan = list(editorial.get("media_plan") or [])
                if featured.status is FeaturedStatus.VALID:
                    plan = [item for item in plan if not item.get("is_featured")]
                plan = [item for item in plan if item.get("is_featured") or int(item.get("paragraph_index", -1)) not in {x.slot for x in accepted.values()}]
                checked = validate_media_plan(self.client, {**editorial, "media_plan": plan}, config=self.config, root=self.root, post_title=str(context.get("title") or ""), existing_featured_id=featured.media_id if featured.status is FeaturedStatus.VALID else None, post_id=int(context["post_id"]))
                vision_errors = [
                    row for row in checked.get("featured_vision", [])
                    if isinstance(row, dict) and row.get("technical")
                ]
                if vision_errors:
                    raise StageError(
                        BlockerCode.PROVIDER_ERROR,
                        Phase.MEDIA,
                        str(vision_errors[0].get("reason") or "vision provider error"),
                    )
                results, featured_id, featured_credit = _execute_media_plan(
                    {**editorial, "media_plan": plan},
                    self.config,
                    self.client,
                    self.root,
                    preflight=checked,
                    post_id=int(context["post_id"]),
                    previous_inline_phashes=tuple(item.phash for item in previous.inline if item.phash),
                    previous_inline_visual_assets=[item.to_dict() for item in previous.inline],
                    featured_visual_asset=(
                        previous.featured.to_dict()
                        if previous.featured.status is FeaturedStatus.VALID
                        and previous.featured.media_id and previous.featured.media_url
                        else None
                    ),
                )
                inline = tuple(
                    InlineMedia(
                        int(row["media_id"]),
                        str(row["media_url"]),
                        int(row.get("paragraph_index", 0)),
                        str(row.get("alt_text", "")),
                        str(row.get("credit_text", "")),
                        str(row.get("subject") or ""),
                        row.get("item_number"),
                        str(row.get("section_heading") or ""),
                        row.get("section_slot"),
                        int(row.get("width") or 1200),
                        int(row.get("height") or 800),
                        str(row.get("phash") or ""),
                        str(row.get("sha256") or ""),
                        str(row.get("visual_group_id") or ""),
                        dict(row.get("visual_verification") or {}),
                        str(row.get("item_identity") or ""),
                        str(row.get("item_title") or ""),
                        str(row.get("item_group") or ""),
                        int(row["html_position"]) if row.get("html_position") is not None else None,
                    )
                    for row in results
                    if row.get("status") in {"accepted", "ok"}
                    and row.get("media_id")
                    and not row.get("featured")
                )
                inline = tuple(accepted.values()) + tuple(item for item in inline if item.media_id not in accepted)
                featured_row = next((row for row in results if row.get("featured") and row.get("media_id")), {})
                fp = FeaturedProgress(
                    FeaturedStatus.VALID, featured_id, str(featured_row.get("media_url") or ""),
                    str(featured_row.get("sha256") or ""), str(featured_row.get("phash") or ""),
                    str(featured_row.get("visual_group_id") or ""), dict(featured_row.get("visual_verification") or {}),
                    str(featured_row.get("alt_text") or ""),
                    str(featured_row.get("credit_text") or ""),
                    int(featured_row.get("width") or 0),
                    int(featured_row.get("height") or 0),
                ) if featured_id else featured
                media = MediaProgress(
                    required=required,
                    inline=inline,
                    featured=fp,
                    search=previous.search,
                    layout_policy=layout_policy,
                )
            if getattr(state, "phase", None) is Phase.MEDIA and media.enrichment_round <= previous.enrichment_round:
                media = replace(
                    media,
                    enrichment_round=previous.enrichment_round + 1,
                )
            _write_json(self.root, int(context["post_id"]), "editorial.partial.json", media.to_dict())
            return media
        except Exception as exc:
            progress = context.get("_v2_media_progress")
            progress = progress if isinstance(progress, MediaProgress) else None
            if isinstance(exc, StageError):
                exc.media_progress = progress or exc.media_progress
                raise
            blocker = BlockerCode.INTERNAL_ERROR
            if isinstance(exc, (VisionGateError, TimeoutError, ConnectionError)):
                blocker = BlockerCode.PROVIDER_ERROR
            elif isinstance(exc, WordPressError):
                blocker = BlockerCode.WORDPRESS_ERROR
            error_class = (
                "provider" if blocker is BlockerCode.PROVIDER_ERROR else
                "wordpress" if blocker is BlockerCode.WORDPRESS_ERROR else
                "contract" if isinstance(exc, TypeError) else
                "internal"
            )
            try:
                record_unexpected_error(
                    self.root, exc, post_id=int(context["post_id"]),
                    run_id=str(context.get("v2_run_id") or ""),
                    file="pipeline_v2/production_stages.py", field="ProductionMediaStage",
                    error_class=error_class,
                )
            except Exception:  # diagnostics must not mask durable progress
                pass
            raise StageError(
                blocker, Phase.MEDIA, _redact_text(f"{type(exc).__name__}: {exc}"),
                media_progress=progress,
            ) from exc

    @staticmethod
    def _from_result(
        result: Any,
        required: int,
        accepted: dict[int, InlineMedia],
        featured: FeaturedProgress,
        layout_policy: dict[str, Any] | None = None,
    ) -> MediaProgress:
        rows = result if isinstance(result, list) else (result.get("inline") or [])
        inline = tuple(accepted.values()) + tuple(InlineMedia.from_dict(row) for row in rows if int(row.get("media_id", 0)) not in accepted)
        search = MediaSearchProgress.from_dict(result.get("search")) if isinstance(result, dict) else MediaSearchProgress()
        return MediaProgress(
            required=required,
            inline=inline,
            featured=featured,
            search=search,
            layout_policy=dict(layout_policy or {}),
        )


class ProductionComposeStage:
    def __init__(self, config: Any, root: Path):
        self.config, self.root = config, Path(root)

    def __call__(self, context: dict[str, Any], editorial: dict[str, Any], media: MediaProgress) -> dict[str, Any]:
        return self.compose_candidate(context, editorial, media, persist=True)

    def compose_candidate(
        self,
        context: dict[str, Any],
        editorial: dict[str, Any],
        media: MediaProgress,
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        try:
            source_editorial, structure_audit, source_layout_policy = _ensure_prepared_media_structure(
                self.root,
                int(context["post_id"]),
                editorial,
                SimpleNamespace(media=media),
                title=str(context.get("title") or ""),
                categories=context.get("categories") or (),
                persist=persist,
            )
            editorial = source_editorial
            ideal_required = required_image_count_for_content(
                str(editorial.get("cleaned_html") or ""),
                title=str(context.get("title") or ""),
            )
            effective_required = int(
                source_layout_policy.get("target_effective") or ideal_required
            )
            media = replace(
                media,
                required=effective_required,
                layout_policy=source_layout_policy,
            )
            is_listicle = detect_list_format(
                str(context.get("title") or ""),
                str(editorial.get("cleaned_html") or ""),
                editorial.get("categories") or context.get("categories") or (),
            ) is not None
            existing_html = normalize_h1_headings(
                str(editorial.get("cleaned_html") or ""),
                post_title=str(context.get("title") or ""),
            )
            subject_rows = post_subjects(
                title=str(context.get("title") or ""),
                content_html=str(editorial.get("cleaned_html") or ""),
                categories=editorial.get("categories") or context.get("categories") or (),
            )
            is_listicle = is_listicle or len(subject_rows) > 1
            reconciliation = reconcile_working_visual_media(
                media,
                listicle_items=subject_rows if is_listicle else (),
            )
            accounting = reconciliation.accounting
            valid_ids = set(accounting.valid_inline_ids)
            duplicate_ids = set(accounting.duplicate_inline_ids)
            from ..media.relevance import iter_content_images

            final_inline_urls = iter_content_images(existing_html)
            final_url = _canonical_media_ref(str(final_inline_urls[-1].get("src") or "")) if final_inline_urls else ""
            featured_id = media.featured.media_id
            permitted_list_repeat_ids = {
                item.media_id
                for item in media.inline
                if is_listicle
                and item.media_id in duplicate_ids
                and featured_id is not None
                and str((item.visual_verification or {}).get("duplicate_of") or "") == str(featured_id)
                and _canonical_media_ref(item.media_url) == final_url
            }
            duplicate_urls = [
                item.media_url for item in media.inline
                if item.media_id in duplicate_ids
                and item.media_id not in permitted_list_repeat_ids
                and item.media_url
            ]
            existing_html = remove_media_urls(
                existing_html,
                [*_visual_reconcile_urls(self.root, int(context["post_id"]), media), *duplicate_urls],
            )
            featured_url = str(media.featured.media_url or "").strip()
            if featured_url and not is_listicle:
                existing_html = remove_media_urls(existing_html, [featured_url])
            # Removing an invalid/duplicate image may leave an empty paragraph
            # and changes the layout identity. Prepare and hash that exact body
            # again before selecting any new placement.
            if existing_html != str(editorial.get("cleaned_html") or ""):
                prepared_editorial, structure_audit, structure_error = _prepare_editorial_media_structure(
                    {**editorial, "cleaned_html": existing_html},
                    SimpleNamespace(media=media),
                    title=str(context.get("title") or ""),
                    categories=context.get("categories") or (),
                )
                if structure_error:
                    raise MediaInsertionError(
                        f"prepared body after media cleanup is not placeable: {structure_error}",
                        code="insufficient_paragraph_slots",
                    )
                editorial = prepared_editorial
                existing_html = str(editorial.get("cleaned_html") or "")
                source_layout_policy = dict(structure_audit.get("media_layout_policy") or {})
                if persist:
                    _write_json(self.root, int(context["post_id"]), "editorial.structure.json", structure_audit)
                    _write_json(self.root, int(context["post_id"]), "editorial.draft.json", editorial)
                effective_required = int(source_layout_policy.get("target_effective") or ideal_required)
                media = replace(
                    media,
                    required=effective_required,
                    layout_policy=source_layout_policy,
                )
            insertable_urls = set(normal_media_urls_to_insert(
                existing_html,
                [str(item.media_url or "") for item in media.inline if item.media_id in valid_ids],
                featured_url,
            ))
            seen_urls: set[str] = set()
            placements: list[dict[str, Any]] = []
            eligible_inline = tuple(reconciliation.eligible_inline) + tuple(
                item for item in media.inline if item.media_id in permitted_list_repeat_ids
            )
            for index, item in enumerate(eligible_inline):
                media_url = str(item.media_url or "").strip()
                if (
                    media_url not in insertable_urls
                    or media_url in seen_urls
                ):
                    continue
                seen_urls.add(media_url)
                placements.append({
                    "paragraph_index": (
                        item.section_slot
                        if is_listicle and item.section_slot is not None
                        else (index if is_listicle else item.slot)
                    ),
                    "media_url": media_url,
                    "alt_text": item.alt_text,
                    "credit_text": item.credit_text,
                    "width": item.width,
                    "height": item.height,
                    "item_identity": item.item_identity,
                    "item_title": item.item_title or item.subject,
                    "item_group": item.item_group or item.section_heading,
                    "html_position": item.html_position,
                    "section_heading": item.section_heading,
                    "section_slot": item.section_slot,
                    "subject": item.subject,
                })
            closing_repeat_record: dict[str, Any] = {}
            candidate_inline = eligible_inline
            if not is_listicle:
                if source_layout_policy:
                    planned_positions = list(source_layout_policy.get("placement_plan") or [])
                    layout_quota_reduced = (
                        source_layout_policy.get("reduction_reason") == "layout_limited"
                        and int(source_layout_policy.get("target_effective") or 0)
                        < int(source_layout_policy.get("target_ideal") or 0)
                        and accounting.valid_distinct_inline_count <= ideal_required
                    )
                    body_hash = str(source_layout_policy.get("body_sha256") or "")
                    if editorial_body_sha256(existing_html) != body_hash:
                        raise MediaInsertionError(
                            "prepared body changed after layout planning",
                            code="layout_body_hash_mismatch",
                        )
                    feature = media.featured
                    repeat_policy = source_layout_policy.get("closing_featured_repeat")
                    feature_decision = str(
                        (feature.visual_verification or {}).get("decision") or ""
                    ).upper()
                    repeat_policy_matches = bool(
                        isinstance(repeat_policy, dict)
                        and repeat_policy.get("role") == "closing_featured_repeat"
                        and repeat_policy.get("placement") == "after_editorial_body"
                        and repeat_policy.get("media_id") == feature.media_id
                        and _canonical_media_ref(str(repeat_policy.get("media_url") or ""))
                        == _canonical_media_ref(str(feature.media_url or ""))
                        and repeat_policy.get("asset_sha256") == feature.sha256
                        and repeat_policy.get("asset_phash") == feature.phash
                        and repeat_policy.get("visual_group_id") == feature.visual_group_id
                        and repeat_policy.get("body_sha256") == body_hash
                        and repeat_policy.get("placement_plan_sha256")
                        == source_layout_policy.get("placement_plan_sha256")
                    )
                    exhausted_proof = bool(
                        media.waiver_applied
                        and media.search.completed
                        and media.search.exhausted
                        and media.search.completion_reason == "EXHAUSTED_VERIFIED"
                    )
                    feature_display_ready = bool(
                        feature.status is FeaturedStatus.VALID
                        and feature.media_id and feature.media_url
                        and feature.alt_text.strip()
                        and feature.credit_text.strip().casefold().startswith("crédito da imagem:")
                        and 1200 <= feature.width <= 1600 and feature.height > 0
                        and feature.sha256 and feature.phash and feature.visual_group_id
                        and feature_decision in {"INITIAL", "DIFFERENT"}
                    )
                    closing_anchor = (
                        planned_positions[-1]
                        if planned_positions
                        and planned_positions[-1].get("anchor") == "after_editorial_body"
                        else None
                    )
                    use_closing_repeat = bool(
                        repeat_policy_matches
                        and feature_display_ready
                        and exhausted_proof
                        and accounting.effective_missing > 0
                        and closing_anchor is not None
                        and len(placements) < len(planned_positions)
                    )
                    available_positions = planned_positions[:-1] if use_closing_repeat else planned_positions
                    if len(placements) > len(available_positions):
                        if use_closing_repeat:
                            use_closing_repeat = False
                            available_positions = planned_positions
                        if len(placements) > len(available_positions):
                            if not layout_quota_reduced:
                                raise MediaInsertionError(
                                    "composed media exceeds the verified layout placement capacity: "
                                    f"requested={len(placements)}, available={len(available_positions)}",
                                    code="insufficient_paragraph_slots",
                                )
                            # When the verified policy already reduced the quota,
                            # retain only the deterministic first-fit set in this
                            # candidate. The writer preserves valid excess media
                            # as accepted-but-not-applied working state.
                            placements = placements[:len(available_positions)]
                    selected_slots = select_normal_media_slots(
                        [int(row["paragraph_index"]) for row in available_positions],
                        len(placements),
                        minimum_spacing=int(source_layout_policy.get("minimum_spacing") or 3),
                    )
                    anchors_by_slot = {
                        int(row["paragraph_index"]): row for row in available_positions
                    }
                    for placement, slot in zip(placements, selected_slots):
                        anchor = anchors_by_slot[slot]
                        if anchor.get("body_sha256") != body_hash:
                            raise MediaInsertionError(
                                "layout placement anchor is not bound to the prepared body",
                                code="layout_anchor_hash_mismatch",
                            )
                        placement["paragraph_index"] = int(anchor["paragraph_index"])
                        if anchor.get("anchor") == "after_editorial_body":
                            placement["placement"] = "after_editorial_body"
                        else:
                            placement.pop("placement", None)
                    if use_closing_repeat and closing_anchor is not None:
                        repeat_slot = int(closing_anchor["paragraph_index"])
                        placements.append({
                            "paragraph_index": repeat_slot,
                            "placement": "after_editorial_body",
                            "role": "closing_featured_repeat",
                            "media_id": int(feature.media_id),
                            "media_url": str(feature.media_url),
                            "alt_text": feature.alt_text,
                            "credit_text": feature.credit_text,
                            "width": feature.width,
                            "height": feature.height,
                            "subject": feature.alt_text,
                        })
                        closing_repeat_record = {
                            **dict(repeat_policy),
                            "paragraph_index": repeat_slot,
                            "placement": "after_editorial_body",
                        }
                    already_embedded_urls = {
                        _canonical_media_ref(str(image.get("src") or ""))
                        for image in iter_content_images(existing_html)
                        if str(image.get("src") or "").strip()
                    }
                    placed_urls = {
                        _canonical_media_ref(str(item.get("media_url") or ""))
                        for item in placements
                        if item.get("role") != "closing_featured_repeat"
                    }
                    candidate_urls = already_embedded_urls | placed_urls
                    candidate_inline = tuple(
                        item for item in eligible_inline
                        if _canonical_media_ref(str(item.media_url or "")) in candidate_urls
                    )
                else:
                    if placements:
                        placements = plan_normal_media_insertions(existing_html, placements)
            placement_audit = [
                {
                    "media_url": str(item.get("media_url") or ""),
                    "paragraph_index": int(item.get("paragraph_index", 0)),
                    **(
                        {"role": str(item["role"]), "media_id": int(item["media_id"])}
                        if item.get("role") == "closing_featured_repeat" and item.get("media_id")
                        else {}
                    ),
                    **(
                        {"placement": str(item["placement"])}
                        if item.get("placement")
                        else {}
                    ),
                    **(
                        {
                            "body_sha256": str(source_layout_policy.get("body_sha256") or ""),
                            "placement_plan_sha256": str(source_layout_policy.get("placement_plan_sha256") or ""),
                        }
                        if source_layout_policy and not is_listicle
                        else {}
                    ),
                }
                for item in placements
            ]
            prepared_body = str(editorial.get("cleaned_html") or "")
            working = dict(editorial)
            # Drop legacy operational localization context before creating the
            # candidate; the canonical editorial document remains schema-only.
            working.pop("localization", None)
            working["cleaned_html"] = insert_media(
                existing_html,
                placements,
                listicle=is_listicle,
            ) if placements else existing_html
            content, trailer, trailer_status = compose_final_content(working, self.config, context.get("original_link"), root=self.root)
            working = attach_trailer_audit(working, trailer, search_status=trailer_status)
            content = normalize_editorial_dashes(content)
            # Keep the exact prepared editorial body as the quota and layout
            # evidence source. The composed publication HTML is stored
            # separately in candidate["content"] and may contain captions,
            # CTA, credits, and source boilerplate that must not alter the
            # 2/4/6 quota or the layout proof.
            candidate_editorial = dict(working)
            candidate_editorial["cleaned_html"] = prepared_body
            candidate_media = replace(
                media,
                inline=candidate_inline,
                accepted_count=None,
                layout_policy=source_layout_policy,
                closing_featured_repeat=closing_repeat_record,
            )
            candidate = {"content": content, "editorial": candidate_editorial, "seo": candidate_editorial.get("seo") or {}, "title": str(candidate_editorial.get("title") or "").strip() or None, "featured_media": media.featured.media_id, "media": candidate_media.to_dict(), "media_placements": placement_audit, "trailer": trailer, "trailer_status": trailer_status, "_v2_run_id": str(context.get("v2_run_id") or "")}
            if persist:
                _write_json(self.root, int(context["post_id"]), "editorial.candidate.json", candidate)
            return candidate
        except MediaInsertionError as exc:
            raise StageError(
                BlockerCode.STRUCTURE,
                Phase.EDITORIAL,
                f"media_insertion:{exc.code}: {exc}",
            ) from exc
        except Exception as exc:
            raise StageError(BlockerCode.MANIFEST_INVALID, Phase.COMPOSE, str(exc)) from exc


def _checklist_failures(checklist: dict[str, Any]) -> list[dict[str, Any]]:
    """Preserve both failed and errored checklist gates for classification."""
    valid_phases = {"relevance", "editorial", "media", "compose", "validate", "publish"}
    failures: list[dict[str, Any]] = []
    for item in checklist.get("items", []):
        if item.get("status") not in {"fail", "error"}:
            continue
        failure = {"gate": item["name"], "detail": item.get("detail", "")}
        if item.get("invalid_media"):
            failure["invalid_media"] = item["invalid_media"]
        if item.get("blocker"):
            failure["blocker"] = item["blocker"]
        phase = item.get("phase")
        if phase == "media_validation":
            phase = "media"
        if phase in valid_phases:
            failure["phase"] = phase
        failures.append(failure)
    return failures


class ProductionValidateStage:
    def __init__(self, client: Any, config: Any, root: Path):
        self.client, self.config, self.root = client, config, Path(root)

    def __call__(self, context: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
        try:
            post = _post(context)
            post_id = int(context["post_id"])
            candidate_media = MediaProgress.from_dict(candidate.get("media"))
            source_editorial = None
            if "candidate_must_be_fresh" in context:
                source_editorial = context.get("editorial") or context.get("draft")
            if "candidate_must_be_fresh" in context and not isinstance(source_editorial, dict):
                draft_path = self.root / "backups" / str(post_id) / "editorial.draft.json"
                source_editorial = _json_object(draft_path) if draft_path.is_file() else None
            if isinstance(source_editorial, dict):
                source_editorial = dict(source_editorial)
                source_editorial, _structure_audit, source_policy = _ensure_prepared_media_structure(
                    self.root,
                    post_id,
                    source_editorial,
                    SimpleNamespace(media=candidate_media),
                    title=str(context.get("title") or ""),
                    categories=context.get("categories") or (),
                )
                source_body = str(source_editorial.get("cleaned_html") or "")
                ideal_required = required_image_count_for_content(
                    source_body,
                    title=str(context.get("title") or ""),
                )
                effective_required = int(
                    source_policy.get("target_effective") or ideal_required
                )
                current_policy = candidate_media.layout_policy
                current_body = str((candidate.get("editorial") or {}).get("cleaned_html") or "")
                policy_valid = False
                if current_policy:
                    policy_result = validate_normal_article_layout_policy(
                        source_body,
                        current_policy,
                        target_ideal=ideal_required,
                        media=candidate_media,
                        listicle=not bool(source_policy),
                    )
                    policy_valid = bool(
                        policy_result.get("valid")
                        and current_policy == source_policy
                        and candidate_media.required == effective_required
                    )
                elif not source_policy:
                    policy_valid = candidate_media.required == effective_required
                if current_body != source_body or not policy_valid:
                    candidate_media = replace(
                        candidate_media,
                        required=effective_required,
                        layout_policy=source_policy,
                    )
                    candidate = ProductionComposeStage(
                        self.config, self.root
                    ).compose_candidate(
                        context,
                        source_editorial,
                        candidate_media,
                        persist=True,
                    )
            checklist_editorial = dict(candidate["editorial"])
            checklist_editorial.pop("decision", None)
            # Localization is preparation context, not part of the strict
            # model/editorial schema. Older drafts may still contain it.
            checklist_editorial.pop("localization", None)
            candidate["editorial"] = dict(checklist_editorial)
            candidate["content"] = normalize_editorial_dashes(candidate.get("content", ""))
            checklist_editorial["cleaned_html"] = normalize_editorial_dashes(checklist_editorial.get("cleaned_html", ""))
            candidate["editorial"]["cleaned_html"] = checklist_editorial["cleaned_html"]
            _persist_draft_repairs(self.root, int(context["post_id"]), normalize_dashes=True)
            _write_json(self.root, int(context["post_id"]), "editorial.candidate.json", candidate)
            post_for_checklist = dict(post)
            candidate_featured = candidate.get("featured_media")
            post_for_checklist["featured_media"] = (
                candidate_featured
                if isinstance(candidate_featured, int) and candidate_featured > 0
                else None
            )
            candidate_title = str(candidate.get("title") or "").strip()
            if candidate_title:
                post_for_checklist["title"] = {"raw": candidate_title}
            seo = dict(checklist_editorial.get("seo") or {})
            checklist = run_pre_publish_checklist(post=post_for_checklist, editorial=checklist_editorial, content=str(candidate["content"]), backup_path=self.root / "backups" / str(context["post_id"]) / "editorial.draft.json", config=self.config, client=self.client, attempts=int((context.get("v2_state").retry.attempts if context.get("v2_state") else 0)), media_context=candidate.get("media"))
            raw_failures = [item for item in checklist.get("items", []) if item.get("status") == "fail"]
            focus_keyword_failure = any(
                item.get("name") == "qualidade_texto"
                and "focus keyword" in str(item.get("detail") or "").casefold()
                for item in raw_failures
            )
            if focus_keyword_failure:
                subjects = post_subjects(title=str(seo.get("title") or ""), content_html=str(candidate["content"]), focus_keyword=str(seo.get("focus_keyword") or ""), game_name=checklist_editorial.get("game_name"))
                title_lower = str(seo.get("title") or "").casefold()
                content_lower = str(candidate["content"]).casefold()
                replacement = next(
                    (
                        subject
                        for row in subjects
                        if (subject := str(row.get("subject") or "").strip())
                        and _keyword_in_text(subject, title_lower)
                        and _keyword_in_text(subject, content_lower)
                    ),
                    "",
                )
                if replacement:
                    seo["focus_keyword"] = replacement
                    checklist_editorial["seo"] = seo
                    candidate["editorial"]["seo"] = seo
                    candidate["seo"] = seo
                    _persist_draft_repairs(
                        self.root,
                        int(context["post_id"]),
                        focus_keyword=str(replacement),
                    )
                    _write_json(self.root, int(context["post_id"]), "editorial.candidate.json", candidate)
                    checklist = run_pre_publish_checklist(post=post_for_checklist, editorial=checklist_editorial, content=str(candidate["content"]), backup_path=self.root / "backups" / str(context["post_id"]) / "editorial.draft.json", config=self.config, client=self.client, attempts=int((context.get("v2_state").retry.attempts if context.get("v2_state") else 0)), media_context=candidate.get("media"))
            media_decision = checklist.get("media_decision") if isinstance(checklist, dict) else None
            if isinstance(media_decision, dict) and isinstance(candidate.get("media"), dict):
                candidate_media = dict(candidate["media"])
                candidate_media["enrichment_round"] = int(media_decision.get("enrichment_round") or 0)
                candidate_media["waiver"] = {
                    "applied": bool(media_decision.get("waiver_applied")),
                    "reason": str(media_decision.get("waiver_reason") or ""),
                }
                candidate["media"] = candidate_media
                _write_json(self.root, int(context["post_id"]), "editorial.candidate.json", candidate)
            failures = _checklist_failures(checklist)
            result = {
                "passed": bool(checklist.get("all_passed")), "failures": failures,
                "checklist": checklist, "_v2_run_id": str(context.get("v2_run_id") or ""),
                "candidate_run_id": str(candidate.get("_v2_run_id") or ""),
                "candidate_hash": hashlib.sha256(str(candidate.get("content") or "").encode()).hexdigest(),
            }
            _write_json(self.root, int(context["post_id"]), "editorial.validation.json", result)
            return result
        except StageError:
            raise
        except Exception as exc:
            raise StageError(BlockerCode.MANIFEST_INVALID, Phase.VALIDATE, str(exc)) from exc


__all__ = ["ProductionEditorialStage", "ProductionMediaStage", "ProductionComposeStage", "ProductionValidateStage"]
