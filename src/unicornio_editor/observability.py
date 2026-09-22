"""Structured, secret-redacted processing telemetry."""

from __future__ import annotations

import json
import os
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


_DECISIONS_FILENAME = "media_decisions.json"

# Gates que representam problema de MIDIA no bloqueio do apply (usado para medir
# a taxa de bloqueio por decisao de busca: auto/reuse nao podem piorar isso).
_MEDIA_GATES = frozenset({
    "imagens_no_corpo", "relevancia_imagens", "imagens_duplicadas",
    "imagens_similares", "imagens_webp", "imagens_visao", "dimensoes_imagens",
    "destaque_relevancia",
})


def decisions_path(root: str | Path) -> Path:
    return Path(root) / "work" / _DECISIONS_FILENAME


_RUN_CONTEXT_MEMO: dict[str, dict[str, Any]] = {}


def _session_source(session_id: str) -> str:
    """``source`` da sessão no state.db do Hermes (autoritativo), com cache.

    O id da sessão do agente pode ser o da sessão principal ou o de uma sessão
    FILHA (subagente/ferramenta) — por isso o prefixo do id não basta. A coluna
    `source` do banco é a classificação autoritativa, e `parent_session_id`
    permite subir a cadeia até a raiz. Uma consulta por processo (memoizada),
    somente-leitura: se o banco não existir/estiver quebrado devolve "".
    """
    if not session_id:
        return ""
    if session_id in _RUN_CONTEXT_MEMO:
        return str(_RUN_CONTEXT_MEMO[session_id].get("source") or "")
    banco = os.environ.get("HERMES_STATE_DB") or str(Path.home() / ".hermes" / "state.db")
    atual = session_id
    fonte = ""
    try:
        import sqlite3

        db = sqlite3.connect(f"file:{banco}?mode=ro", uri=True)
        try:
            for _ in range(5):  # cadeia de subagentes é curta; evita laço infinito
                linha = db.execute(
                    "SELECT source, parent_session_id FROM sessions WHERE id = ?",
                    (atual,),
                ).fetchone()
                if not linha:
                    break
                fonte = str(linha[0] or "")
                pai = str(linha[1] or "")
                if not pai or pai == atual:
                    break
                if fonte == "cron":
                    break
                atual = pai
        finally:
            db.close()
    except Exception:  # noqa: BLE001 - classificação é instrumentação
        fonte = ""
    _RUN_CONTEXT_MEMO[session_id] = {"source": fonte, "root_id": atual}
    return fonte


def run_context() -> dict[str, Any]:
    """Identifica a ORIGEM da execução (cron x manual x teste).

    Sem isso a telemetria mistura o cron editorial com execuções manuais e
    verificações — o KPI oficial (`tool_context_bytes_per_ready`) ficava
    contaminado justamente pelo trabalho de quem investiga o problema.

    A classificação usa, nesta ordem: ``UNICORNIO_RUN_SOURCE`` (override
    explícito, usado pelos testes), a coluna ``source`` da sessão no state.db
    (autoritativa, subindo a cadeia de `parent_session_id` quando o id é de uma
    sessão filha) e, por último, o prefixo do id (``cron_<job>_<data>_<hora>``).
    """
    sessao = str(os.environ.get("HERMES_SESSION_ID") or "").strip()
    job = str(os.environ.get("HERMES_EDITORIAL_CRON_JOB_ID") or "").strip()
    origem = str(os.environ.get("UNICORNIO_RUN_SOURCE") or "").strip().lower()
    fonte_db = "" if origem else _session_source(sessao)
    if origem:
        pass
    elif fonte_db == "cron":
        origem = "cron"
    elif fonte_db in {"cli", "telegram", "whatsapp", "api", "web"}:
        origem = "manual"
    elif fonte_db == "subagent":
        # Sessão filha sem raiz 'cron' conhecida: fato próprio, não "cron".
        origem = "subagent"
    elif fonte_db:
        origem = fonte_db
    elif sessao.startswith("cron_"):
        origem = "cron"
    elif sessao:
        origem = "manual"
    else:
        origem = "unknown"
    if origem == "cron":
        # cron_<job_id>_<YYYYMMDD>_<HHMMSS>
        partes = sessao.split("_")
        if len(partes) >= 3 and partes[0] == "cron":
            job = partes[1]
        elif not job:
            job = ""
    else:
        job = ""
    return {"run_source": origem, "session_id": sessao, "cron_job_id": job}


