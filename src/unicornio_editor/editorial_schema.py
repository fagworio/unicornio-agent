"""Strict validation for the JSON produced by the editorial model."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse


class EditorialValidationError(ValueError):
    """Raised when an editorial result cannot be safely applied."""


_TOP_LEVEL = {
    "site_relevance",
    "cleaned_html",
    "title",
    "seo",
    "media_plan",
    "needs_trailer",
    "trailer_url",
    "game_name",
    # Evidência gerada pelo código após uma busca oficial sem resultado. O
    # modelo não pode declarar apenas o waiver: ambos os campos são validados.
    "trailer_unavailable",
    "trailer_search_evidence",
    # Busca de imagens esgotada (media-search-web devolveu count=0):
    # autoriza o waiver do minimo de imagens inline (artigo publica com
    # featured + texto). Listicle NAO dispensa — vai para awaiting_human.
    "media_exhausted",
}
_RELEVANCE = {"decision", "confidence", "reason", "matched_topics"}
_SEO = {"title", "meta_description", "focus_keyword"}
_MEDIA = {
    "paragraph_index",
    # Deterministic placement emitted by the media resolver when the final
    # inline image is safely attached to the end of the editorial body.
    "placement",
    "source_page_url",
    "direct_image_url",
    "alt_text",
    # Optional editorial caption; never used as proof of ownership or a gate.
    "credit_text",
    "is_featured",
    # Reuse of an existing Media Library attachment: the image is downloaded
    # from this attachment and re-uploaded as a NEW attachment (the original
    # title/alt/caption are never overwritten).
    "media_library_id",
    # Evidência determinística da descoberta (o apply RECALCULA e não confia
    # nestes campos — eles existem para auditoria e para o índice local).
    "subject",
    "target_id",
    "item_identity",
    "item_title",
    "item_group",
    "item_number",
    "section_heading",
    "section_slot",
    "html_position",
    "phash",
    "evidence_score",
    "evidence_verdict",
    # Resolver provenance is retained across JSON, but apply independently
    # re-fetches and re-scores the origin page before accepting the asset.
    "origin_evidence",
    "candidate_id",
    # Rastreabilidade da DECISÃO de mídia que escolheu esta imagem: o
    # `decision_id` vem do ledger append-only (work/media_decisions.jsonl) que o
    # `media-search-web`/`-listicle` devolve no stdout. Sem ele, o media-validate
    # e o apply só conseguiam atribuir o resultado à ÚLTIMA decisão do post —
    # num item de lista (auto/choose/auto) isso culpava a decisão errada.
    "decision_id",
    "decision",
    "provenance_mode",
    "source_page_verified",
    # Query de descoberta que retornou esta imagem (ex.: "redfall xbox
    # series"). Evidencia de relevancia (fluxo manual do editor): se a busca
    # filtrada retornou a imagem, ela e o que se procura. O agente DEVE
    # declarar a query real usada; nunca inventar para "decorar" uma imagem
    # errada.
    "search_query",
}
_LEGACY_MEDIA_RIGHTS_FIELDS = {
    "author", "license", "license_url", "captured_at",
    "rights_verified", "rights_checked", "rights_decision", "rights_evidence",
    "source_rights_attestation",
}


def validate_editorial(
    payload: Mapping[str, Any],
    *,
    min_confidence: float = 0.8,
    media_source_policy: str = "strict",
) -> dict[str, Any]:
    if media_source_policy not in {"strict", "credit_only"}:
        raise EditorialValidationError("media_source_policy must be strict or credit_only")
    if not isinstance(payload, Mapping):
        raise EditorialValidationError("editorial result must be an object")
    _keys(
        payload,
        _TOP_LEVEL,
        "top-level",
        optional={
            "cleaned_html", "seo", "title", "media_exhausted",
            "trailer_unavailable", "trailer_search_evidence",
        },
    )
    relevance = _object(payload["site_relevance"], "site_relevance")
    _keys(relevance, _RELEVANCE, "site_relevance")
    decision = relevance["decision"]
    if decision not in {"process", "skip"}:
        raise EditorialValidationError("site_relevance.decision must be process or skip")
    confidence = _number(relevance["confidence"], "site_relevance.confidence")
    if not 0 <= confidence <= 1:
        raise EditorialValidationError("site_relevance.confidence must be between 0 and 1")
    if decision == "process" and confidence < min_confidence:
        raise EditorialValidationError("process decision is below the confidence threshold")
    _nonempty_string(relevance["reason"], "site_relevance.reason")
    if not isinstance(relevance["matched_topics"], list) or not all(
        isinstance(item, str) and item.strip() for item in relevance["matched_topics"]
    ):
        raise EditorialValidationError("site_relevance.matched_topics must be a list of strings")

    # cleaned_html is OPTIONAL: when absent or null the workflow reuses the
    # deterministic cleaned content of the prepared post (no-rewrite path).
    # This is the token-economy default: the model must not re-emit text it
    # did not change. CTA/Fonte/rodape are always code-inserted (builder).
    cleaned_html = payload.get("cleaned_html")
    if cleaned_html is not None and isinstance(cleaned_html, str) and cleaned_html.strip():
        # P0 (auditoria): o corpo do post não pode ser a SAÍDA de um comando do
        # CLI. O acidente do post 114180 publicou o envelope
        # {"post_id":..., "cleaned_html":...} como se fosse texto editorial.
        from .content_quality import looks_like_operational_envelope

        if looks_like_operational_envelope(cleaned_html):
            raise EditorialValidationError(
                "cleaned_html parece a saida de um comando do CLI (envelope JSON "
                "com post_id/cleaned_html) — envie o HTML editorial, nao o "
                "resultado de `content`/`queue`"
            )
    if cleaned_html is not None and not isinstance(cleaned_html, str):
        raise EditorialValidationError("cleaned_html must be a string or null")
    if decision == "process" and isinstance(cleaned_html, str) and not cleaned_html.strip():
        raise EditorialValidationError("cleaned_html cannot be empty when processing")

    title = payload.get("title")
    if title is not None:
        if not isinstance(title, str) or not title.strip():
            raise EditorialValidationError("title must be a non-empty string or null")
        if len(title.strip()) > 180:
            raise EditorialValidationError("title must contain at most 180 characters")

    # seo is OPTIONAL too: when absent the workflow inherits a valid existing
    # Rank Math meta (token economy — the model must not re-emit SEO the post
    # already has). When the post has no valid meta, apply fails with a clear
    # message telling the model to provide seo.
    if "seo" in payload and payload["seo"] is not None:
        seo = _object(payload["seo"], "seo")
        _keys(seo, _SEO, "seo")
        title = _nonempty_string(seo["title"], "seo.title")
        if len(title) > 65:
            raise EditorialValidationError("seo.title must contain at most 65 characters")
        description = _nonempty_string(seo["meta_description"], "seo.meta_description")
        if not 120 <= len(description) <= 160:
            raise EditorialValidationError("seo.meta_description must contain 120 to 160 characters")
        _nonempty_string(seo["focus_keyword"], "seo.focus_keyword")

    media_plan = payload["media_plan"]
    if not isinstance(media_plan, list) or len(media_plan) > 12:
        raise EditorialValidationError("media_plan must contain between 0 and 12 items")
    normalized_media = []
    featured_count = 0
    for index, item in enumerate(media_plan):
        media = _object(item, f"media_plan[{index}]")
        media_optional = {
            "media_library_id", "search_query", "subject", "phash",
            "evidence_score", "evidence_verdict", "candidate_id",
            "origin_evidence",
            "credit_text",
            "provenance_mode",
            "source_page_verified",
            "target_id", "item_identity", "item_title", "item_group",
            "item_number", "section_heading", "section_slot", "html_position",
            # Rastreabilidade opcional (vem do ledger de decisões); o apply
            # NUNCA confia nela para decidir, só para medir qualidade.
            "decision_id", "decision",
            "placement",
            *_LEGACY_MEDIA_RIGHTS_FIELDS,
        }
        if media_source_policy == "credit_only":
            media_optional.add("source_page_url")
        _keys(
            media, _MEDIA | _LEGACY_MEDIA_RIGHTS_FIELDS, f"media_plan[{index}]",
            # Campos de evidência são OPCIONAIS: o apply recalcula o score e não
            # confia no JSON (um media_plan manual/adulterado não pode passar
            # apenas por preencher evidence_score).
            optional=media_optional,
        )
        paragraph_index = media["paragraph_index"]
        if isinstance(paragraph_index, bool) or not isinstance(paragraph_index, int) or paragraph_index < 0:
            raise EditorialValidationError(f"media_plan[{index}].paragraph_index must be non-negative")
        placement = media.get("placement")
        if placement is not None and placement not in {
            "after_editorial_body", "after_list_item_description",
        }:
            raise EditorialValidationError(
                f"media_plan[{index}].placement has unsupported value"
            )
        list_fields = {
            "item_identity", "item_title", "item_group",
            "item_number", "section_heading", "section_slot", "html_position",
        }
        if any(key in media for key in list_fields):
            for name in ("target_id", "item_identity", "item_title", "section_heading"):
                _nonempty_string(media.get(name), f"media_plan[{index}].{name}")
            if "item_group" in media and not isinstance(media["item_group"], str):
                raise EditorialValidationError(f"media_plan[{index}].item_group must be a string")
            item_number = media.get("item_number")
            if item_number is not None and (
                isinstance(item_number, bool) or not isinstance(item_number, int) or item_number < 1
            ):
                raise EditorialValidationError(f"media_plan[{index}].item_number must be positive or null")
            for name in ("section_slot", "html_position"):
                value = media.get(name)
                if value is not None and (
                    isinstance(value, bool) or not isinstance(value, int) or value < 0
                ):
                    raise EditorialValidationError(f"media_plan[{index}].{name} must be non-negative")
        if placement == "after_list_item_description" and not all(
            str(media.get(key) or "").strip()
            for key in ("target_id", "item_identity", "item_title", "section_heading")
        ):
            raise EditorialValidationError(
                f"media_plan[{index}] list placement requires item identity and heading"
            )
        _nonempty_string(media["alt_text"], f"media_plan[{index}].alt_text")
        _http_url(media["direct_image_url"], f"media_plan[{index}].direct_image_url")
        source_page_url = str(media.get("source_page_url") or "").strip()
        credit_text = str(media.get("credit_text") or "").strip()
        if media_source_policy == "credit_only":
            # The page is a non-authoritative diagnostic under this policy; it
            # is discarded below and never fetched or used as an acceptance gate.
            if not credit_text:
                from .media.text import credit_text_with_hosting_domain
                credit_text = credit_text_with_hosting_domain(
                    "", str(media["direct_image_url"])
                )
            _nonempty_string(credit_text, f"media_plan[{index}].credit_text")
            provenance_mode = "credit_only"
        elif source_page_url:
            _http_url(source_page_url, f"media_plan[{index}].source_page_url")
            provenance_mode = "source_page"
        else:
            raise EditorialValidationError(
                f"media_plan[{index}].source_page_url is required by strict media source policy"
            )
        is_featured = media["is_featured"]
        if not isinstance(is_featured, bool):
            raise EditorialValidationError(f"media_plan[{index}].is_featured must be boolean")
        if is_featured:
            featured_count += 1
        # Accept old plans so checkpoints and saved drafts remain readable,
        # but do not carry legal-validation fields into the active plan.
        normalized = {
            key: value for key, value in media.items()
            if key not in _LEGACY_MEDIA_RIGHTS_FIELDS
        }
        if media.get("origin_evidence") is not None:
            normalized["origin_evidence"] = _normalize_origin_evidence(
                media["origin_evidence"], f"media_plan[{index}].origin_evidence"
            )
        if media_source_policy == "credit_only":
            normalized["provenance_mode"] = provenance_mode
            normalized["source_page_verified"] = False
            normalized["rights_verified"] = False
            normalized["credit_text"] = credit_text
            normalized.pop("source_page_url", None)
        elif source_page_url:
            normalized.pop("provenance_mode", None)
            normalized["source_page_url"] = source_page_url
        else:
            normalized.pop("source_page_url", None)
        normalized_media.append(normalized)
    if featured_count > 1:
        raise EditorialValidationError("media_plan must contain at most one featured image")

    needs_trailer = payload["needs_trailer"]
    if not isinstance(needs_trailer, bool):
        raise EditorialValidationError("needs_trailer must be boolean")
    trailer_url = payload["trailer_url"]
    if needs_trailer:
        _http_url(trailer_url, "trailer_url")
    elif trailer_url is not None:
        raise EditorialValidationError("trailer_url must be null when needs_trailer is false")

    game_name = payload["game_name"]
    if game_name is not None and (not isinstance(game_name, str) or not game_name.strip()):
        raise EditorialValidationError("game_name must be null or a non-empty string")
    if needs_trailer and not game_name:
        raise EditorialValidationError("needs_trailer requires game_name")

    trailer_unavailable = payload.get("trailer_unavailable", False)
    if not isinstance(trailer_unavailable, bool):
        raise EditorialValidationError("trailer_unavailable must be boolean")
    evidence = payload.get("trailer_search_evidence")
    if trailer_unavailable:
        if not game_name:
            raise EditorialValidationError("trailer_unavailable requires game_name")
        evidence_obj = _object(evidence, "trailer_search_evidence")
        _keys(
            evidence_obj,
            {"query", "provider", "searched_at", "result"},
            "trailer_search_evidence",
        )
        for name in ("query", "provider", "searched_at"):
            _nonempty_string(evidence_obj[name], f"trailer_search_evidence.{name}")
        if evidence_obj["result"] != "official_not_found":
            raise EditorialValidationError(
                "trailer_search_evidence.result must be official_not_found"
            )
    elif evidence is not None:
        raise EditorialValidationError(
            "trailer_search_evidence requires trailer_unavailable=true"
        )

    media_exhausted = payload.get("media_exhausted", False)
    if media_exhausted is not None and not isinstance(media_exhausted, bool):
        raise EditorialValidationError("media_exhausted must be a boolean or null")

    result = dict(payload)
    result["media_plan"] = normalized_media
    return result


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EditorialValidationError(f"{name} must be an object")
    return value


def _keys(value: Mapping[str, Any], expected: set[str], name: str, optional: set[str] | None = None) -> None:
    actual = set(value)
    required = expected - (optional or set())
    missing = required - actual
    unknown = actual - expected
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing={sorted(missing)}")
        if unknown:
            details.append(f"unknown={sorted(unknown)}")
        raise EditorialValidationError(f"{name} has invalid fields ({', '.join(details)})")


def _nonempty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EditorialValidationError(f"{name} must be a non-empty string")
    return value


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EditorialValidationError(f"{name} must be a number")
    return float(value)


def _http_url(value: Any, name: str) -> str:
    _nonempty_string(value, name)
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
        parsed.port  # validate syntax and range of an optional port
    except ValueError as exc:
        raise EditorialValidationError(f"{name} must be a valid HTTP(S) URL") from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise EditorialValidationError(f"{name} must be an absolute HTTP(S) URL")
    return value


def _normalize_origin_evidence(value: Any, name: str) -> dict[str, Any]:
    evidence = _object(value, name)
    evidence_keys = {"verdict", "score", "local_score", "subject", "matched", "fields"}
    _keys(evidence, evidence_keys, name, optional=evidence_keys - {"fields"})
    fields_value = _object(evidence["fields"], f"{name}.fields")
    allowed_fields = {
        "filename", "og_title", "page_title", "alt_original",
        "figcaption", "heading", "page_url",
    }
    _keys(fields_value, allowed_fields, f"{name}.fields", optional=allowed_fields)
    fields: dict[str, str] = {}
    for key, raw in fields_value.items():
        if not isinstance(raw, str) or not raw.strip() or len(raw) > 500:
            raise EditorialValidationError(f"{name}.fields.{key} must be a non-empty string up to 500 chars")
        fields[key] = _http_url(raw, f"{name}.fields.page_url") if key == "page_url" else raw
    if not fields:
        raise EditorialValidationError(f"{name}.fields must contain at least one source field")

    normalized: dict[str, Any] = {"fields": fields}
    verdict = evidence.get("verdict")
    if verdict is not None:
        if not isinstance(verdict, str) or not verdict.strip() or len(verdict) > 48:
            raise EditorialValidationError(f"{name}.verdict must be a non-empty string up to 48 chars")
        normalized["verdict"] = verdict
    subject = evidence.get("subject")
    if subject is not None:
        if not isinstance(subject, str) or not subject.strip() or len(subject) > 200:
            raise EditorialValidationError(f"{name}.subject must be a non-empty string up to 200 chars")
        normalized["subject"] = subject
    matched = evidence.get("matched")
    if matched is not None:
        if (
            not isinstance(matched, list)
            or len(matched) > len(allowed_fields)
            or any(not isinstance(item, str) or item not in allowed_fields for item in matched)
        ):
            raise EditorialValidationError(f"{name}.matched contains invalid source field names")
        normalized["matched"] = matched
    for key in ("score", "local_score"):
        raw = evidence.get(key)
        if raw is not None:
            score = _number(raw, f"{name}.{key}")
            if not math.isfinite(score):
                raise EditorialValidationError(f"{name}.{key} must be finite")
            normalized[key] = score
    return normalized
