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
from ..editorial_provider import EditorialProviderError, generate_editorial_batch
from ..editorial_schema import validate_editorial
from ..media.inserter import insert_media
from ..workflow import (
    _execute_media_plan,
    compose_final_content,
    resolve_editorial_defaults,
    validate_media_plan,
)
from .errors import StageError
from .model import BlockerCode, FeaturedProgress, FeaturedStatus, InlineMedia, MediaProgress, Phase


def _write_json(root: Path, post_id: int, name: str, value: dict[str, Any]) -> Path:
    target = root / "backups" / str(post_id) / name
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)
    return target


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
                raise StageError(BlockerCode.PROVIDER_ERROR, Phase.EDITORIAL, str((item or {}).get("reason") or "editorial provider requested retry"))
            editorial = resolve_editorial_defaults(dict(item["editorial"]), _post(context))
            editorial = validate_editorial(editorial, min_confidence=self.config.min_relevance_confidence)
            editorial["decision"] = (editorial.get("site_relevance") or {}).get("decision")
            _write_json(self.root, post_id, "editorial.draft.json", editorial)
            return editorial
        except StageError:
            raise
        except EditorialProviderError as exc:
            raise StageError(BlockerCode.PROVIDER_ERROR, Phase.EDITORIAL, str(exc)) from exc
        except Exception as exc:
            raise StageError(BlockerCode.MANIFEST_INVALID, Phase.EDITORIAL, str(exc)) from exc


class ProductionMediaStage:
    def __init__(self, client: Any, config: Any, root: Path, resolver: Callable[..., Any] | None = None):
        self.client, self.config, self.root, self.resolver = client, config, Path(root), resolver

    def __call__(self, context: dict[str, Any], state: Any, editorial: dict[str, Any]) -> MediaProgress:
        previous = state.media
        required = previous.required
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
                checked = validate_media_plan(self.client, {**editorial, "media_plan": plan}, config=self.config, root=self.root, post_title=str(context.get("title") or ""), existing_featured_id=featured.media_id if featured.status is FeaturedStatus.VALID else None)
                results, featured_id, featured_credit = _execute_media_plan({**editorial, "media_plan": plan}, self.config, self.client, self.root, preflight=checked, post_id=int(context["post_id"]))
                inline = tuple(InlineMedia(int(row["media_id"]), str(row["media_url"]), int(row.get("paragraph_index", 0)), str(row.get("alt_text", "")), str(row.get("credit_text", ""))) for row in results if row.get("status") in {"accepted", "ok"} and row.get("media_id") and not row.get("featured"))
                inline = tuple(accepted.values()) + tuple(item for item in inline if item.media_id not in accepted)
                fp = FeaturedProgress(FeaturedStatus.VALID, featured_id, str(next((row.get("media_url") for row in results if row.get("featured") and row.get("media_id")), "") or "")) if featured_id else featured
                media = MediaProgress(required=max(required, len(inline)), inline=inline, featured=fp)
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
        return MediaProgress(required=max(required, len(inline)), inline=inline, featured=featured)


class ProductionComposeStage:
    def __init__(self, config: Any, root: Path):
        self.config, self.root = config, Path(root)

    def __call__(self, context: dict[str, Any], editorial: dict[str, Any], media: MediaProgress) -> dict[str, Any]:
        try:
            placements: list[dict[str, Any]] = [{"paragraph_index": item.slot, "media_url": item.media_url, "alt_text": item.alt_text, "credit_text": item.credit_text, "width": 1200, "height": 800} for item in media.inline]
            working = dict(editorial)
            working["cleaned_html"] = insert_media(str(editorial["cleaned_html"]), placements, listicle=bool(editorial.get("listicle"))) if placements else str(editorial["cleaned_html"])
            content, trailer, trailer_status = compose_final_content(working, self.config, context.get("original_link"), root=self.root)
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
            checklist = run_pre_publish_checklist(post=post, editorial=checklist_editorial, content=str(candidate["content"]), backup_path=self.root / "backups" / str(context["post_id"]) / "editorial.draft.json", config=self.config, client=self.client, attempts=int((context.get("v2_state").retry.attempts if context.get("v2_state") else 0)))
            return {"passed": bool(checklist.get("all_passed")), "failures": [{"gate": item["name"]} for item in checklist.get("items", []) if item.get("status") == "fail"], "checklist": checklist}
        except Exception as exc:
            raise StageError(BlockerCode.MANIFEST_INVALID, Phase.VALIDATE, str(exc)) from exc


__all__ = ["ProductionEditorialStage", "ProductionMediaStage", "ProductionComposeStage", "ProductionValidateStage"]
