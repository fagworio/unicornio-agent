"""Production stage adapters for the V2 runner.

These adapters deliberately reuse the mature V1 functions.  They do not call
``apply_editorial``; orchestration and lifecycle decisions remain in V2.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from ..batch import load_editorial_batch, prepare_batch
from ..checklist import run_pre_publish_checklist
from ..checklist import required_image_count
from ..content_quality import normalize_editorial_dashes, word_count
from ..content_quality import _keyword_in_text
from ..editorial_provider import EditorialProviderError, generate_editorial_batch
from ..editorial_schema import validate_editorial
from ..list_quality import detect_list_format
from ..media.evidence import post_subjects
from ..media.inserter import insert_media
from ..workflow import (
    _execute_media_plan,
    compose_final_content,
    attach_trailer_audit,
    resolve_editorial_defaults,
    validate_media_plan,)
from .errors import StageError
from .model import BlockerCode, FeaturedProgress, FeaturedStatus, InlineMedia, MediaProgress, Phase


def _write_json(root: Path, post_id: int, name: str, value: dict[str, Any]) -> Path:
    target = root / "backups" / str(post_id) / name
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)
    return target


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
            input_payload = json.loads(input_path.read_text(encoding="utf-8"))
            draft_path = self.root / "backups" / str(post_id) / "editorial.draft.json"
            previous_editorial = json.loads(draft_path.read_text(encoding="utf-8")) if draft_path.is_file() else None
            validation_path = self.root / "backups" / str(post_id) / "editorial.validation.json"
            if state.phase is Phase.EDITORIAL and validation_path.is_file():
                validation = json.loads(validation_path.read_text(encoding="utf-8"))
                editorial_gates = {"qualidade_texto", "seo", "estrutura", "fonte", "schema_editorial", "conteudo_nao_vazio", "conteudo_sem_metadados_operacionais", "estrutura_lista", "cta_canonico"}
                failures = [failure for failure in validation.get("failures", []) if failure.get("gate") in editorial_gates]
                input_payload["posts"][0]["rework"] = {"blocker": state.blocker.value if state.blocker else "editorial", "failed_gates": failures, "previous_editorial": previous_editorial or {}}
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
                if retry_kind == "facts":
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
                _write_json(self.root, post_id, "editorial.error.json", {"reason": reason, "status": (item or {}).get("status", "needs_retry"), "blocker": blocker.value})
                raise StageError(blocker, phase, reason)
            raw_editorial = dict(item["editorial"])
            if state.phase is Phase.EDITORIAL and isinstance(previous_editorial, dict):
                previous_clean = dict(previous_editorial)
                previous_clean.pop("decision", None)
                merged = dict(previous_clean)
                merged.update({key: value for key, value in raw_editorial.items() if value is not None})
                if isinstance(previous_clean.get("seo"), dict) and isinstance(raw_editorial.get("seo"), dict):
                    merged["seo"] = {**previous_clean["seo"], **raw_editorial["seo"]}
                editorial = resolve_editorial_defaults(merged, _post(context))
            else:
                editorial = resolve_editorial_defaults(raw_editorial, _post(context))
            editorial["cleaned_html"] = normalize_editorial_dashes(editorial.get("cleaned_html", ""))
            editorial = validate_editorial(editorial, min_confidence=self.config.min_relevance_confidence)
            editorial["decision"] = (editorial.get("site_relevance") or {}).get("decision")
            _write_json(self.root, post_id, "editorial.draft.json", editorial)
            return editorial
        except StageError:
            raise
        except EditorialProviderError as exc:
            _write_json(self.root, post_id, "editorial.error.json", {"reason": str(exc), "status": "provider_error"})
            raise StageError(BlockerCode.PROVIDER_ERROR, Phase.EDITORIAL, str(exc)) from exc
        except Exception as exc:
            raise StageError(BlockerCode.MANIFEST_INVALID, Phase.EDITORIAL, str(exc)) from exc


class ProductionMediaStage:
    def __init__(self, client: Any, config: Any, root: Path, resolver: Callable[..., Any] | None = None):
        self.client, self.config, self.root, self.resolver = client, config, Path(root), resolver

    def __call__(self, context: dict[str, Any], state: Any, editorial: dict[str, Any]) -> MediaProgress:
        previous = state.media
        required = required_image_count(
            word_count(str(editorial.get("cleaned_html") or "")),
            title=str(context.get("title") or ""),
            content=str(editorial.get("cleaned_html") or ""),
        )
        accepted = {item.media_id: item for item in previous.inline}
        featured = previous.featured
        try:
            if self.resolver is not None:
                result = self.resolver(context, state, editorial, previous)
                if isinstance(result, MediaProgress):
                    media = result
                else:
                    media = self._from_result(result, required, accepted, featured)
            else:
                plan = list(editorial.get("media_plan") or [])
                if featured.status is FeaturedStatus.VALID:
                    plan = [item for item in plan if not item.get("is_featured")]
                plan = [item for item in plan if item.get("is_featured") or int(item.get("paragraph_index", -1)) not in {x.slot for x in accepted.values()}]
                checked = validate_media_plan(self.client, {**editorial, "media_plan": plan}, config=self.config, root=self.root, post_title=str(context.get("title") or ""), existing_featured_id=featured.media_id if featured.status is FeaturedStatus.VALID else None, post_id=int(context["post_id"]))
                results, featured_id, featured_credit = _execute_media_plan({**editorial, "media_plan": plan}, self.config, self.client, self.root, preflight=checked, post_id=int(context["post_id"]))
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
                    )
                    for row in results
                    if row.get("status") in {"accepted", "ok"}
                    and row.get("media_id")
                    and not row.get("featured")
                )
                inline = tuple(accepted.values()) + tuple(item for item in inline if item.media_id not in accepted)
                fp = FeaturedProgress(FeaturedStatus.VALID, featured_id, str(next((row.get("media_url") for row in results if row.get("featured") and row.get("media_id")), "") or "")) if featured_id else featured
                media = MediaProgress(required=required, inline=inline, featured=fp)
            _write_json(self.root, int(context["post_id"]), "editorial.partial.json", media.to_dict())
            return media
        except StageError:
            raise
        except Exception as exc:
            raise StageError(BlockerCode.MEDIA_INVALID, Phase.MEDIA, str(exc)) from exc

    @staticmethod
    def _from_result(result: Any, required: int, accepted: dict[int, InlineMedia], featured: FeaturedProgress) -> MediaProgress:
        rows = result if isinstance(result, list) else (result.get("inline") or [])
        inline = tuple(accepted.values()) + tuple(InlineMedia.from_dict(row) for row in rows if int(row.get("media_id", 0)) not in accepted)
        return MediaProgress(required=required, inline=inline, featured=featured)


class ProductionComposeStage:
    def __init__(self, config: Any, root: Path):
        self.config, self.root = config, Path(root)

    def __call__(self, context: dict[str, Any], editorial: dict[str, Any], media: MediaProgress) -> dict[str, Any]:
        try:
            is_listicle = detect_list_format(
                str(context.get("title") or ""),
                str(editorial.get("cleaned_html") or ""),
            ) is not None
            placements: list[dict[str, Any]] = [
                {
                    "paragraph_index": (
                        item.section_slot
                        if is_listicle and item.section_slot is not None
                        else (index if is_listicle else item.slot)
                    ),
                    "media_url": item.media_url,
                    "alt_text": item.alt_text,
                    "credit_text": item.credit_text,
                    "width": item.width,
                    "height": item.height,
                }
                for index, item in enumerate(media.inline)
            ]
            working = dict(editorial)
            working["cleaned_html"] = insert_media(
                str(editorial["cleaned_html"]),
                placements,
                listicle=is_listicle,
            ) if placements else str(editorial["cleaned_html"])
            content, trailer, trailer_status = compose_final_content(working, self.config, context.get("original_link"), root=self.root)
            working = attach_trailer_audit(working, trailer, search_status=trailer_status)
            content = normalize_editorial_dashes(content)
            candidate = {"content": content, "editorial": working, "seo": working.get("seo") or {}, "featured_media": media.featured.media_id, "media": media.to_dict(), "trailer": trailer, "trailer_status": trailer_status}
            _write_json(self.root, int(context["post_id"]), "editorial.candidate.json", candidate)
            return candidate
        except Exception as exc:
            raise StageError(BlockerCode.MANIFEST_INVALID, Phase.COMPOSE, str(exc)) from exc


class ProductionValidateStage:
    def __init__(self, client: Any, config: Any, root: Path):
        self.client, self.config, self.root = client, config, Path(root)

    def __call__(self, context: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
        try:
            post = _post(context)
            checklist_editorial = dict(candidate["editorial"])
            checklist_editorial.pop("decision", None)
            candidate["content"] = normalize_editorial_dashes(candidate.get("content", ""))
            checklist_editorial["cleaned_html"] = normalize_editorial_dashes(checklist_editorial.get("cleaned_html", ""))
            candidate["editorial"]["cleaned_html"] = checklist_editorial["cleaned_html"]
            _persist_draft_repairs(self.root, int(context["post_id"]), normalize_dashes=True)
            _write_json(self.root, int(context["post_id"]), "editorial.candidate.json", candidate)
            post_for_checklist = dict(post)
            candidate_featured = candidate.get("featured_media")
            if isinstance(candidate_featured, int) and candidate_featured > 0:
                post_for_checklist["featured_media"] = candidate_featured
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
            failures = []
            for item in checklist.get("items", []):
                if item.get("status") != "fail":
                    continue
                failure = {"gate": item["name"], "detail": item.get("detail", "")}
                if item.get("invalid_media"):
                    failure["invalid_media"] = item["invalid_media"]
                failures.append(failure)
            _write_json(self.root, int(context["post_id"]), "editorial.validation.json", {"passed": bool(checklist.get("all_passed")), "failures": failures, "checklist": checklist})
            return {"passed": bool(checklist.get("all_passed")), "failures": failures, "checklist": checklist}
        except Exception as exc:
            raise StageError(BlockerCode.MANIFEST_INVALID, Phase.VALIDATE, str(exc)) from exc


__all__ = ["ProductionEditorialStage", "ProductionMediaStage", "ProductionComposeStage", "ProductionValidateStage"]
