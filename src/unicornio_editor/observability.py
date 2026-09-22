"""Structured, secret-redacted processing telemetry."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TextIO

_VERSION = "0.1.0"
_SENSITIVE_PARTS = ("password", "token", "secret", "authorization", "cookie", "api_key")


def build_processing_markers(
    decision: str,
    confidence: float,
    *,
    processed_at: str | None = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    if decision not in {"process", "skip"}:
        raise ValueError("decision must be process or skip")
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    return {
        "_ai_editor_version": _VERSION,
        "_ai_editor_decision": decision,
        "_ai_editor_confidence": str(confidence),
        "_ai_editor_processed_at": processed_at or datetime.now(timezone.utc).isoformat(),
        "_ai_editor_correlation_id": correlation_id or str(uuid.uuid4()),
    }


def log_event(stream: TextIO, event: str, **fields: Any) -> None:
    safe_fields = {
        key: value for key, value in fields.items() if not _is_sensitive(key)
    }
    record = {"event": event, **safe_fields}
    stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def append_event(path: Path, event: str, **fields: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        log_event(stream, event, **fields)


# ---------------------------------------------------------------------------
# Telemetria central da fila de publicacao (blocagens / resultados).
# ---------------------------------------------------------------------------
TELEMETRY_FILENAME = "telemetry.jsonl"


def telemetry_path(root: str | Path) -> Path:
    return Path(root) / "work" / TELEMETRY_FILENAME


def append_telemetry(root: str | Path, event: str, **fields: Any) -> None:
    """Registra um evento do pipeline no telemetry.jsonl central (fail-soft)."""
    append_event(
        telemetry_path(root),
        event,
        ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **fields,
    )


def read_telemetry_summary(
    root: str | Path, *, hours: int | None = None
) -> dict[str, Any]:
    """Resumo agregado do telemetry.jsonl (contadores por evento e motivo).

    Inclui o contexto POR POST e POR COMANDO (P0 da auditoria de contexto): sem
    isso o operador via "75 milhoes de tokens" sem conseguir apontar quantos
    bytes cada etapa colocou na conversa de cada post.

    ``hours`` limita a janela (mesma janela do gasto que vem do state.db): sem
    ela um total historico seria dividido pelos READY de 24h — numero bonito e
    errado. Registros sem timestamp entram (nao descartamos evidencia por falta
    de campo).
    """
    limite = None
    if hours and int(hours) > 0:
        limite = datetime.now(timezone.utc) - timedelta(hours=int(hours))
    path = telemetry_path(root)
    counts: dict[str, int] = {}
    reasons: dict[str, dict[str, int]] = {}
    cmd_bytes: dict[str, int] = {}
    cmd_bytes_kind: dict[str, int] = {}
    post_cmd_bytes: dict[str, dict[str, int]] = {}
    post_totals: dict[str, int] = {}
    total_cmd_bytes = 0
    last_ts: str | None = None
    started_posts: set[int] = set()
    blocked_posts: set[int] = set()
    ready_posts: set[int] = set()
    ready_with_first_pass = 0
    first_pass_ready = 0
    ready_attempts = 0
    ready_durations: list[int] = []
    media_funnel: dict[str, dict[str, int]] = {}
    media_by_domain: dict[str, dict[str, int]] = {}
    # Economia de midia (unidade: por BUSCA e por READY). `deferred` e contado
    # SEPARADO de `rejected`: candidato dispensado por capacidade ja atendida
    # nao foi investigado, entao nao e rejeicao — somar os dois faria a busca
    # parecer pior justamente quando ficou mais eficiente.
    midia = {
        "searches": 0,
        "searches_with_web": 0,
        "engines_queried": 0,
        "needed_total": 0,
        "reuse_total": 0,
        "strong_total": 0,
        "ambiguous_total": 0,
        "accepted_total": 0,
        "rejected_total": 0,
        "deferred_total": 0,
        "examined_total": 0,
        "vision_calls": 0,
    }
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            event = record.get("event")
            if not isinstance(event, str):
                continue
            if limite is not None:
                quando = _timestamp(record.get("ts"))
                if quando is not None and quando < limite:
                    continue
            counts[event] = counts.get(event, 0) + 1
            reason = record.get("reason")
            if isinstance(reason, str) and reason.strip():
                bucket = reasons.setdefault(event, {})
                bucket[reason] = bucket.get(reason, 0) + 1
            if event == "cmd_output":
                command = record.get("command")
                size = record.get("bytes")
                if isinstance(command, str) and isinstance(size, int):
                    cmd_bytes[command] = cmd_bytes.get(command, 0) + size
                    total_cmd_bytes += size
                    kind = record.get("kind")
                    chave_kind = kind if isinstance(kind, str) and kind else "read"
                    cmd_bytes_kind[chave_kind] = cmd_bytes_kind.get(chave_kind, 0) + size
                    cmd_post = record.get("post_id")
                    if isinstance(cmd_post, int):
                        detalhe = post_cmd_bytes.setdefault(str(cmd_post), {})
                        detalhe[command] = detalhe.get(command, 0) + size
                        post_totals[str(cmd_post)] = post_totals.get(str(cmd_post), 0) + size
            post_id = record.get("post_id")
            if isinstance(post_id, int):
                if event == "apply_started":
                    started_posts.add(post_id)
                elif event == "apply_blocked":
                    blocked_posts.add(post_id)
                elif event == "apply_ready":
                    ready_posts.add(post_id)
            if event == "apply_ready":
                first_pass = record.get("first_pass")
                if isinstance(first_pass, bool):
                    ready_with_first_pass += 1
                    first_pass_ready += int(first_pass)
                attempts = record.get("attempts")
                if isinstance(attempts, int):
                    ready_attempts += attempts
                duration = record.get("duration_ms")
                if isinstance(duration, int):
                    ready_durations.append(duration)
            if event == "media_search_result":
                midia["searches"] += 1
                for campo in ("needed", "reuse", "strong", "ambiguous", "accepted",
                              "rejected", "deferred", "examined", "engines_queried"):
                    valor = record.get(campo)
                    if isinstance(valor, int):
                        midia[f"{campo}_total" if campo in {"needed", "reuse", "strong",
                                                            "ambiguous", "accepted",
                                                            "rejected", "deferred",
                                                            "examined"} else campo] += valor
                if isinstance(record.get("engines_queried"), int) and record["engines_queried"] > 0:
                    midia["searches_with_web"] += 1
            if event == "vision_call":
                midia["vision_calls"] += 1
            if event == "media_funnel":
                stage = record.get("stage")
                status = record.get("status")
                domain = record.get("source_domain")
                if isinstance(stage, str) and isinstance(status, str):
                    stage_bucket = media_funnel.setdefault(stage, {})
                    stage_bucket[status] = stage_bucket.get(status, 0) + 1
                    if isinstance(domain, str) and domain:
                        domain_bucket = media_by_domain.setdefault(domain, {})
                        key = f"{stage}:{status}"
                        domain_bucket[key] = domain_bucket.get(key, 0) + 1
            ts = record.get("ts")
            if isinstance(ts, str):
                last_ts = ts
    return {
        "file": str(path),
        "total_events": sum(counts.values()),
        "by_event": counts,
        "by_reason": reasons,
        "context_bytes_by_command": cmd_bytes,
        "context_bytes_by_kind": cmd_bytes_kind,
        "context_bytes_by_post": post_totals,
        "post_context_detail": post_cmd_bytes,
        "context_bytes_total": total_cmd_bytes,
        "context_bytes_per_ready": (
            round(total_cmd_bytes / len(ready_posts)) if ready_posts else None
        ),
        "production": {
            "unique_started_posts": len(started_posts),
            "unique_blocked_posts": len(blocked_posts),
            "unique_ready_posts": len(ready_posts),
            "unique_touched_posts": len(started_posts | blocked_posts | ready_posts),
            "first_pass_ready": first_pass_ready,
            "first_pass_ready_rate": (
                round(first_pass_ready / ready_with_first_pass, 4)
                if ready_with_first_pass else None
            ),
            "average_attempts_per_ready": (
                round(ready_attempts / ready_with_first_pass, 2)
                if ready_with_first_pass else None
            ),
            "average_ready_duration_ms": (
                round(sum(ready_durations) / len(ready_durations))
                if ready_durations else None
            ),
        },
        "media_funnel": media_funnel,
        "media_by_domain": media_by_domain,
        "media_economy": {
            **midia,
            "local_reuse_rate": (
                round(midia["reuse_total"] / midia["needed_total"], 4)
                if midia["needed_total"] else None
            ),
        },
        "last_event_at": last_ts,
    }


def _timestamp(valor: Any) -> datetime | None:
    """ISO-8601 do telemetry.jsonl -> datetime (None quando ausente/invalido)."""
    if not isinstance(valor, str) or not valor:
        return None
    try:
        quando = datetime.fromisoformat(valor)
    except ValueError:
        return None
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=timezone.utc)
    return quando


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_PARTS)
