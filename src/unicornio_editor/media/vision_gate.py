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
        raise VisionGateError(f"API de visao respondeu HTTP {exc.code}") from exc
    except (URLError, OSError, ValueError) as exc:
        raise VisionGateError(f"falha ao chamar a API de visao: {exc}") from exc
    try:
        answer = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise VisionGateError("resposta invalida da API de visao") from exc
    return _parse_response(answer)


def _decide(result: dict[str, Any], *, require_key_art: bool = False) -> tuple[bool, str]:
    status = result["status"]
    confidence = result["confidence"]
    visual_type = result.get("visual_type", "other")
    detail = f"[{status} {confidence:.2f} {visual_type}]"
    # Key art nao pode ser um banner tipografico/infografico (card de manchete,
    # share card, infografico) mesmo que o texto cite a obra — nao e a arte da
    # obra em si. Preserva wordmarks/title-treatments legitimos (o modelo julga).
    if require_key_art and visual_type in {"text_banner", "infographic"}:
        return False, f"imagem e banner tipografico/infografico, nao key art {detail}"
    if status == "UNRELATED" and confidence >= _REJECT_THRESHOLD:
        return False, f"modelo NEGOU o assunto {detail}"
    # MATCH alto -> aceita.
    if status == "MATCH" and confidence >= _ACCEPT_THRESHOLD:
        return True, f"modelo confirmou o assunto {detail}"
    # Inconclusivo (MATCH baixo, PARTIAL_MATCH, AMBIGUOUS ou UNRELATED de baixa
    # confianca) NAO bloqueia: a imagem pode estar correta, apenas o modelo
    # nao esta confiante. Passa com aviso para nao prender posts em rework.
    return True, f"inconclusivo (sem rejeicao clara) {detail}"


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
) -> tuple[bool, str]:
    """Ask the vision model whether ``image_url`` is consistent with ``subject``.

    Returns ``(ok, reason)``. Uses ``detail`` (default `low`). When the
    result is ambiguous/inconclusive and ``allow_high`` is True, re-asks at
    `detail: high` once before deciding (cost escalation only on hard cases).
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
        model=model, detail=detail, timeout=timeout,
    )
    ok, reason = _decide(result, require_key_art=require_key_art)
    if ok or not allow_high:
        return ok, reason
    if result["status"] == "AMBIGUOUS" or result["confidence"] < _ACCEPT_THRESHOLD:
        # Escalate to high once for the hard cases.
        high_result = _call_vision(
            image_url=image_url, prompt=prompt, api_key=api_key, base_url=base_url,
            model=model, detail="high", timeout=timeout,
        )
        return _decide(high_result, require_key_art=require_key_art)
    return ok, reason


def vision_config_ready(*, enabled: bool, api_key: str) -> tuple[bool, str]:
    """Report whether the vision gate is configured to run."""
    if not enabled:
        return False, "gate de visao desativado (EDITOR_VISION_ENABLED=false)"
    if not api_key:
        return False, "chave de visao ausente (OPENAI_API_KEY / EDITOR_VISION_API_KEY)"
    return True, "gate de visao ativo"


__all__ = ["verify_image_subject", "vision_config_ready", "VisionGateError"]
