"""Vision gate: confirms an image depicts its caption with a vision LLM.

The deterministic gates (relevance text, source-page verification) cannot
catch a CDN that serves the wrong image under a correct slug at the exact
moment of download. This final belt-and-suspenders gate asks a cheap vision
model whether the actual published image depicts the subject named in its
alt/caption. Fail-closed: API errors block publication with the reason.

Any OpenAI-compatible vision endpoint works (OpenAI, Gemini via
``OPENAI_COMPAT`` base URL, local vLLM, ...), configured through
``EDITOR_VISION_*`` env vars.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class VisionGateError(RuntimeError):
    """Raised when the vision model cannot confirm the image subject."""


_SYSTEM_PROMPT = (
    "Você é um verificador de imagens editoriais. Responda apenas com a palavra "
    "SIM ou NÃO, sem pontuação e sem explicações."
)


def _confirm_yes_no(text: str) -> bool | None:
    match = re.search(r"\b(sim|não|nao)\b|\b(yes|no)\b", (text or "").strip().lower())
    if not match:
        return None
    return match.group(1) in {"sim", "yes"}


def verify_image_subject(
    *,
    image_url: str,
    subject: str,
    api_key: str,
    base_url: str,
    model: str,
    timeout: float = 30.0,
) -> tuple[bool, str]:
    """Ask the vision model whether ``image_url`` depicts ``subject``.

    Returns ``(ok, reason)`` where ``ok`` is True only when the model
    confirms the subject. Raises :class:`VisionGateError` on API failures
    (fail-closed: the caller must not publish unverified images).
    """
    if not api_key:
        raise VisionGateError("EDITOR_VISION_API_KEY ausente (gate de visao habilitado sem chave)")
    if not image_url or not image_url.startswith(("http://", "https://")):
        raise VisionGateError(f"imagem sem URL valida para verificacao: {image_url or 'vazio'}")
    if not subject or not subject.strip():
        raise VisionGateError("assunto (alt) vazio; impossivel verificar a imagem")

    prompt = (
        f"A imagem abaixo retrata: {subject.strip()}. "
        "Se a imagem retratar exatamente isso, responda SIM. Caso contrario, responda NAO."
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            },
        ],
        "max_tokens": 8,
        "temperature": 0,
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
    confirmed = _confirm_yes_no(answer)
    if confirmed is None:
        raise VisionGateError(f"resposta inconclusiva da API de visao: {answer!r}")
    if confirmed:
        return True, "modelo de visao confirmou o assunto da imagem"
    return False, f"modelo de visao NEGOU o assunto ({subject.strip()[:80]})"


def vision_config_ready(*, enabled: bool, api_key: str) -> tuple[bool, str]:
    """Report whether the vision gate is configured to run."""
    if not enabled:
        return False, "gate de visao desativado (EDITOR_VISION_ENABLED=false)"
    if not api_key:
        return False, "EDITOR_VISION_API_KEY ausente (gate habilitado sem chave)"
    return True, "gate de visao ativo"
