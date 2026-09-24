"""Direct, single-request editorial generation for a prepared microbatch.

This module deliberately does not use the Hermes tool loop.  It sends one
OpenAI-compatible chat-completions request, asks for a structured batch
response, validates every result with the existing editorial schema and writes
an output envelope that ``apply-batch`` can consume.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .batch import BATCH_SCHEMA_VERSION, BatchError, batch_directory, validate_batch_id
from .editorial_schema import EditorialValidationError, validate_editorial


class EditorialProviderError(RuntimeError):
    """Raised when the direct editorial provider call cannot be trusted."""


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
        "source_page_url": {"type": "string"},
        "direct_image_url": {"type": "string"},
        "author": {"type": "string"},
        "license": {"type": "string"},
        "license_url": {"type": "string"},
        "captured_at": {"type": "string"},
        "credit_text": {"type": "string"},
        "alt_text": {"type": "string"},
        "is_featured": {"type": "boolean"},
    },
    "required": [
        "paragraph_index",
        "source_page_url",
        "direct_image_url",
        "author",
        "license",
        "license_url",
        "captured_at",
        "credit_text",
        "alt_text",
        "is_featured",
    ],
    "additionalProperties": False,
}

_SEO_SCHEMA = {
    "type": ["object", "null"],
    "properties": {
        "title": {"type": "string"},
        "meta_description": {"type": "string"},
        "focus_keyword": {"type": "string"},
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
        "site_relevance": _RELEVANCE_SCHEMA,
        "media_plan": {"type": "array", "items": _MEDIA_ITEM_SCHEMA},
        "needs_trailer": {"type": "boolean"},
        "trailer_url": {"type": ["string", "null"]},
        "game_name": {"type": ["string", "null"]},
        "cleaned_html": {"type": ["string", "null"]},
        "seo": _SEO_SCHEMA,
    },
    "required": [
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
                    "editorial": _EDITORIAL_SCHEMA,
                },
                "required": ["post_id", "status", "reason", "editorial"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["batch_id", "results"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = """You are the UnicornioHater editorial transformation service.
You receive a prepared JSON envelope containing one or two independent posts.
Return ONLY the requested structured JSON object. Do not call tools, browse,
search, invent facts, or combine facts between posts. Use post_id as the only
correlation key. If one post cannot be safely completed, return
status=needs_retry and a concise reason for that post; still return the other
post when it is valid. For status=ok, editorial must follow the existing
editorial contract: site_relevance, media_plan, needs_trailer, trailer_url and
game_name are required fields. cleaned_html and seo must be null unless a real
change is required — never re-emit text or SEO the post already carries.

