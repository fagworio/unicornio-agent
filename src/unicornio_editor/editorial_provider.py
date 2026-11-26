"""Direct, single-request editorial generation for a prepared microbatch.

This module deliberately does not use the Hermes tool loop.  It sends one
OpenAI-compatible chat-completions request, asks for a structured batch
response, validates every result with the existing editorial schema and writes
an output envelope that ``apply-batch`` can consume.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import socket
import tempfile
from functools import wraps
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .ai_provider import AIProviderError, call_hermes_oneshot, hermes_command_available
from .batch import (
    BATCH_SCHEMA_VERSION,
    BatchError,
    batch_directory,
    build_media_resolve_batch_input,
    load_editorial_batch,
    validate_batch_id,
)
from .editorial_schema import EditorialValidationError, validate_editorial
from .retry_policy import (
    clear_expired_editorial_backoff,
    editorial_backoff_status,
    editorial_provider_request_lock,
    parse_retry_after_seconds,
    record_editorial_backoff,
)


class EditorialProviderError(RuntimeError):
    """Raised when the direct editorial provider call cannot be trusted."""

    def __init__(
        self,
        message: str,
        *,
        failure_class: str = "editorial_schema_invalid",
        retryable: bool = True,
        http_status: int | None = None,
        retry_after_seconds: int | None = None,
        retry_at: str | None = None,
        resume_batch_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.retryable = bool(retryable)
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds
        self.retry_at = retry_at
        self.resume_batch_id = resume_batch_id


_RELEVANCE_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["process", "skip"]},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
        "matched_topics": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["decision", "confidence", "reason", "matched_topics"],
    "additionalProperties": False,
}

_MEDIA_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "paragraph_index": {"type": "integer"},
        "direct_image_url": {"type": "string"},
        "credit_text": {"type": "string", "minLength": 1},
        "alt_text": {"type": "string"},
        "is_featured": {"type": "boolean"},
    },
    "required": [
        "paragraph_index",
        "direct_image_url",
        "credit_text",
        "alt_text",
        "is_featured",
    ],
    "additionalProperties": False,
}

_SEO_SCHEMA = {
    "type": ["object", "null"],
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 65},
        "meta_description": {"type": "string", "minLength": 120, "maxLength": 160},
        "focus_keyword": {"type": "string", "minLength": 1},
    },
    "required": ["title", "meta_description", "focus_keyword"],
    "additionalProperties": False,
}

# Contrato editorial como schema ESTRITO. Structured Outputs (strict=true) exige
# additionalProperties:false em todo objeto e que `required` liste TODAS as
# propriedades declaradas — por isso os campos opcionais do contrato
# (cleaned_html/seo) sao declarados como nullable e devem vir como null quando
# nao houver mudanca real. Sem isso a OpenAI rejeita a requisicao inteira com
# HTTP 400 ("additionalProperties is required to be supplied and to be false").
_EDITORIAL_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": ["string", "null"], "maxLength": 180},
        "site_relevance": _RELEVANCE_SCHEMA,
        "media_plan": {"type": "array", "items": _MEDIA_ITEM_SCHEMA},
        "needs_trailer": {"type": "boolean"},
        "trailer_url": {"type": ["string", "null"]},
        "game_name": {"type": ["string", "null"]},
        "cleaned_html": {"type": ["string", "null"]},
        "seo": _SEO_SCHEMA,
    },
    "required": [
        "title",
        "site_relevance",
        "media_plan",
        "needs_trailer",
        "trailer_url",
        "game_name",
        "cleaned_html",
        "seo",
    ],
    "additionalProperties": False,
}

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "batch_id": {"type": "string"},
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "post_id": {"type": "integer"},
                    "status": {"type": "string", "enum": ["ok", "needs_retry"]},
                    "reason": {"type": "string"},
                    "retry_kind": {"type": "string", "enum": ["none", "text", "seo", "relevance", "facts", "media"]},
                    "editorial": _EDITORIAL_SCHEMA,
                },
                "required": ["post_id", "status", "reason", "retry_kind", "editorial"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["batch_id", "results"],
    "additionalProperties": False,
}


def _output_schema_for_policy(media_source_policy: str) -> dict[str, Any]:
    if media_source_policy not in {"strict", "credit_only"}:
        raise EditorialProviderError("media_source_policy must be strict or credit_only", retryable=False)
    schema = copy.deepcopy(_OUTPUT_SCHEMA)
    if media_source_policy == "strict":
        media_item = schema["properties"]["results"]["items"]["properties"]["editorial"]["properties"]["media_plan"]["items"]
        media_item["properties"]["source_page_url"] = {"type": "string", "minLength": 1}
        media_item["required"].append("source_page_url")
    return schema

_SYSTEM_PROMPT = """You are the UnicornioHater editorial transformation service.
You receive a prepared JSON envelope containing one or two independent posts.
Return ONLY the requested structured JSON object. Do not call tools, browse,
search, invent facts, or combine facts between posts. Use post_id as the only
correlation key. If one post cannot be safely completed, return
status=needs_retry and a concise reason for that post; still return the other
post when it is valid. For status=ok, editorial must follow the existing
editorial contract: title, site_relevance, media_plan, needs_trailer,
trailer_url and game_name are required fields. For a non-localized post, title
must be null unless the visible title really needs changing; cleaned_html and
seo must be null unless a real change is required — never re-emit text or SEO
the post already carries.

