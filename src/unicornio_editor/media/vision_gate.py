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

import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class VisionGateError(RuntimeError):
    """Raised when the vision model cannot confirm the image subject."""


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


def _parse_response(text: str) -> dict[str, Any]:
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
    if status not in _STATUS:
        raise VisionGateError(f"status de visao desconhecido: {status!r}")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise VisionGateError(f"confidence de visao invalida: {confidence!r}")
    confidence = float(confidence)
    if not 0 <= confidence <= 1:
        raise VisionGateError(f"confidence fora de [0,1]: {confidence!r}")
    if visual_type not in _VISUAL_TYPES:
        visual_type = "other"
    return {"status": status, "confidence": confidence, "visual_type": visual_type}


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
        _registrar_chamada(root, detail=detail, model=model, base_url=base_url,
                           erro=f"HTTP {exc.code}")
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
        _registrar_chamada(
            root, detail=detail, model=model, base_url=base_url,
            erro=f"HTTP {exc.code}", batch_size=len(items),
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
        output[candidate_id] = _parse_response(json.dumps(row, ensure_ascii=False))
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
) -> None:
    """Evento de UMA chamada HTTP à API de visão (com tokens quando houver)."""
    if root is None:
        return
    try:
        from ..observability import append_telemetry

        dados = usage or {}
        detalhes = dados.get("prompt_tokens_details") or {}
        append_telemetry(
            root,
            "vision_api_request",
            scope="vision",
            detail=str(detail),
            model=str(model),
            provider=str(base_url)[:120],
            input_tokens=int(dados.get("prompt_tokens") or 0),
            cached_tokens=int(detalhes.get("cached_tokens") or 0),
            output_tokens=int(dados.get("completion_tokens") or 0),
            error=str(erro or "")[:160],
            batch_size=max(1, int(batch_size or 1)),
        )
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
    if not image_url or not image_url.startswith(("http://", "https://")):
        raise VisionGateError(f"imagem sem URL valida para verificacao: {image_url or 'vazio'}")
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
        if not image_url.startswith(("http://", "https://")):
            raise VisionGateError(f"imagem sem URL valida: {candidate_id}")
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
    "vision_config_ready",
    "VisionGateError",
]