Neither images nor the trailer are resolved in this call: both are handled
deterministically afterwards by the code. So: always return "media_plan": [];
never invent image URLs, licenses, credits or provenance; never answer
needs_retry because of missing inline images, a missing featured image or any
image count; and return needs_trailer=false with trailer_url=null unless the
envelope already carries a verified trailer URL. Do fill game_name when the
subject is a game. Use needs_retry only when the text/SEO facts themselves
cannot be handled safely."""


def _read_input(path: Path | str) -> tuple[str, list[dict[str, Any]]]:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EditorialProviderError(f"entrada editorial invalida: {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EditorialProviderError("entrada editorial precisa ser um objeto JSON")
    batch_id = validate_batch_id(str(payload.get("batch_id") or ""))
    posts = payload.get("posts")
    if not isinstance(posts, list) or not 1 <= len(posts) <= 2:
        raise EditorialProviderError("entrada editorial precisa conter de 1 a 2 posts")
    ids: set[int] = set()
    for item in posts:
        if not isinstance(item, dict):
            raise EditorialProviderError("cada post da entrada editorial precisa ser um objeto")
        post_id = item.get("post_id")
        if isinstance(post_id, bool) or not isinstance(post_id, int) or post_id <= 0:
            raise EditorialProviderError("post_id invalido na entrada editorial")
        if post_id in ids:
            raise EditorialProviderError(f"post_id duplicado na entrada editorial: {post_id}")
        ids.add(post_id)
    return batch_id, posts


def _response_text(body: dict[str, Any]) -> str:
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise EditorialProviderError("resposta editorial sem choices[0].message.content") from exc
    if isinstance(content, list):
        content = "".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    if not isinstance(content, str) or not content.strip():
        raise EditorialProviderError("resposta editorial vazia")
    return content.strip()


def _parse_json(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EditorialProviderError("resposta editorial nao e JSON") from exc
    if not isinstance(value, dict):
        raise EditorialProviderError("resposta editorial precisa ser um objeto")
    return value


def _normalize_output(
    payload: dict[str, Any],
    *,
    batch_id: str,
    post_ids: set[int],
    min_confidence: float,
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
        if status == "needs_retry":
            normalized.append({"post_id": post_id, "status": status, "reason": reason or "retry solicitado"})
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
                    })
                    seen.add(post_id)
                    continue
            if not isinstance(editorial, dict):
                raise EditorialProviderError(f"editorial ausente para {post_id}")
            # O trailer e descoberto de forma DETERMINISTICA pelo codigo (por
            # `game_name`, em compose_final_content): nao existe busca de
            # trailer nesta chamada sem ferramentas. Se o modelo sinalizou
            # needs_trailer sem uma URL verificada, o contrato rejeitaria o item
            # inteiro; normalizamos para false preservando game_name, e a
            # descoberta continua acontecendo no apply.
            if editorial.get("needs_trailer") and not editorial.get("trailer_url"):
                editorial = {**editorial, "needs_trailer": False, "trailer_url": None}
            try:
                checked = validate_editorial(editorial, min_confidence=min_confidence)
            except EditorialValidationError as exc:
                normalized.append({
                    "post_id": post_id,
                    "status": "needs_retry",
                    "reason": f"editorial invalido: {exc}",
                })
            else:
                normalized.append({
                    "post_id": post_id,
                    "status": "ok",
                    "reason": reason,
                    "editorial": checked,
                })
        seen.add(post_id)
    if seen != post_ids:
        raise EditorialProviderError("resposta editorial nao cobre exatamente os posts de entrada")
    return {"schema_version": BATCH_SCHEMA_VERSION, "batch_id": batch_id, "results": normalized}


def _write_json(path: Path, payload: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return str(path)


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
) -> dict[str, Any]:
    """Generate and validate one editorial batch with exactly one HTTP call."""
    if not api_key:
        raise EditorialProviderError("EDITORIAL_API_KEY/OPENAI_API_KEY ausente")
    batch_id, posts = _read_input(input_path)
    request_payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Transforme este envelope sem ferramentas. Devolva "
                    '"media_plan": [] — a resolucao de imagens e feita DEPOIS por '
                    "media-resolve-batch: nao invente URLs, licencas ou creditos e "
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
                "schema": _OUTPUT_SCHEMA,
            },
        },
    }
    request = Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(request_payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise EditorialProviderError(f"API editorial respondeu HTTP {exc.code}") from exc
    except (URLError, OSError, ValueError) as exc:
        raise EditorialProviderError(f"falha na API editorial: {exc}") from exc

    normalized = _normalize_output(
        _parse_json(_response_text(body)),
        batch_id=batch_id,
        post_ids={int(item["post_id"]) for item in posts},
        min_confidence=min_confidence,
    )
    destination = Path(output_path) if output_path else batch_directory(root or Path(input_path).parent, batch_id) / "editorial.output.json"
    output = _write_json(destination, normalized)
    usage = body.get("usage") or {}
    if root is not None:
        from .config import editorial_price_per_1m
        from .observability import append_telemetry, usage_cost_usd

        entrada = int(usage.get("prompt_tokens") or 0)
        saida = int(usage.get("completion_tokens") or 0)
        detalhes = usage.get("prompt_tokens_details") or {}
        preco_in, preco_out = editorial_price_per_1m()
        custo = usage_cost_usd(
            entrada, saida, price_in_per_1m=preco_in, price_out_per_1m=preco_out
        )
        evento: dict[str, Any] = {
            "batch_id": batch_id,
            "batch_size": len(posts),
            "posts_generated": sum(1 for item in normalized["results"] if item["status"] == "ok"),
            "input_tokens": entrada,
            "output_tokens": saida,
            "cached_tokens": int(detalhes.get("cached_tokens") or 0),
            "model": model,
            "provider": base_url[:120],
        }
        # Custo da chamada DIRETA: o gate (`cost_guard`) prefere este numero ao
        # preco do ambiente, porque registra o preco vigente na hora da chamada.
        if custo is not None:
            evento["model_cost_usd"] = custo
        append_telemetry(root, "editorial_model_request", **evento)
    return {
        "schema_version": BATCH_SCHEMA_VERSION,
        "batch_id": batch_id,
        "provider_requests": 1,
        "count": len(posts),
        "ok": sum(1 for item in normalized["results"] if item["status"] == "ok"),
        "needs_retry": sum(1 for item in normalized["results"] if item["status"] == "needs_retry"),
        "output": output,
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
    }


__all__ = ["EditorialProviderError", "generate_editorial_batch"]
