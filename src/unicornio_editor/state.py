"""Estado operacional dos posts no WordPress (fonte de verdade do pipeline).

O pipeline editorial evoluiu de marcadores de filesystem (editorial.latest.json
significava "pronto") para estados explícitos persistidos como meta no
WordPress. Somente ``_hermes_state = ready`` significa que o post está apto à
publicação; ``editorial.latest.json`` é apenas o rascunho editorial persistido.

Estados:

- NEW            post pending sem processamento (ou sem meta de estado)
- PROCESSING     reservado (apply é single-shot; não é gravado hoje)
- BLOCKED        preflight/apply recusou — precisa rework (re-edição)
- PARTIAL        relevância aprovada; enriquecimento incompleto com progresso
- READY          preflight completo passou — apto à publicação
- SKIPPED        relevância decidiu skip com confiança (decisão final)
- UNCERTAIN      primeira dúvida; retry automático controlado após cooldown
- AWAITING_HUMAN segunda dúvida ou tentativa esgotada — decisão humana
- PUBLISHED      publicado pelo cron

Meta persistida (chaves ``_hermes_*``):

- ``_hermes_state``             estado atual
- ``_hermes_attempts``          tentativas de rework consecutivas (apply)
- ``_hermes_next_retry_at``     ISO-8601 UTC; vazio = elegível agora
- ``_hermes_last_error``        resumo do último erro/bloqueio
- ``_hermes_ready_hash``        SHA-256 do Ready Manifest (integridade)
- ``_hermes_policy_version``    versão da política que gerou o READY
- ``_hermes_processed_at``      ISO-8601 UTC da última transição
"""

from __future__ import annotations

import datetime
import json
from typing import Any

STATE_NEW = "new"
STATE_PROCESSING = "processing"
STATE_BLOCKED = "blocked"
STATE_PARTIAL = "partial"
STATE_READY = "ready"
STATE_SKIPPED = "skipped"
STATE_UNCERTAIN = "uncertain"
STATE_AWAITING_HUMAN = "awaiting_human"
STATE_PUBLISHED = "published"

ALL_STATES = frozenset(
    {
        STATE_NEW,
        STATE_PROCESSING,
        STATE_BLOCKED,
        STATE_PARTIAL,
        STATE_READY,
        STATE_SKIPPED,
        STATE_UNCERTAIN,
        STATE_AWAITING_HUMAN,
        STATE_PUBLISHED,
    }
)

META_STATE = "_hermes_state"
META_ATTEMPTS = "_hermes_attempts"
META_MEDIA_SEARCH_ATTEMPTS = "_hermes_media_search_attempts"
META_NEXT_RETRY = "_hermes_next_retry_at"
META_LAST_ERROR = "_hermes_last_error"
META_READY_HASH = "_hermes_ready_hash"
META_POLICY_VERSION = "_hermes_policy_version"
META_PROCESSED_AT = "_hermes_processed_at"
META_PARTIAL_KIND = "_hermes_partial_kind"
META_PARTIAL_REQUIRED = "_hermes_media_required"
META_PARTIAL_COMPLETED = "_hermes_media_completed"
META_PARTIAL_MISSING = "_hermes_media_missing"
META_PARTIAL_PROCESSING_PASSES = "_hermes_processing_passes"
META_PARTIAL_NO_PROGRESS = "_hermes_no_progress_attempts"