def record_media_decision(
    root: str | Path,
    post_id: int,
    *,
    decision: str,
    score_gap: int | None = None,
    query: str = "",
) -> None:
    """Guarda a decisão de mídia do post (auto/choose/reuse/none).

    É o que permite CRUZAR economia com QUALIDADE depois: os gates seguintes
    (media-validate, apply, first-pass) carregam essa decisão nos eventos, então
    dá para comparar taxa de rejeição/bloqueio entre `auto` e `choose` — a prova
    de que dispensar julgamento não piorou a imagem escolhida.
    """
    if not post_id:
        return
    caminho = decisions_path(root)
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
        if not isinstance(dados, dict):
            dados = {}
    except (OSError, ValueError):
        dados = {}
    dados[str(int(post_id))] = {
        "decision": str(decision or ""),
        "score_gap": score_gap,
        "query": str(query or "")[:120],
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        temporario = caminho.with_name(caminho.name + f".{os.getpid()}.tmp")
        temporario.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
        os.replace(temporario, caminho)
    except Exception:  # noqa: BLE001 - ledger é instrumentação
        pass


def read_media_decision(root: str | Path, post_id: int) -> dict[str, Any]:
    """Decisão de mídia registrada para o post (vazio quando não há)."""
    if not post_id:
        return {}
    try:
        dados = json.loads(decisions_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(dados, dict):
        return {}
    valor = dados.get(str(int(post_id)))
    return valor if isinstance(valor, dict) else {}


def append_telemetry(root: str | Path, event: str, **fields: Any) -> None:
    """Registra um evento do pipeline no telemetry.jsonl central (fail-soft).

    Todo evento carrega a ORIGEM da execução (``run_source``/``session_id``/
    ``cron_job_id``): sem ela não é possível separar cron de execução manual e o
    KPI de contexto mistura os dois universos.
    """
    contexto = run_context()
    for chave, valor in contexto.items():
        fields.setdefault(chave, valor)
    append_event(
        telemetry_path(root),
        event,
        ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **fields,
    )


def read_telemetry_summary(
    root: str | Path,
    *,
    hours: int | None = None,
    run_source: str | None = None,
    cron_job_id: str | None = None,
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
    # Balanço por ORIGEM (cron x manual x teste) e qualidade POR DECISÃO.
    por_origem: dict[str, dict[str, int]] = {}
    decisao_qualidade: dict[str, dict[str, Any]] = {}
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
            # Origem da execução: o balanço por origem é sempre calculado (para
            # o operador VER quanto do total veio de execução manual), enquanto o
            # filtro (KPI oficial) restringe a fatia do cron editorial.
            origem = str(record.get("run_source") or "unknown")
            bucket_origem = por_origem.setdefault(
                origem, {"events": 0, "cmd_bytes": 0, "ready": 0}
            )
            bucket_origem["events"] += 1
            if run_source is not None and record.get("run_source") != run_source:
                continue
            if cron_job_id is not None and str(record.get("cron_job_id") or "") != cron_job_id:
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
                    bucket_origem["cmd_bytes"] += size
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
            # QUALIDADE por decisão: cruza a decisão de busca (auto/choose/reuse)
            # com o que os gates fizeram depois. É a prova que falta de que a
            # economia de julgamento NÃO piorou a imagem escolhida.
            decisao = record.get("decision")
            if isinstance(decisao, str) and decisao:
                bucket_decisao = decisao_qualidade.setdefault(
                    decisao,
                    {
                        "searches": 0,
                        "validate_events": 0,
                        "validate_rejected_items": 0,
                        "validate_clean_events": 0,
                        "apply_ready": 0,
                        "apply_first_pass_ready": 0,
                        "apply_media_blocks": 0,
                        "apply_other_blocks": 0,
                    },
                )
                if event == "media_search_result":
                    bucket_decisao["searches"] += 1
                elif event == "media_validate_result":
                    bucket_decisao["validate_events"] += 1
                    itens = record.get("rejected_items")
                    if isinstance(itens, int):
                        bucket_decisao["validate_rejected_items"] += itens
                        if itens == 0:
                            bucket_decisao["validate_clean_events"] += 1
                elif event == "apply_ready":
                    bucket_decisao["apply_ready"] += 1
                    if record.get("first_pass"):
                        bucket_decisao["apply_first_pass_ready"] += 1
                elif event == "apply_blocked":
                    motivos = record.get("failure_reasons")
                    nomes = motivos if isinstance(motivos, list) else []
                    if any(str(nome) in _MEDIA_GATES for nome in nomes):
                        bucket_decisao["apply_media_blocks"] += 1
                    else:
                        bucket_decisao["apply_other_blocks"] += 1
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
        "run_sources": por_origem,
        "decision_quality": {
            decisao: {
                **numeros,
                # "ficou tão bom quanto antes?" por decisão: itens rejeitados por
                # preflight (absoluto e por evento), taxa de BLOQUEIO DE MÍDIA no
                # apply e primeira passada. Se `auto` piorar isso, a hipótese da
                # margem (EDITOR_AUTO_SCORE_MARGIN) cai.
                "validate_rejected_items_per_event": (
                    round(numeros["validate_rejected_items"] / numeros["validate_events"], 4)
                    if numeros["validate_events"] else None
                ),
                "validate_posts_with_rejection_rate": (
                    round(
                        (numeros["validate_events"] - numeros["validate_clean_events"])
                        / numeros["validate_events"],
                        4,
                    )
                    if numeros["validate_events"] else None
                ),
                "first_pass_ready_rate": (
                    round(numeros["apply_first_pass_ready"] / numeros["apply_ready"], 4)
                    if numeros["apply_ready"] else None
                ),
                "media_block_rate": (
                    round(
                        numeros["apply_media_blocks"]
                        / max(1, numeros["apply_media_blocks"] + numeros["apply_ready"]),
                        4,
                    )
                    if (numeros["apply_media_blocks"] + numeros["apply_ready"]) else None
                ),
            }
            for decisao, numeros in sorted(decisao_qualidade.items())
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