Neither images nor the trailer are resolved in this call: both are handled
deterministically afterwards by the code. So: always return "media_plan": [];
never invent image URLs, captions or provenance; never answer
needs_retry because of missing inline images, a missing featured image or any
image count; and return needs_trailer=false with trailer_url=null unless the
envelope already carries a verified trailer URL. Do fill game_name when the
subject is a game. If multiple distinct games are explicitly discussed, return
semicolon-delimited game names rather than merging their identities; game_name triggers deterministic trailer lookup, and needs_trailer is not a search switch.
Use needs_retry only when the text/SEO facts themselves
cannot be handled safely. When decision=process, matched_topics MUST contain
one or more values copied exactly from relevance_policy.allowed_topics. Do not
return synonyms, subcategories, translations or derived labels in place of the
allowed labels. When a post contains a rework object, treat previous_editorial as
its baseline, correct specifically the failed_gates, preserve fields that do not
need changes, and attempt the requested correction. If a retry is needed, set
retry_kind to text, seo, relevance, facts, or media. Use retry_kind=media only
when the editorial payload itself is valid and the remaining media work is
deterministic; never use a media retry to block a valid editorial.

When localization.required is true, the source article is unequivocally in
English. Return a natural Brazilian-Portuguese rewrite: title, cleaned_html
and seo (title, meta_description and focus_keyword) must be populated and may
not be null. Preserve official names, links, credits, embeds and factual
uncertainty. When localization.required is false, keep the token-economy
contract: those fields may be null and valid existing Portuguese fields may be
preserved by the pipeline."""

def _read_input(path: Path | str) -> tuple[str, list[dict[str, Any]], str]:
    source = Path(path)
    try:
        source_bytes = source.read_bytes()
        payload = json.loads(source_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EditorialProviderError(
            "entrada editorial ausente ou JSON inválido",
            failure_class="request_input_invalid",
            retryable=False,
        ) from exc
    if not isinstance(payload, dict):
        raise EditorialProviderError("entrada editorial precisa ser um objeto JSON", failure_class="request_input_invalid", retryable=False)
    batch_id = validate_batch_id(str(payload.get("batch_id") or ""))
    posts = payload.get("posts")
    if not isinstance(posts, list) or not 1 <= len(posts) <= 2:
        raise EditorialProviderError("entrada editorial precisa conter de 1 a 2 posts", failure_class="request_input_invalid", retryable=False)
    ids: set[int] = set()
    for item in posts:
        if not isinstance(item, dict):
            raise EditorialProviderError("cada post da entrada editorial precisa ser um objeto", failure_class="request_input_invalid", retryable=False)
        post_id = item.get("post_id")
        if isinstance(post_id, bool) or not isinstance(post_id, int) or post_id <= 0:
            raise EditorialProviderError("post_id invalido na entrada editorial", failure_class="request_input_invalid", retryable=False)
        if post_id in ids:
            raise EditorialProviderError(f"post_id duplicado na entrada editorial: {post_id}", failure_class="request_input_invalid", retryable=False)
        ids.add(post_id)
    return batch_id, posts, hashlib.sha256(source_bytes).hexdigest()


def _response_text(body: dict[str, Any]) -> str:
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise EditorialProviderError("resposta editorial sem choices[0].message.content", failure_class="invalid_response") from exc
    if isinstance(content, list):
        content = "".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    if not isinstance(content, str) or not content.strip():
        raise EditorialProviderError("resposta editorial vazia", failure_class="invalid_response")
    return content.strip()


def _parse_json(text: str) -> dict[str, Any]:
    text = str(text or "").strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EditorialProviderError("resposta editorial nao e JSON", failure_class="invalid_response") from exc
    if not isinstance(value, dict):
        raise EditorialProviderError("resposta editorial precisa ser um objeto", failure_class="invalid_response")
    return value


def _verified_model_identity(usage: Any) -> str:
    """Return only a model name explicitly reported by the provider."""
    if not isinstance(usage, dict):
        return ""
    for key in ("model", "model_name", "model_id", "actual_model"):
        value = usage.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:160]
    for key in ("response", "metadata", "provider"):
        nested = usage.get(key)
        found = _verified_model_identity(nested)
        if found:
            return found
    return ""


def _normalize_output(
    payload: dict[str, Any],
    *,
    batch_id: str,
    post_ids: set[int],
    min_confidence: float,
    relevance_policies: dict[int, set[str]] | None = None,
    localization_requirements: dict[int, bool] | None = None,
    media_source_policy: str = "strict",
) -> dict[str, Any]:
    if set(payload) != {"batch_id", "results"}:
        raise EditorialProviderError("resposta editorial possui campos inesperados")
    if payload.get("batch_id") != batch_id:
        raise EditorialProviderError("batch_id da resposta editorial nao corresponde a entrada")
    results = payload.get("results")
    if not isinstance(results, list) or len(results) != len(post_ids):
        raise EditorialProviderError("resposta editorial precisa conter um resultado por post")
    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()
    for index, item in enumerate(results):
        if not isinstance(item, dict):
            raise EditorialProviderError(f"results[{index}] invalido")
        post_id = item.get("post_id")
        if isinstance(post_id, bool) or not isinstance(post_id, int) or post_id not in post_ids:
            raise EditorialProviderError(f"results[{index}].post_id invalido")
        if post_id in seen:
            raise EditorialProviderError(f"post_id duplicado na resposta: {post_id}")
        status = item.get("status")
        reason = str(item.get("reason") or "").strip()
        if status not in {"ok", "needs_retry"}:
            raise EditorialProviderError(f"status editorial invalido para {post_id}: {status!r}")
        retry_kind = str(item.get("retry_kind") or "none")
        if status == "needs_retry" and retry_kind != "media":
            normalized.append({
                "post_id": post_id,
                "status": status,
                "reason": reason or "retry solicitado",
                "retry_kind": retry_kind,
                "failure_class": "editorial_content_retry",
                "retryable": True,
            })
        else:
            editorial = item.get("editorial")
            if isinstance(editorial, str):
                # Structured Outputs (strict=true) nao aceita objeto livre: o
                # contrato editorial chega serializado numa string. String
                # invalida isola o post como needs_retry sem derrubar o batch.
                try:
                    editorial = json.loads(editorial)
                except json.JSONDecodeError as exc:
                    normalized.append({
                        "post_id": post_id,
                        "status": "needs_retry",
                        "reason": f"editorial invalido: string nao e JSON ({exc})",
                        "retry_kind": "text",
                        "failure_class": "invalid_response",
                        "retryable": True,
                        "http_status": None,
                    })
                    seen.add(post_id)
                    continue
            if not isinstance(editorial, dict):
                normalized.append({
                    "post_id": post_id,
                    "status": "needs_retry",
                    "reason": reason or "editorial ausente",
                    "retry_kind": "text",
                    "failure_class": "invalid_response",
                    "retryable": True,
                })
                seen.add(post_id)
                continue
            normalizations: list[str] = []
            # O trailer e descoberto de forma DETERMINISTICA pelo codigo (por
            # `game_name`, em compose_final_content): nao existe busca de
            # trailer nesta chamada sem ferramentas. Se o modelo sinalizou
            # needs_trailer sem uma URL verificada, o contrato rejeitaria o item
            # inteiro; normalizamos para false preservando game_name, e a
            # descoberta continua acontecendo no apply.
            if editorial.get("needs_trailer") and not editorial.get("trailer_url"):
                editorial = {**editorial, "needs_trailer": False, "trailer_url": None}
                normalizations.append("needs_trailer_without_url_cleared")
            elif editorial.get("needs_trailer") is False and editorial.get("trailer_url") is not None:
                # A URL de trailer só é válida quando o contrato declara que
                # há trailer. Limpar a inconsistência é seguro e evita gastar
                # uma tentativa operacional; o schema continua estrito para
                # todos os demais casos.
                editorial = {**editorial, "trailer_url": None}
                normalizations.append("trailer_url_cleared_when_needs_trailer_false")
            if status == "needs_retry" and retry_kind == "media":
                editorial = {**editorial, "media_plan": []}
            localize = bool((localization_requirements or {}).get(post_id))
            missing_localization_fields: list[str] = []
            if localize:
                if not isinstance(editorial.get("title"), str) or not editorial["title"].strip():
                    missing_localization_fields.append("title")
                if not isinstance(editorial.get("cleaned_html"), str) or not editorial["cleaned_html"].strip():
                    missing_localization_fields.append("cleaned_html")
                seo_payload = editorial.get("seo")
                if not isinstance(seo_payload, dict) or any(
                    not isinstance(seo_payload.get(name), str)
                    or not str(seo_payload.get(name)).strip()
                    for name in ("title", "meta_description", "focus_keyword")
                ):
                    missing_localization_fields.append("seo")
            if missing_localization_fields and status == "ok":
                normalized.append({
                    "post_id": post_id,
                    "status": "needs_retry",
                    "reason": "localizacao_pt_br_incompleta: " + ", ".join(missing_localization_fields),
                    "retry_kind": "text",
                    "failure_class": "editorial_schema_invalid",
                    "retryable": True,
                    "http_status": None,
                    "normalizations": normalizations,
                })
                seen.add(post_id)
                continue
            try:
                checked = validate_editorial(
                    editorial,
                    min_confidence=min_confidence,
                    media_source_policy=media_source_policy,
                )
                allowed_topics = set((relevance_policies or {}).get(post_id) or ())
                matched_topics = {
                    str(topic).strip().casefold()
                    for topic in (checked.get("site_relevance", {}).get("matched_topics") or [])
                    if str(topic).strip()
                }
                if (
                    checked["site_relevance"]["decision"] == "process"
                    and allowed_topics
                    and not allowed_topics.intersection(matched_topics)
                ):
                    raise EditorialValidationError(
                        "process sem matched_topics permitido pela pauta"
                    )
            except EditorialValidationError as exc:
                normalized.append({
                    "post_id": post_id,
                    "status": "needs_retry",
                    "reason": f"editorial invalido: {exc}",
                    "retry_kind": "text",
                    "failure_class": "editorial_schema_invalid",
                    "retryable": True,
                    "http_status": None,
                    "normalizations": normalizations,
                })
            else:
                normalized.append({
                    "post_id": post_id,
                    "status": "ok",
                    "reason": reason,
                    "retry_kind": retry_kind,
                    "editorial": checked,
                    "normalizations": normalizations,
                })
        seen.add(post_id)
    if seen != post_ids:
        raise EditorialProviderError("resposta editorial nao cobre exatamente os posts de entrada")
    return {"schema_version": BATCH_SCHEMA_VERSION, "batch_id": batch_id, "results": normalized}


def _write_json(path: Path, payload: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return str(path)


def _editorial_output_integrity_path(destination: Path) -> Path:
    return destination.with_name(f"{destination.name}.integrity.json")


def _write_editorial_output_integrity(
    destination: Path,
    *,
    batch_id: str,
    input_sha256: str,
) -> None:
    output_sha256 = hashlib.sha256(destination.read_bytes()).hexdigest()
    _write_json(
        _editorial_output_integrity_path(destination),
        {
            "schema_version": 1,
            "batch_id": batch_id,
            "source_input_sha256": input_sha256,
            "output_sha256": output_sha256,
        },
    )


def _require_editorial_backoff_batch(root: Path | str | None, batch_id: str) -> None:
    if root is None:
        return
    state = editorial_backoff_status(root)
    if state.get("status") == "none":
        return
    if state.get("status") == "invalid":
        raise EditorialProviderError(
            "marcador de cooldown editorial inválido; retomar está bloqueado",
            failure_class="retry_state_invalid",
            retryable=False,
        )
    resume_batch_id = str(state.get("batch_id") or "")
    if resume_batch_id != batch_id:
        raise EditorialProviderError(
            f"retome o batch editorial {resume_batch_id} antes de iniciar outro",
            failure_class="batch_resume_required",
            retryable=False,
            http_status=429,
            retry_after_seconds=int(state.get("remaining_seconds") or 0),
            retry_at=str(state.get("not_before") or "") or None,
            resume_batch_id=resume_batch_id,
        )
    if state.get("active"):
        raise EditorialProviderError(
            f"provedor editorial em cooldown até {state.get('not_before')}",
            failure_class="rate_limited",
            retryable=True,
            http_status=429,
            retry_after_seconds=int(state.get("remaining_seconds") or 0),
            retry_at=str(state.get("not_before") or "") or None,
            resume_batch_id=resume_batch_id,
        )


def _record_editorial_rate_limit(
    root: Path | str | None,
    batch_id: str,
    retry_after_seconds: int | None,
) -> dict[str, Any] | None:
    if root is None:
        return None
    state = record_editorial_backoff(
        root,
        batch_id=batch_id,
        http_status=429,
        retry_after_seconds=retry_after_seconds,
        failure_class="rate_limited",
    )
    try:
        from .observability import append_telemetry

        append_telemetry(
            root,
            "editorial_provider_rate_limited",
            batch_id=batch_id,
            http_status=429,
            retry_after_seconds=state["retry_after_seconds"],
            retry_at=state["not_before"],
        )
    except Exception:  # noqa: BLE001 - telemetry must not change retry safety
        pass
    return state


def complete_editorial_backoff(root: Path | str, batch_id: str) -> bool:
    """Clear an expired batch cooldown only after its caller completed successfully."""
    state = clear_expired_editorial_backoff(root, batch_id=batch_id)
    if state is None:
        return False
    try:
        from .observability import append_telemetry

        append_telemetry(
            root,
            "editorial_provider_retry_completed",
            batch_id=batch_id,
            retry_after_seconds=int(state.get("retry_after_seconds") or 0),
        )
    except Exception:  # noqa: BLE001 - observability must not reopen completed work
        pass
    return True


def _reuse_existing_editorial_output(
    destination: Path,
    *,
    batch_id: str,
    posts: list[dict[str, Any]],
    input_sha256: str,
    root: Path | str | None,
    emit_media_input: bool,
    media_source_policy: str,
) -> dict[str, Any] | None:
    if not destination.exists():
        return None
    try:
        output_bytes = destination.read_bytes()
        payload = json.loads(output_bytes.decode("utf-8"))
        if not isinstance(payload, dict):
            raise BatchError("output editorial precisa ser um objeto JSON")
        integrity = json.loads(
            _editorial_output_integrity_path(destination).read_text(encoding="utf-8")
        )
        if not isinstance(integrity, dict):
            raise BatchError("manifesto de integridade editorial inválido")
        validated = load_editorial_batch(destination)
        if destination.read_bytes() != output_bytes:
            raise BatchError("output editorial mudou durante a validação")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, BatchError) as exc:
        raise EditorialProviderError(
            "output editorial existente inválido; não será sobrescrito",
            failure_class="batch_output_invalid",
            retryable=False,
        ) from exc
    expected_ids = {int(post["post_id"]) for post in posts}
    output_ids = {int(item["post_id"]) for item in validated["items"]}
    output_sha256 = hashlib.sha256(output_bytes).hexdigest()
    if (
        payload.get("batch_id") != batch_id
        or payload.get("source_input_sha256") != input_sha256
        or validated.get("batch_id") != batch_id
        or output_ids != expected_ids
        or type(integrity.get("schema_version")) is not int
        or integrity.get("schema_version") != 1
        or integrity.get("batch_id") != batch_id
        or integrity.get("source_input_sha256") != input_sha256
        or integrity.get("output_sha256") != output_sha256
    ):
        raise EditorialProviderError(
            "output editorial existente não corresponde à entrada ou ao manifesto "
            f"do batch {batch_id}; use um novo batch_id em vez de sobrescrever",
            failure_class="batch_output_conflict",
            retryable=False,
            resume_batch_id=batch_id,
        )
    validated_items = validated["items"]
    media_input = None
    if emit_media_input:
        if root is None:
            raise BatchError("root é obrigatório para gerar media.input.json")
        media_payload = {
            "schema_version": validated["schema_version"],
            "batch_id": batch_id,
            "results": validated_items,
        }
        media_input = build_media_resolve_batch_input(media_payload, root, media_source_policy=media_source_policy)
    if root is not None:
        try:
            from .observability import append_telemetry

            append_telemetry(
                root,
                "editorial_output_reused",
                batch_id=batch_id,
                batch_size=len(posts),
            )
        except Exception:  # noqa: BLE001 - observability is fail-soft
            pass
    result: dict[str, Any] = {
        "schema_version": BATCH_SCHEMA_VERSION,
        "batch_id": batch_id,
        "provider_requests": 0,
        "cached_output": True,
        "count": len(posts),
        "ok": sum(1 for item in validated_items if item["status"] == "ok"),
        "needs_retry": sum(
            1 for item in validated_items if item["status"] == "needs_retry"
        ),
        "output": str(destination),
        "input_tokens": 0,
        "output_tokens": 0,
    }
    if emit_media_input and media_input is not None:
        result["media_input"] = media_input
    return result


def _serialize_editorial_provider_calls(
    function: Callable[..., Any],
) -> Callable[..., Any]:
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        input_path = args[0] if args else kwargs.get("input_path")
        if input_path is None:
            return function(*args, **kwargs)
        root = kwargs.get("root")
        operation_root = Path(root) if root is not None else Path(input_path).resolve().parent
        with editorial_provider_request_lock(operation_root):
            return function(*args, **kwargs)

    return wrapped


@_serialize_editorial_provider_calls
def generate_editorial_batch(
    input_path: Path | str,
    *,
    api_key: str,
    base_url: str,
    model: str,
    timeout: float = 60.0,
    min_confidence: float = 0.8,
    root: Path | str | None = None,
    output_path: Path | str | None = None,
    provider: str = "openai",
    hermes_command: str = "hermes",
    hermes_model: str = "",
    hermes_timeout: float | None = None,
    media_source_policy: str = "strict",
    emit_media_input: bool = False,
    defer_backoff_clear: bool = True,
) -> dict[str, Any]:
    """Generate and validate one editorial batch.

    ``provider=auto`` prefers the installed Hermes CLI and falls back to the
    legacy OpenAI-compatible endpoint only when Hermes is unavailable. The
    default remains ``openai`` for direct library callers and old tests; the
    production ``Config`` passes ``auto`` explicitly.
    """
    batch_id, posts, input_sha256 = _read_input(input_path)
    input_file = Path(input_path)
    operation_root = Path(root) if root is not None else input_file.resolve().parent
    destination = (
        Path(output_path)
        if output_path
        else batch_directory(operation_root, batch_id) / "editorial.output.json"
    )
    _require_editorial_backoff_batch(operation_root, batch_id)
    cached_result = _reuse_existing_editorial_output(
        destination,
        batch_id=batch_id,
        posts=posts,
        input_sha256=input_sha256,
        root=operation_root,
        emit_media_input=emit_media_input,
        media_source_policy=media_source_policy,
    )
    if cached_result is not None:
        if not defer_backoff_clear:
            complete_editorial_backoff(operation_root, batch_id)
        return cached_result
    selected_provider = str(provider or "openai").strip().lower()
    if selected_provider not in {"auto", "openai", "hermes"}:
        raise EditorialProviderError(
            f"provedor editorial inválido: {provider!r}",
            failure_class="configuration_error",
            retryable=False,
        )
    if selected_provider == "auto":
        selected_provider = "hermes" if hermes_command_available(hermes_command) else "openai"
    if selected_provider == "openai" and not api_key:
        raise EditorialProviderError(
            "EDITORIAL_API_KEY/OPENAI_API_KEY ausente e Hermes não disponível",
            failure_class="configuration_error",
            retryable=False,
        )
    if selected_provider == "hermes" and not hermes_command_available(hermes_command):
        # An explicit Hermes selection is a hard provider boundary.  Falling
        # back to an OpenAI-compatible endpoint would spend paid budget and
        # make a V1 Hermes restoration look healthy while using another model.
        raise EditorialProviderError(
            f"Hermes não disponível para o provedor explicitamente selecionado: {hermes_command}",
            failure_class="configuration_error",
            retryable=False,
        )
    output_schema = _output_schema_for_policy(media_source_policy)
    request_payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Transforme este envelope sem ferramentas. Devolva "
                    '"media_plan": [] — a resolucao de imagens e feita DEPOIS por '
                    "media-resolve-batch: nao invente URLs, legendas ou proveniencia e "
                    "nao responda needs_retry por falta de imagem ou de featured.\n"
                    + json.dumps(
                        {"batch_id": batch_id, "posts": posts}, ensure_ascii=False
                    )
                ),
            },
        ],
        "temperature": 0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "editorial_batch",
                "strict": True,
                "schema": output_schema,
            },
        },
    }
    usage: dict[str, Any] = {}
    if selected_provider == "hermes":
        hermes_prompt = (
            f"{_SYSTEM_PROMPT}\n\n"
            "Return the JSON object directly, with no Markdown fences and no commentary. "
            "It must conform exactly to this JSON Schema:\n"
            + json.dumps(output_schema, ensure_ascii=False, separators=(",", ":"))
            + "\n\nInput envelope:\n"
            + json.dumps(
                {"batch_id": batch_id, "posts": posts}, ensure_ascii=False
            )
        )
        try:
            response_text, usage = call_hermes_oneshot(
                hermes_prompt,
                command=hermes_command,
                model=hermes_model,
                timeout=hermes_timeout if hermes_timeout is not None else timeout,
                cwd=Path(input_path).resolve().parent,
            )
        except AIProviderError as exc:
            metadata = dict(exc.metadata or {})
            raw_status = metadata.get("http_status")
            http_status = (
                int(raw_status)
                if isinstance(raw_status, int) and not isinstance(raw_status, bool)
                else (429 if exc.failure_class == "rate_limited" else None)
            )
            retry_after = metadata.get("retry_after_seconds")
            retry_after = retry_after if isinstance(retry_after, int) and not isinstance(retry_after, bool) else None
            failure_class = "rate_limited" if http_status == 429 else exc.failure_class
            backoff = (
                _record_editorial_rate_limit(operation_root, batch_id, retry_after)
                if http_status == 429
                else None
            )
            raise EditorialProviderError(
                str(exc),
                failure_class=failure_class,
                retryable=exc.retryable,
                http_status=http_status,
                retry_after_seconds=(
                    int(backoff["retry_after_seconds"])
                    if backoff is not None
                    else retry_after
                ),
                retry_at=str(backoff["not_before"]) if backoff is not None else None,
                resume_batch_id=batch_id if http_status == 429 else None,
            ) from exc
        body = {
            "choices": [{"message": {"content": response_text}}],
            "usage": usage,
        }
    else:
        request = Request(
            f"{base_url.rstrip('/')}/chat/completions",
            data=json.dumps(request_payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                response_bytes = response.read()
        except HTTPError as exc:
            retryable = exc.code in {408, 425, 429} or 500 <= int(exc.code) < 600
            response_headers = getattr(exc, "headers", None)
            retry_after = parse_retry_after_seconds(
                response_headers.get("Retry-After") if response_headers else None
            )
            backoff = (
                _record_editorial_rate_limit(operation_root, batch_id, retry_after)
                if exc.code == 429
                else None
            )
            raise EditorialProviderError(
                f"API editorial respondeu HTTP {exc.code}",
                failure_class="rate_limited" if exc.code == 429 else "api_http_error",
                retryable=retryable,
                http_status=int(exc.code),
                retry_after_seconds=(
                    int(backoff["retry_after_seconds"])
                    if backoff is not None
                    else retry_after
                ),
                retry_at=str(backoff["not_before"]) if backoff is not None else None,
                resume_batch_id=batch_id if exc.code == 429 else None,
            ) from exc
        except (URLError, OSError, TimeoutError, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            reason_text = str(reason).casefold()
            failure_class = (
                "timeout"
                if isinstance(reason, (TimeoutError, socket.timeout))
                or "timed out" in reason_text
                or "timeout" in reason_text
                else "transport_error"
            )
            raise EditorialProviderError(
                f"falha na API editorial: {exc}",
                failure_class=failure_class,
                retryable=True,
            ) from exc
        try:
            body = json.loads(response_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EditorialProviderError(
                "resposta HTTP editorial nao e JSON valido",
                failure_class="invalid_response",
                retryable=True,
            ) from exc

    normalized = _normalize_output(
        _parse_json(_response_text(body)),
        batch_id=batch_id,
        post_ids={int(item["post_id"]) for item in posts},
        min_confidence=min_confidence,
        relevance_policies={
            int(item["post_id"]): {
                str(topic).strip().casefold()
                for topic in ((item.get("relevance_policy") or {}).get("allowed_topics") or [])
                if str(topic).strip()
            }
            for item in posts
        },
        localization_requirements={
            int(item["post_id"]): bool((item.get("localization") or {}).get("required"))
            for item in posts
        },
        media_source_policy=media_source_policy,
    )
    input_retry_modes = {
        int(item["post_id"]): str(item.get("retry_mode") or "").strip()
        for item in posts
    }
    for result in normalized["results"]:
        result["retry_mode"] = input_retry_modes.get(int(result["post_id"]), "")
    normalized["source_input_sha256"] = input_sha256
    output = _write_json(destination, normalized)
    _write_editorial_output_integrity(
        destination,
        batch_id=batch_id,
        input_sha256=input_sha256,
    )
    media_input = None
    if emit_media_input:
        media_input = build_media_resolve_batch_input(normalized, operation_root, media_source_policy=media_source_policy)
    usage = body.get("usage") or {}
    input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    if root is not None:
        from .config import editorial_price_per_1m
        from .observability import append_telemetry, usage_cost_usd

        entrada = input_tokens
        saida = output_tokens
        detalhes = usage.get("prompt_tokens_details") or {}
        preco_in, preco_out = editorial_price_per_1m()
        custo = usage_cost_usd(
            entrada, saida, price_in_per_1m=preco_in, price_out_per_1m=preco_out
        )
        configured_model = (
            hermes_model if selected_provider == "hermes" else model
        )
        verified_model = _verified_model_identity(usage)
        evento: dict[str, Any] = {
            "batch_id": batch_id,
            "batch_size": len(posts),
            "posts_generated": sum(1 for item in normalized["results"] if item["status"] == "ok"),
            "input_tokens": entrada,
            "output_tokens": saida,
            "cached_tokens": int(detalhes.get("cached_tokens") or 0),
            "provider": "hermes" if selected_provider == "hermes" else base_url[:120],
            "backend": selected_provider,
            "model_configured": str(configured_model or "")[:160],
            "model_identity_source": "provider_usage" if verified_model else "unverified",
        }
        if verified_model:
            evento["model"] = verified_model
        # Custo da chamada DIRETA: o gate (`cost_guard`) prefere este numero ao
        # preco do ambiente, porque registra o preco vigente na hora da chamada.
        if custo is not None:
            evento["model_cost_usd"] = custo
        append_telemetry(root, "editorial_model_request", **evento)
        for result in normalized["results"]:
            for normalization in result.get("normalizations") or []:
                append_telemetry(
                    root,
                    "editorial_normalization",
                    batch_id=batch_id,
                    post_id=int(result["post_id"]),
                    normalization=str(normalization),
                )
    result = {
        "schema_version": BATCH_SCHEMA_VERSION,
        "batch_id": batch_id,
        "provider_requests": 1,
        "count": len(posts),
        "ok": sum(1 for item in normalized["results"] if item["status"] == "ok"),
        "needs_retry": sum(1 for item in normalized["results"] if item["status"] == "needs_retry"),
        "output": output,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    if emit_media_input:
        result["media_input"] = media_input
    if not defer_backoff_clear:
        complete_editorial_backoff(operation_root, batch_id)
    return result


__all__ = [
    "EditorialProviderError",
    "complete_editorial_backoff",
    "generate_editorial_batch",
]