def now_iso() -> str:
    """ISO-8601 UTC com segundos (formato usado nas meta keys)."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def build_state_markers(
    state: str,
    *,
    attempts: int = 0,
    next_retry_at: str = "",
    last_error: str = "",
    ready_hash: str = "",
    policy_version: int = 0,
    processed_at: str | None = None,
    media_search_attempts: int | None = None,
    partial_kind: str = "",
    partial_required: int | None = None,
    partial_completed: int | None = None,
    partial_missing: int | None = None,
    processing_passes: int | None = None,
    no_progress_attempts: int | None = None,
) -> dict[str, Any]:
    """Meta payload ``_hermes_*`` para um update no WordPress."""
    if state not in ALL_STATES:
        raise ValueError(f"unknown state: {state!r}")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        raise ValueError("attempts must be a non-negative integer")
    markers: dict[str, Any] = {
        META_STATE: state,
        # WP REST exige string para meta registrada (tipo 'string'); o read
        # converte de volta com _int_or/_str_or.
        META_ATTEMPTS: str(attempts),
        META_NEXT_RETRY: next_retry_at or "",
        META_LAST_ERROR: last_error or "",
        META_READY_HASH: ready_hash or "",
        META_POLICY_VERSION: str(policy_version),
        META_PROCESSED_AT: processed_at or now_iso(),
    }
    if state == STATE_READY:
        if not ready_hash:
            raise ValueError("ready state requires a ready hash")
        if not policy_version:
            raise ValueError("ready state requires a policy version")
    if media_search_attempts is not None:
        if isinstance(media_search_attempts, bool) or not isinstance(media_search_attempts, int) or media_search_attempts < 0:
            raise ValueError("media_search_attempts must be a non-negative integer")
        markers[META_MEDIA_SEARCH_ATTEMPTS] = str(media_search_attempts)
    if no_progress_attempts is not None:
        markers[META_PARTIAL_NO_PROGRESS] = str(max(0, no_progress_attempts))
    if partial_kind:
        markers[META_PARTIAL_KIND] = partial_kind
    for key, value in (
        (META_PARTIAL_REQUIRED, partial_required),
        (META_PARTIAL_COMPLETED, partial_completed),
        (META_PARTIAL_MISSING, partial_missing),
        (META_PARTIAL_PROCESSING_PASSES, processing_passes),
    ):
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{key} must be a non-negative integer")
            markers[key] = str(value)
    # UNCERTAIN is retryable after its controlled cooldown. Clearing
    # next_retry_at here would make every uncertain post immediately eligible
    # and defeat the backoff policy.
    if state in (STATE_READY, STATE_PUBLISHED, STATE_SKIPPED):
        markers[META_NEXT_RETRY] = ""
    return markers


def read_state(post: dict[str, Any]) -> dict[str, Any]:
    """Estado do post a partir da meta; tolerante a qualquer formato inválido.

    Retorna ``{state, attempts, next_retry_at, last_error, ready_hash,
    policy_version, processed_at, partial_*}``. ``state`` é ``None`` quando o
    post não tem meta de estado (legado — o pipeline decide pelo filesystem).
    """
    meta = post.get("meta")
    if not isinstance(meta, dict):
        meta = {}
    state = meta.get(META_STATE)
    if state is not None and state not in ALL_STATES:
        state = None
    return {
        "state": state,
        "attempts": _int_or(meta.get(META_ATTEMPTS), 0),
        "media_search_attempts": _int_or(meta.get(META_MEDIA_SEARCH_ATTEMPTS), 0),
        "next_retry_at": _str_or(meta.get(META_NEXT_RETRY)),
        "last_error": _str_or(meta.get(META_LAST_ERROR)),
        "ready_hash": _str_or(meta.get(META_READY_HASH)),
        "policy_version": _int_or(meta.get(META_POLICY_VERSION), 0),
        "processed_at": _str_or(meta.get(META_PROCESSED_AT)),
        "partial_kind": _str_or(meta.get(META_PARTIAL_KIND)),
        "partial_required": _int_or(meta.get(META_PARTIAL_REQUIRED), 0),
        "partial_completed": _int_or(meta.get(META_PARTIAL_COMPLETED), 0),
        "partial_missing": _int_or(meta.get(META_PARTIAL_MISSING), 0),
        "processing_passes": _int_or(meta.get(META_PARTIAL_PROCESSING_PASSES), 0),
        "no_progress_attempts": _int_or(meta.get(META_PARTIAL_NO_PROGRESS), 0),
    }


def rework_backoff(
    attempts: int,
    *,
    cooldown_minutes: int,
    max_attempts: int,
    now: datetime.datetime | None = None,
) -> dict[str, Any]:
    """Próxima janela de rework após uma falha do apply.

    Política (defaults EDITOR_REWORK_COOLDOWN_MINUTES=30,
    EDITOR_MAX_REWORK_ATTEMPTS=3):

    - 1ª falha  -> BLOCKED, next_retry_at = now + 30m
    - 2ª falha  -> BLOCKED, next_retry_at = now + 2h (30m * 4)
    - 3ª falha  -> AWAITING_HUMAN (esgotou; decisão humana via ``retry``)

    Retorna ``{state, attempts, next_retry_at}``.
    """
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
        raise ValueError("attempts must be a positive integer")
    if cooldown_minutes < 1 or max_attempts < 1:
        raise ValueError("cooldown and max_attempts must be positive")
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if attempts >= max_attempts:
        return {"state": STATE_AWAITING_HUMAN, "attempts": attempts, "next_retry_at": ""}
    multiplier = 4 ** (attempts - 1)  # 1 -> 30m, 2 -> 2h (com cooldown=30)
    retry_at = now + datetime.timedelta(minutes=cooldown_minutes * multiplier)
    return {
        "state": STATE_BLOCKED,
        "attempts": attempts,
        "next_retry_at": retry_at.isoformat(timespec="seconds"),
    }


def cooldown_expired(next_retry_at: str, now: datetime.datetime | None = None) -> bool:
    """True quando o cooldown liberou: valor vazio (nunca agendado) ou já passado."""
    if not next_retry_at:
        return True
    try:
        parsed = datetime.datetime.fromisoformat(next_retry_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed <= (now or datetime.datetime.now(datetime.timezone.utc))


def retry_eligible(state_info: dict[str, Any], now: datetime.datetime | None = None) -> bool:
    """True quando o post bloqueado pode voltar à agenda do monitor.

    BLOCKED com ``next_retry_at`` vazio (legado/bloqueio por publish) ou já
    vencido é elegível; BLOCKED ainda em cooldown não é.
    """
    if state_info.get("state") not in (STATE_BLOCKED, STATE_PARTIAL):
        return False
    return cooldown_expired(state_info.get("next_retry_at") or "", now)


def uncertain_second_pass_eligible(
    post: dict[str, Any], now: datetime.datetime | None = None
) -> bool:
    """Return true only for the bounded UNCERTAIN second-pass contract."""
    if post.get("status") != "pending":
        return False
    info = read_state(post)
    return (
        info.get("state") == STATE_UNCERTAIN
        and int(info.get("attempts") or 0) == 1
        and cooldown_expired(info.get("next_retry_at") or "", now)
    )



def canonical_json(value: Any) -> str:
    """Serialização canônica (chaves ordenadas, sem espaços) para hashing."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _int_or(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return max(0, value)
    # WP REST devolve meta como string mesmo para numeros.
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return default


def _str_or(value: Any) -> str:
    return value if isinstance(value, str) else ""
