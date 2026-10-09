"""Vision gate: confirms an image depicts its subject with a cheap vision LLM.

The deterministic gates (relevance text, source-page verification) cannot
catch a CDN that serves the wrong image under a correct slug at the exact
moment of download. This final belt-and-suspenders gate asks a cheap vision
model whether the actual published image is visually consistent with the
subject it is meant to illustrate. Fail-closed: API errors block publication
with the reason.

Design (cost-controlled):
- Prompt is RESTRICTED: the model judges the PIXELS, treating ALT/filename/URL
  as context only ("a real bat captioned Redfall is NOT Redfall"). It never
  tries to name the work (knowledge cutoff would fail for 2026 news).
- Uses Structured Outputs -> {status, confidence, visual_type}.
- `detail: low` by default (~2833 tokens/image). On AMBIGUOUS and when
  `allow_high` is set, re-asks at `detail: high` (~13x cost) before deciding.

Any OpenAI-compatible vision endpoint works (OpenAI, Gemini via
`OPENAI_COMPAT` base URL, local vLLM, ...), configured through
`EDITOR_VISION_*` env vars (key falls back to `OPENAI_API_KEY`).
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import re
import tempfile
from pathlib import Path
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .downloader import download_image


class VisionGateError(RuntimeError):
    """Raised when the vision model cannot confirm the image subject."""


class VisionInputUnavailable(RuntimeError):
    """Raised when one candidate cannot be materialized for vision."""


VISUAL_DECISIONS = ("SAME_IMAGE", "SAME_ART_CROP", "DIFFERENT", "UNCERTAIN", "ERROR")


@dataclass(frozen=True)
class VisualComparison:
    """A pixel-only comparison.  ``duplicate`` is deliberately conservative."""

    decision: str
    confidence: float
    reference_id: str = ""
    candidate_id: str = ""
    reason: str = ""

    @property
    def duplicate(self) -> bool:
        return self.decision in {"SAME_IMAGE", "SAME_ART_CROP"}

    @property
    def verified_different(self) -> bool:
        return self.decision == "DIFFERENT"

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision, "confidence": self.confidence,
            "reference_id": self.reference_id, "candidate_id": self.candidate_id,
            "reason": self.reason,
        }


# status allowed by the model.
_STATUS = ("MATCH", "PARTIAL_MATCH", "UNRELATED", "AMBIGUOUS")
_VISUAL_TYPES = (
    "gameplay", "key_art", "movie_still", "character", "person", "product",
    "logo", "poster", "photograph", "illustration", "animal", "other",
    "text_banner", "infographic",
)
_ACCEPT_THRESHOLD = 0.85   # MATCH and confidence >= this -> accept
_REJECT_THRESHOLD = 0.80   # UNRELATED and confidence >= this -> reject

_SYSTEM_PROMPT = (
    "You are an editorial image validator. You judge the actual VISUAL CONTENT "
    "of an image and decide whether it is consistent with a described subject. "
    "Return ONLY a JSON object with keys status, confidence and visual_type. "
    "status must be one of: MATCH, PARTIAL_MATCH, UNRELATED, AMBIGUOUS. "
    "confidence is a float 0..1. visual_type must be one of: "
    "gameplay, key_art, movie_still, character, person, product, logo, poster, "
    "photograph, illustration, animal, other, text_banner, infographic."
)

_BATCH_SYSTEM_PROMPT = (
    "You are an editorial image validator working on independent candidates. "
    "For every candidate, judge only the actual pixels against that candidate's "
    "expected subject. Return ONLY JSON with an 'items' array. Each item must "
    "contain candidate_id, status, confidence and visual_type. Never move a "
    "decision from one candidate to another."
)


def _image_data_url_from_bytes(data: bytes) -> str:
    if not data:
        raise VisionInputUnavailable("empty image payload")
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            image.verify()
            mime = Image.MIME.get(image.format or "", "")
    except Exception as exc:  # noqa: BLE001 - invalid candidate bytes
        raise VisionInputUnavailable("image bytes are not decodable") from exc
    if not mime.startswith("image/"):
        raise VisionInputUnavailable("image MIME type is unsupported")
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def prepare_vision_image_input_from_path(path: str | Path) -> str:
    """Return a data URL from already materialised image bytes.

    Visual identity must compare the exact converted WebP bytes that will be
    uploaded, never a remote URL which may later serve different pixels.
    """
    try:
        return _image_data_url_from_bytes(Path(path).read_bytes())
    except OSError as exc:
        raise VisionInputUnavailable("image file is unavailable") from exc


def _data_url_bytes(value: str) -> bytes | None:
    match = re.fullmatch(r"data:(image/[^;]+);base64,(.+)", value.strip(), re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    try:
        return base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError):
        raise VisionInputUnavailable("invalid base64 image input")


def prepare_vision_image_input(
    image_url: str,
    *,
    timeout: float = 30.0,
    max_bytes: int = 8 * 1024 * 1024,
    url_policy: str = "audit",
) -> str:
    """Materialize an image locally and return a provider-safe data URL.

    External URLs are fetched by this process, so the vision provider never
    has to fetch a CDN, signed URL, or anti-hotlink resource itself. The
    returned Base64 is an in-memory API payload and must not be logged.
    """
    value = str(image_url or "").strip()
    if not value:
        raise VisionInputUnavailable("empty image URL")
    if value.lower().startswith("data:"):
        data = _data_url_bytes(value)
        if data is None:
            raise VisionInputUnavailable("invalid data URL image input")
        return _image_data_url_from_bytes(data)
    if not value.startswith(("http://", "https://")):
        raise VisionInputUnavailable("image URL must use HTTP(S) or a data URL")
    try:
        with tempfile.TemporaryDirectory(prefix="unicornio-vision-") as directory:
            path = download_image(
                value,
                Path(directory) / "source-image",
                max_bytes=max_bytes,
                url_policy=url_policy,
                timeout=timeout,
            )
            return _image_data_url_from_bytes(Path(path).read_bytes())
    except VisionInputUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - candidate-specific download failure
        raise VisionInputUnavailable(f"image download unavailable: {type(exc).__name__}") from exc


def _valid_image_input(value: str) -> bool:
    text = str(value or "").strip()
    if text.startswith(("http://", "https://")):
        return True
    return bool(re.fullmatch(r"data:image/[^;]+;base64,[A-Za-z0-9+/=\\r\\n]+", text, re.IGNORECASE))


def _http_error_context(exc: HTTPError) -> dict[str, str]:
    """Extract only safe provider error fields from an HTTP response body."""
    try:
        raw = exc.read().decode("utf-8", errors="replace")[:2000]
    except Exception:  # noqa: BLE001 - the body is optional diagnostic context
        raw = ""
    try:
        payload = json.loads(raw)
    except ValueError:
        return {"response_body": raw}
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        error = payload if isinstance(payload, dict) else {}
    safe = {
        key: str(error.get(key) or "")[:500]
        for key in ("type", "code", "message", "param")
        if error.get(key) is not None
    }
    body = json.dumps(safe, ensure_ascii=False, sort_keys=True)[:2000]
    return {
        "response_body": body,
        "response_error_type": safe.get("type", ""),
        "response_error_code": safe.get("code", ""),
        "response_message": safe.get("message", ""),
    }


def _json_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": list(_STATUS),
            },
            "confidence": {"type": "number"},
            "visual_type": {
                "type": "string",
                "enum": list(_VISUAL_TYPES),
            },
        },
        "required": ["status", "confidence", "visual_type"],
        "additionalProperties": False,
    }


def _batch_json_output_schema() -> dict[str, Any]:
    """Strict envelope used by providers that implement JSON Schema output."""
    item = _json_output_schema()
    item["properties"]["candidate_id"] = {"type": "string"}
    item["required"] = ["candidate_id", *item["required"]]
    return {
        "type": "object",
        "properties": {"items": {"type": "array", "items": item}},
        "required": ["items"],
        "additionalProperties": False,
    }


def _parse_response(text: str, *, allow_unknown_status: bool = False) -> dict[str, Any]:
    """Parse the model answer (Structured Outputs returns pure JSON)."""
    raw = (text or "").strip()
    try:
        data = json.loads(raw)
    except ValueError:
        # Fallback: strip code fences if any.
        fenced = re.search(r"{.*}", raw, re.DOTALL)
        if not fenced:
            raise VisionGateError(f"resposta invalida da API de visao: {raw[:120]!r}")
        try:
            data = json.loads(fenced.group(0))
        except ValueError as exc:
            raise VisionGateError(f"resposta invalida da API de visao: {raw[:120]!r}") from exc
    status = data.get("status")
    confidence = data.get("confidence")
    visual_type = data.get("visual_type")
    raw_status = status
    normalized_status = {
        "VALID": "MATCH",
        "INVALID": "UNRELATED",
    }.get(str(status).strip().upper(), status)
    if normalized_status not in _STATUS and not allow_unknown_status:
        raise VisionGateError(f"status de visao desconhecido: {status!r}")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise VisionGateError(f"confidence de visao invalida: {confidence!r}")
    confidence = float(confidence)
    if not 0 <= confidence <= 1:
        raise VisionGateError(f"confidence fora de [0,1]: {confidence!r}")
    if visual_type not in _VISUAL_TYPES:
        visual_type = "other"
    if normalized_status not in _STATUS:
        normalized_status = "UNKNOWN"
    return {
        "status": normalized_status,
        "confidence": confidence,
        "visual_type": visual_type,
        "vision_status_normalized": normalized_status if normalized_status != "UNKNOWN" else "INCONCLUSIVE",
        **({"vision_status_raw": raw_status} if raw_status != normalized_status else {}),
    }


def _build_user_prompt(
    subject: str, *,
    context: str = "", category: str = "", alt: str = "",
    require_key_art: bool = False,
) -> str:
    """Restricted prompt: metadata is context, pixels are the evidence."""
    lines = [
        "Do not assume that the ALT text, filename, URL or source description "
        "correctly describes the image. Judge the actual visual content of the "
        "image; the textual metadata is context only.",
        "",
        f"Expected subject: {subject.strip()}",
    ]
    if category.strip():
        lines.append(f"Category: {category.strip()}")
    if context.strip():
        lines.append(f"Context: {context.strip()}")
    if alt.strip():
        lines.append(f"ALT text (context only): {alt.strip()}")
    lines += [
        "",
        "Answer whether the image is visually consistent with the expected "
        "subject and category. Reject unrelated real-world photography, generic "
        "animals, unrelated games, unrelated brand logos, and stock photography.",
    ]
    if require_key_art:
        lines += [
            "",
            "This image is intended as KEY ART / featured image of the subject: a "
            "valid key art is the subject's own artwork, screenshot, character, or "
            "official title treatment/wordmark.",
            "An article headline card, news/share banner, infographic, or "
            "typographic graphic that merely announces or describes the subject in "
            "text but carries NO actual artwork of it is NOT acceptable as key art "
            "— classify it as visual_type 'text_banner' (or 'infographic') and "
            "status 'UNRELATED'.",
        ]
    return "\n".join(lines)


def _call_vision(
    *,
    image_url: str,
    prompt: str,
    api_key: str,
    base_url: str,
    model: str,
    detail: str,
    timeout: float,
    root: Any = None,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": image_url, "detail": detail},
                    },
                ],
            },
        ],
        "max_tokens": 60,
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    request = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        error_context = _http_error_context(exc)
        _registrar_chamada(root, detail=detail, model=model, base_url=base_url,
                           erro=f"HTTP {exc.code}", **error_context)
        raise VisionGateError(f"API de visao respondeu HTTP {exc.code}") from exc
    except (URLError, OSError, ValueError) as exc:
        _registrar_chamada(root, detail=detail, model=model, base_url=base_url, erro=str(exc))
        raise VisionGateError(f"falha ao chamar a API de visao: {exc}") from exc
    try:
        answer = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        _registrar_chamada(root, detail=detail, model=model, base_url=base_url,
                           erro="resposta invalida")
        raise VisionGateError("resposta invalida da API de visao") from exc
    # Uma requisição HTTP = um evento, com o `usage` devolvido pelo provedor.
    # `verify_image_subject` pode chamar DUAS vezes (low + high): contar em volta
    # da função subestimava o custo real de visão e impedia reconciliar com o
    # dashboard do provedor.
    _registrar_chamada(
        root, detail=detail, model=model, base_url=base_url, usage=body.get("usage") or {}
    )
    return _parse_response(answer)


_VISUAL_COMPARISON_SYSTEM_PROMPT = (
    "You compare TWO images using pixels only. Ignore filenames, URLs, ALT text, "
    "captions and any external knowledge. Return ONLY JSON with decision, confidence "
    "and reason. decision must be SAME_IMAGE (same underlying image), SAME_ART_CROP "
    "(same artwork/frame but crop/resize/format differs), DIFFERENT, UNCERTAIN, or ERROR."
)


def _parse_visual_comparison(text: str, *, reference_id: str, candidate_id: str) -> VisualComparison:
    try:
        value = json.loads((text or "").strip())
    except ValueError as exc:
        raise VisionGateError("resposta de comparacao visual nao e JSON") from exc
    decision = str(value.get("decision") or "").upper()
    confidence = value.get("confidence")
    if decision not in VISUAL_DECISIONS:
        raise VisionGateError(f"decisao visual desconhecida: {decision!r}")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
        raise VisionGateError("confidence de comparacao visual invalida")
    return VisualComparison(
        decision=decision, confidence=float(confidence), reference_id=reference_id,
        candidate_id=candidate_id, reason=str(value.get("reason") or "")[:500],
    )


def _call_visual_comparison(
    *, reference_image: str, candidate_image: str, reference_id: str, candidate_id: str,
    api_key: str, base_url: str, model: str, detail: str, timeout: float, root: Any = None,
) -> VisualComparison:
    if not (_valid_image_input(reference_image) and _valid_image_input(candidate_image)):
        raise VisionInputUnavailable("comparacao visual requer duas imagens materializadas")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _VISUAL_COMPARISON_SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": "Reference image A:"},
                {"type": "image_url", "image_url": {"url": reference_image, "detail": detail}},
                {"type": "text", "text": "Candidate image B:"},
                {"type": "image_url", "image_url": {"url": candidate_image, "detail": detail}},
            ]},
        ],
        "temperature": 0,
        "max_tokens": 100,
        "response_format": {"type": "json_object"},
    }
    request = Request(
        f"{base_url.rstrip('/')}/chat/completions", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}, method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        answer = body["choices"][0]["message"]["content"]
    except HTTPError as exc:
        _registrar_chamada(root, detail=detail, model=model, base_url=base_url,
                           operation="visual_comparison", erro=f"HTTP {exc.code}",
                           candidate_ids=[reference_id, candidate_id], **_http_error_context(exc))
        raise VisionGateError(f"API de comparacao visual respondeu HTTP {exc.code}") from exc
    except (URLError, OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        _registrar_chamada(root, detail=detail, model=model, base_url=base_url,
                           operation="visual_comparison", erro=type(exc).__name__,
                           candidate_ids=[reference_id, candidate_id])
        raise VisionGateError("falha ao chamar a comparacao visual") from exc
    _registrar_chamada(root, detail=detail, model=model, base_url=base_url,
                       operation="visual_comparison", usage=body.get("usage") or {},
                       candidate_ids=[reference_id, candidate_id])
    return _parse_visual_comparison(str(answer), reference_id=reference_id, candidate_id=candidate_id)


def compare_visual_assets(
    reference_image: str, candidate_image: str, *, reference_id: str = "", candidate_id: str = "",
    api_key: str, base_url: str, model: str, timeout: float = 30.0, root: Any = None,
    call_budget: list[int] | None = None, max_calls: int | None = None,
) -> VisualComparison:
    """Compare two materialised images; LOW first and HIGH only if uncertain.

    API/JSON failures are represented as ``ERROR`` so callers cannot mistake a
    technical failure for proof that assets are distinct.
    """
    def call(detail: str) -> VisualComparison:
        if call_budget is not None:
            if max_calls is not None and call_budget[0] >= max_calls:
                return VisualComparison("ERROR", 0.0, reference_id, candidate_id, "visual_comparison_budget_exhausted")
            call_budget[0] += 1
        return _call_visual_comparison(
            reference_image=reference_image, candidate_image=candidate_image,
            reference_id=reference_id, candidate_id=candidate_id, api_key=api_key,
            base_url=base_url, model=model, detail=detail, timeout=timeout, root=root,
        )
    try:
        result = call("low")
        if result.decision != "UNCERTAIN":
            return result
        return call("high")
    except (VisionGateError, VisionInputUnavailable) as exc:
        return VisualComparison("ERROR", 0.0, reference_id, candidate_id, str(exc)[:500])


def _call_vision_batch(
    *,
    items: list[dict[str, Any]],
    api_key: str,
    base_url: str,
    model: str,
    detail: str,
    timeout: float,
    root: Any = None,
) -> dict[str, dict[str, Any]]:
    """Evaluate several independent images with one HTTP request.

    The response is keyed by the caller-provided candidate id after strict
    validation. A missing, duplicated or unknown id is an error: silent
    positional correlation would be unsafe for editorial media.
    """
    if not items:
        return {}
    if len(items) > 20:
        raise VisionGateError("batch de visao excede o limite seguro de 20 imagens")
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "Validate each candidate independently. Metadata is context only.\n"
                + "\n".join(
                    f"candidate_id={item['candidate_id']} | expected_subject={str(item['subject']).strip()}"
                    for item in items
                )
            ),
        }
    ]
    for item in items:
        content.append(
            {
                "type": "text",
                "text": (
                    f"Candidate {item['candidate_id']}. Subject: {str(item['subject']).strip()}. "
                    "Judge this image only; do not transfer evidence from other candidates."
                    + (
                        " It is featured key art: reject a text-only news banner or infographic."
                        if item.get("require_key_art") else ""
                    )
                ),
            }
        )
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": str(item["image_url"]),
                    "detail": detail,
                },
            }
        )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _BATCH_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "max_tokens": max(60, 60 * len(items)),
        "temperature": 0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "vision_batch_result",
                "strict": True,
                "schema": _batch_json_output_schema(),
            },
        },
    }
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    request = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        error_context = _http_error_context(exc)
        _registrar_chamada(
            root, detail=detail, model=model, base_url=base_url,
            erro=f"HTTP {exc.code}", batch_size=len(items),
            candidate_ids=[str(item["candidate_id"]) for item in items],
            **error_context,
        )
        raise VisionGateError(f"API de visao respondeu HTTP {exc.code}") from exc
    except (URLError, OSError, ValueError) as exc:
        _registrar_chamada(
            root, detail=detail, model=model, base_url=base_url,
            erro=str(exc), batch_size=len(items),
        )
        raise VisionGateError(f"falha ao chamar a API de visao: {exc}") from exc
    try:
        answer = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        _registrar_chamada(
            root, detail=detail, model=model, base_url=base_url,
            erro="resposta invalida", batch_size=len(items),
        )
        raise VisionGateError("resposta invalida da API de visao em batch") from exc
    _registrar_chamada(
        root, detail=detail, model=model, base_url=base_url,
        usage=body.get("usage") or {}, batch_size=len(items),
    )
    try:
        parsed = json.loads(str(answer or "").strip())
    except ValueError as exc:
        raise VisionGateError("resposta batch de visao nao e JSON") from exc
    rows = parsed.get("items") if isinstance(parsed, dict) else None
    if not isinstance(rows, list):
        raise VisionGateError("resposta batch de visao nao possui items[]")
    expected = {str(item["candidate_id"]) for item in items}
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise VisionGateError("item invalido na resposta batch de visao")
        candidate_id = str(row.get("candidate_id") or "")
        if candidate_id not in expected or candidate_id in output:
            raise VisionGateError(f"candidate_id invalido ou duplicado: {candidate_id!r}")
        output[candidate_id] = _parse_response(
            json.dumps(row, ensure_ascii=False), allow_unknown_status=True
        )
    missing = expected.difference(output)
    if missing:
        raise VisionGateError(
            "resposta batch de visao sem candidatos: " + ", ".join(sorted(missing))
        )
    return output


def _registrar_chamada(
    root: Any,
    *,
    detail: str,
    model: str,
    base_url: str,
    usage: dict[str, Any] | None = None,
    erro: str = "",
    batch_size: int = 1,
    candidate_ids: list[str] | None = None,
    operation: str = "subject_validation",
    **error_context: str,
) -> None:
    """Evento de UMA chamada HTTP à API de visão (com tokens quando houver)."""
    if root is None:
        return
    try:
        from ..config import editorial_vision_price_per_1m
        from ..observability import append_telemetry, usage_cost_usd

        dados = usage or {}
        detalhes = dados.get("prompt_tokens_details") or {}
        entrada = int(dados.get("prompt_tokens") or 0)
        saida = int(dados.get("completion_tokens") or 0)
        preco_in, preco_out = editorial_vision_price_per_1m()
        custo = usage_cost_usd(
            entrada, saida, price_in_per_1m=preco_in, price_out_per_1m=preco_out
        )
        evento: dict[str, Any] = {
            "scope": "vision",
            "operation": str(operation),
            "detail": str(detail),
            "model": str(model),
            "provider": str(base_url)[:120],
            "input_tokens": entrada,
            "cached_tokens": int(detalhes.get("cached_tokens") or 0),
            "output_tokens": saida,
            "error": str(erro or "")[:160],
            "batch_size": max(1, int(batch_size or 1)),
        }
        if candidate_ids:
            evento["candidate_ids"] = [str(value)[:80] for value in candidate_ids[:20]]
        for key, value in error_context.items():
            if value:
                evento[key] = str(value)[:2000]
        # Custo da chamada DIRETA (a visao nao passa pelo Hermes): sem isto o teto
        # de USD do cron mede menos do que gastou.
        if custo is not None:
            evento["model_cost_usd"] = custo
        append_telemetry(root, "vision_api_request", **evento)
    except Exception:  # noqa: BLE001 - telemetria nunca quebra o gate
        pass


def _decide_tri(
    result: dict[str, Any], *, require_key_art: bool = False
) -> tuple[str, str]:
    """Decisão TRIPLA: ``accept`` | ``reject`` | ``inconclusive``.

    A decisão binária antiga colapsava "modelo não tem certeza" em ``True``, o
    que fazia ``verify_image_subject`` retornar ANTES de escalar para
    ``detail=high`` — justamente no caso (AMBIGUOUS / MATCH de baixa confiança /
    PARTIAL_MATCH) para o qual a escalada existe. Aqui o inconclusivo é um estado
    próprio: quem escala decide o que fazer com ele.
    """
    status = result["status"]
    confidence = result["confidence"]
    visual_type = result.get("visual_type", "other")
    detail = f"[{status} {confidence:.2f} {visual_type}]"
    # Key art nao pode ser um banner tipografico/infografico (card de manchete,
    # share card, infografico) mesmo que o texto cite a obra — nao e a arte da
    # obra em si. Preserva wordmarks/title-treatments legitimos (o modelo julga).
    if require_key_art and visual_type in {"text_banner", "infographic"}:
        return "reject", f"imagem e banner tipografico/infografico, nao key art {detail}"
    if status == "UNRELATED" and confidence >= _REJECT_THRESHOLD:
        return "reject", f"modelo NEGOU o assunto {detail}"
    if status == "MATCH" and confidence >= _ACCEPT_THRESHOLD:
        return "accept", f"modelo confirmou o assunto {detail}"
    return "inconclusive", f"inconclusivo (sem rejeicao clara) {detail}"


def _decide(result: dict[str, Any], *, require_key_art: bool = False) -> tuple[bool, str]:
    """Decisão binária (compatível): inconclusivo NÃO bloqueia.

    Mantida para chamadas que não escalam (visão inline). Quem tem escalada usa
    :func:`_decide_tri`.
    """
    veredito, razao = _decide_tri(result, require_key_art=require_key_art)
    return veredito != "reject", razao


def verify_image_subject(
    *,
    image_url: str,
    subject: str,
    api_key: str,
    base_url: str,
    model: str,
    timeout: float = 30.0,
    context: str = "",
    category: str = "",
    alt: str = "",
    detail: str = "low",
    allow_high: bool = False,
    require_key_art: bool = False,
    root: Any = None,
) -> tuple[bool, str]:
    """Ask the vision model whether ``image_url`` is consistent with ``subject``.

    Returns ``(ok, reason)``. Uses ``detail`` (default `low`). A decisão é
    TRIPLA: ACCEPT devolve na hora; INCONCLUSIVO escala para ``detail: high``
    (uma vez) quando ``allow_high``; REJECT bloqueia. Só depois do `high` sai a
    decisão final — antes o inconclusivo retornava como aceito e a escalada era
    inalcançável no caso que a motivou.
    Raises :class:`VisionGateError` on API failures (fail-closed).
    """
    if not api_key:
        raise VisionGateError("chave de visao ausente (gate habilitado sem chave)")
    if not _valid_image_input(image_url):
        raise VisionGateError(f"imagem sem input valido para verificacao: {image_url or 'vazio'}")
    if not subject or not subject.strip():
        raise VisionGateError("assunto (alt) vazio; impossivel verificar a imagem")

    detail = detail if detail in {"low", "high"} else "low"
    prompt = _build_user_prompt(
        subject, context=context, category=category, alt=alt,
        require_key_art=require_key_art,
    )
    result = _call_vision(
        image_url=image_url, prompt=prompt, api_key=api_key, base_url=base_url,
        model=model, detail=detail, timeout=timeout, root=root,
    )
    veredito, razao = _decide_tri(result, require_key_art=require_key_art)
    if veredito == "accept":
        return True, razao
    if veredito == "inconclusive" and allow_high:
        high_result = _call_vision(
            image_url=image_url, prompt=prompt, api_key=api_key, base_url=base_url,
            model=model, detail="high", timeout=timeout, root=root,
        )
        final, razao_final = _decide_tri(high_result, require_key_art=require_key_art)
        # Só o ACCEPT passa depois do high: "inconclusivo no detalhe máximo" não
        # é prova de que a imagem é a certa (é o gate cumprindo o que promete).
        return final == "accept", f"{razao} | high: {razao_final}"
    if veredito == "inconclusive":
        # Sem escalada permitida (inline): mantém a política histórica de não
        # prender o post em rework por falta de confiança do modelo.
        return True, razao
    return False, razao


def verify_image_subject_batch(
    *,
    items: list[dict[str, Any]],
    api_key: str,
    base_url: str,
    model: str,
    timeout: float = 30.0,
    detail: str = "low",
    allow_high: bool = False,
    root: Any = None,
) -> dict[str, dict[str, Any]]:
    """Validate independent image candidates in one request.

    The low-detail pass is always one HTTP request. When ``allow_high`` is
    enabled, only the inconclusive subset is sent in a second batch at high
    detail; definitive results are never repeated.
    """
    if not api_key:
        raise VisionGateError("chave de visao ausente (batch habilitado sem chave)")
    if not isinstance(items, list) or not items:
        raise VisionGateError("batch de visao vazio")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise VisionGateError("candidato invalido no batch de visao")
        candidate_id = str(item.get("candidate_id") or "").strip()
        image_url = str(item.get("image_url") or "").strip()
        subject = str(item.get("subject") or "").strip()
        if not candidate_id or candidate_id in seen:
            raise VisionGateError(f"candidate_id ausente ou duplicado: {candidate_id!r}")
        if not _valid_image_input(image_url):
            raise VisionGateError(f"imagem sem input valido: {candidate_id}")
        if not subject:
            raise VisionGateError(f"assunto vazio: {candidate_id}")
        seen.add(candidate_id)
        normalized.append(
            {
                "candidate_id": candidate_id,
                "image_url": image_url,
                "subject": subject,
                "require_key_art": bool(item.get("require_key_art")),
            }
        )
    detail = detail if detail in {"low", "high"} else "low"
    raw = _call_vision_batch(
        items=normalized,
        api_key=api_key,
        base_url=base_url,
        model=model,
        detail=detail,
        timeout=timeout,
        root=root,
    )
    output: dict[str, dict[str, Any]] = {}
    for item in normalized:
        candidate_id = item["candidate_id"]
        result = raw[candidate_id]
        verdict, reason = _decide_tri(
            result, require_key_art=bool(item.get("require_key_art"))
        )
        output[candidate_id] = {
            **result,
            "status": result["status"],
            "verdict": verdict,
            "ok": verdict == "accept",
            "reason": reason,
        }
    if allow_high:
        ambiguous = [
            item for item in normalized
            if output[item["candidate_id"]]["verdict"] == "inconclusive"
        ]
        if ambiguous:
            high_raw = _call_vision_batch(
                items=ambiguous,
                api_key=api_key,
                base_url=base_url,
                model=model,
                detail="high",
                timeout=timeout,
                root=root,
            )
            for item in ambiguous:
                candidate_id = item["candidate_id"]
                high_result = high_raw[candidate_id]
                verdict, reason = _decide_tri(
                    high_result,
                    require_key_art=bool(item.get("require_key_art")),
                )
                low = output[candidate_id]
                output[candidate_id] = {
                    **high_result,
                    "status": high_result["status"],
                    "verdict": verdict,
                    "ok": verdict == "accept",
                    "reason": low["reason"] + " | high: " + reason,
                    "low_status": low["status"],
                    "low_confidence": low["confidence"],
                }
    return output


def vision_config_ready(*, enabled: bool, api_key: str) -> tuple[bool, str]:
    """Report whether the vision gate is configured to run."""
    if not enabled:
        return False, "gate de visao desativado (EDITOR_VISION_ENABLED=false)"
    if not api_key:
        return False, "chave de visao ausente (OPENAI_API_KEY / EDITOR_VISION_API_KEY)"
    return True, "gate de visao ativo"


__all__ = [
    "verify_image_subject",
    "verify_image_subject_batch",
    "prepare_vision_image_input",
    "prepare_vision_image_input_from_path",
    "compare_visual_assets",
    "VisualComparison",
    "VISUAL_DECISIONS",
    "vision_config_ready",
    "VisionGateError",
    "VisionInputUnavailable",
]
