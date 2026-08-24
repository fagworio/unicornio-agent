"""Estado operacional dos posts no WordPress (fonte de verdade do pipeline).

O pipeline editorial evoluiu de marcadores de filesystem (editorial.latest.json
significava "pronto") para estados explícitos persistidos como meta no
WordPress. Somente ``_hermes_state = ready`` significa que o post está apto à
publicação; ``editorial.latest.json`` é apenas o rascunho editorial persistido.

Estados:

- NEW            post pending sem processamento (ou sem meta de estado)
- PROCESSING     reservado (apply é single-shot; não é gravado hoje)
- BLOCKED        preflight/apply recusou — precisa rework (re-edição)
- READY          preflight completo passou — apto à publicação
- SKIPPED        relevância decidiu skip com confiança (decisão final)
- UNCERTAIN      não-final: fora da fila, visível para revisão
- AWAITING_HUMAN esgotou as tentativas automáticas de rework — decisão humana
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
        STATE_READY,
        STATE_SKIPPED,
        STATE_UNCERTAIN,
        STATE_AWAITING_HUMAN,
        STATE_PUBLISHED,
    }
)

META_STATE = "_hermes_state"
META_ATTEMPTS = "_hermes_attempts"
META_NEXT_RETRY = "_hermes_next_retry_at"
META_LAST_ERROR = "_hermes_last_error"
META_READY_HASH = "_hermes_ready_hash"
META_POLICY_VERSION = "_hermes_policy_version"
META_PROCESSED_AT = "_hermes_processed_at"

# Estados que saem da fila de trabalho (não acordam o agente, não publicam).
_OUT_OF_QUEUE = frozenset(
    {STATE_UNCERTAIN, STATE_AWAITING_HUMAN, STATE_SKIPPED, STATE_PUBLISHED, STATE_READY}
)


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
) -> dict[str, Any]:
    """Meta payload ``_hermes_*`` para um update no WordPress."""
    if state not in ALL_STATES:
        raise ValueError(f"unknown state: {state!r}")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        raise ValueError("attempts must be a non-negative integer")
    markers: dict[str, Any] = {
        META_STATE: state,
        META_ATTEMPTS: attempts,
        META_NEXT_RETRY: next_retry_at or "",
        META_LAST_ERROR: last_error or "",
        META_READY_HASH: ready_hash or "",
        META_POLICY_VERSION: policy_version,
        META_PROCESSED_AT: processed_at or now_iso(),
    }
    if state == STATE_READY:
        if not ready_hash:
            raise ValueError("ready state requires a ready hash")
        if not policy_version:
            raise ValueError("ready state requires a policy version")
    if state in (STATE_READY, STATE_PUBLISHED, STATE_SKIPPED, STATE_UNCERTAIN):
        markers[META_NEXT_RETRY] = ""
    return markers


def read_state(post: dict[str, Any]) -> dict[str, Any]:
    """Estado do post a partir da meta; tolerante a qualquer formato inválido.

    Retorna ``{state, attempts, next_retry_at, last_error, ready_hash,
    policy_version, processed_at}``. ``state`` é ``None`` quando o post não
    tem meta de estado (legado — o pipeline decide pelo filesystem).
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
        "next_retry_at": _str_or(meta.get(META_NEXT_RETRY)),
        "last_error": _str_or(meta.get(META_LAST_ERROR)),
        "ready_hash": _str_or(meta.get(META_READY_HASH)),
        "policy_version": _int_or(meta.get(META_POLICY_VERSION), 0),
        "processed_at": _str_or(meta.get(META_PROCESSED_AT)),
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


def retry_eligible(state_info: dict[str, Any], now: datetime.datetime | None = None) -> bool:
    """True quando o post bloqueado pode voltar à agenda do monitor.

    BLOCKED com ``next_retry_at`` vazio (legado/bloqueio por publish) ou já
    vencido é elegível; BLOCKED ainda em cooldown não é.
    """
    if state_info.get("state") != STATE_BLOCKED:
        return False
    next_retry = state_info.get("next_retry_at") or ""
    if not next_retry:
        return True
    try:
        parsed = datetime.datetime.fromisoformat(next_retry.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed <= (now or datetime.datetime.now(datetime.timezone.utc))


def out_of_queue(state: str | None) -> bool:
    """Estados que não entram na fila de trabalho do agente."""
    return state in _OUT_OF_QUEUE


def canonical_json(value: Any) -> str:
    """Serialização canônica (chaves ordenadas, sem espaços) para hashing."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _int_or(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(0, value)


def _str_or(value: Any) -> str:
    return value if isinstance(value, str) else ""
