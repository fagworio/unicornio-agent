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
        key: value for key, value in fields.items() if not _is_sensitive(key, value)
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


_DECISIONS_FILENAME = "media_decisions.jsonl"
_LEGACY_DECISIONS_FILENAME = "media_decisions.json"

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


def _session_info(session_id: str) -> dict[str, str]:
    """``source`` e id RAIZ da sessão no state.db do Hermes (autoritativo), com cache.

    O id da sessão do agente pode ser o da sessão principal ou o de uma sessão
    FILHA (subagente/ferramenta) — por isso o prefixo do id não basta. A coluna
    `source` do banco é a classificação autoritativa, e `parent_session_id`
    permite subir a cadeia até a raiz (que é de onde sai o id do JOB quando o
    processo roda numa sessão filha: sem isso o run_source saía cron mas o
    cron_job_id vinha vazio). Uma consulta por processo (memoizada),
    somente-leitura: se o banco não existir/estiver quebrado devolve vazio.
    """
    if not session_id:
        return {"source": "", "root_id": ""}
    if session_id in _RUN_CONTEXT_MEMO:
        return _RUN_CONTEXT_MEMO[session_id]
    banco = os.environ.get("HERMES_STATE_DB") or str(Path.home() / ".hermes" / "state.db")
    atual = session_id
    fonte = ""
    raiz = ""
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
                raiz = atual
                pai = str(linha[1] or "")
                if fonte == "cron" or not pai or pai == atual:
                    break
                atual = pai
        finally:
            db.close()
    except Exception:  # noqa: BLE001 - classificação é instrumentação
        fonte, raiz = "", ""
    _RUN_CONTEXT_MEMO[session_id] = {"source": fonte, "root_id": raiz}
    return _RUN_CONTEXT_MEMO[session_id]


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
    info = {"source": "", "root_id": ""} if origem else _session_info(sessao)
    fonte_db = info["source"]
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
        # cron_<job_id>_<YYYYMMDD>_<HHMMSS> — o id do job sai da sessão RAIZ
        # (quando o processo roda numa sessão filha, o id do processo não tem o
        # prefixo cron_ e o job ficaria vazio).
        candidatos_id = [sessao, str(info.get("root_id") or "")]
        for candidato in candidatos_id:
            partes = candidato.split("_")
            if len(partes) >= 3 and partes[0] == "cron":
                job = partes[1]
                break
        else:
            if not job:
                job = ""
    else:
        job = ""
    # root_session_id é o que permite CRUZAR telemetria e state.db pelas MESMAS
    # sessões: o processo pode rodar numa sessão filha (ferramenta/subagente) e o
    # numerador (tokens/custo) tem de vir da sessão RAIZ que o banco registra.
    raiz = str(info.get("root_id") or "") or sessao
    return {
        "run_source": origem,
        "session_id": sessao,
        "root_session_id": raiz,
        "cron_job_id": job,
    }


def _ler_decisoes(root: str | Path) -> list[dict[str, Any]]:
    """Todas as decisões de mídia registradas (append-only), em ordem.

    Um post faz VÁRIAS buscas (uma por imagem/item). O formato antigo
    (``media_decisions.json``, mapa post -> última decisão) sobrescrevia a
    anterior: ``imagem 1 -> auto`` seguido de ``imagem 2 -> choose`` ficava
    registrado apenas como ``choose``, e um bloqueio posterior era atribuído à
    decisão errada. O log append-only preserva cada decisão com seu
    ``decision_id``, que é o que permite calibrar a margem do `auto` com dado.
    """
    caminho = decisions_path(root)
    decisoes: list[dict[str, Any]] = []
    if caminho.is_file():
        for linha in caminho.read_text(encoding="utf-8").splitlines():
            linha = linha.strip()
            if not linha:
                continue
            try:
                registro = json.loads(linha)
            except ValueError:
                continue
            if isinstance(registro, dict):
                decisoes.append(registro)
    legado = Path(root) / "work" / _LEGACY_DECISIONS_FILENAME
    if legado.is_file():
        # Compatibilidade com o arquivo antigo (mapa post -> última decisão).
        # Ele é carregado PRIMEIRO e as entradas de um post que JÁ tem registro
        # no JSONL são descartadas: o log novo é autoritativo e, entrando depois,
        # o legado não pode virar a "última decisão" de um post recente.
        try:
            dados = json.loads(legado.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            dados = {}
        if isinstance(dados, dict):
            com_jsonl = {str(d.get("post_id")) for d in decisoes}
            antigas = [
                {**valor, "post_id": post_id, "legacy": True}
                for post_id, valor in dados.items()
                if isinstance(valor, dict) and str(post_id) not in com_jsonl
            ]
            decisoes = antigas + decisoes
    return decisoes


def read_media_decisions(root: str | Path, post_id: int | None = None) -> list[dict[str, Any]]:
    """Decisões de mídia registradas (todas, ou só as de um post)."""
    decisoes = _ler_decisoes(root)
    if post_id is None:
        return decisoes
    return [d for d in decisoes if str(d.get("post_id") or "") == str(int(post_id))]


def record_media_decision(
    root: str | Path,
    post_id: int,
    *,
    decision: str,
    score_gap: int | None = None,
    query: str = "",
    subject: str = "",
    item_index: int | None = None,
    selected_url: str = "",
    coverage: str = "",
    decision_id: str = "",
) -> str:
    """Registra UMA decisão de mídia (append-only) e devolve o ``decision_id``.

    O id entra no ``media_plan`` e nos eventos dos gates seguintes, então o
    ``media-validate`` consegue dizer exatamente qual candidato/ITEM foi rejeitado
    e qual decisão o escolheu (``auto`` com gap X, ``choose``, ``reuse``). Listas
    (listicle) registram uma decisão POR ITEM — antes gravavam ``post_id=0`` e
    ficavam fora de qualquer cruzamento de qualidade.
    """
    if not post_id and item_index is None:
        return ""
    identificador = decision_id or uuid.uuid4().hex[:12]
    registro = {
        "decision_id": identificador,
        "post_id": int(post_id or 0),
        "query": str(query or "")[:120],
        "subject": str(subject or "")[:120],
        "decision": str(decision or ""),
        "coverage": str(coverage or ""),
        "score_gap": score_gap,
        "item_index": item_index,
        "selected_url": str(selected_url or "")[:300],
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    caminho = decisions_path(root)
    try:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        with caminho.open("a", encoding="utf-8") as arquivo:
            arquivo.write(json.dumps(registro, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - ledger é instrumentação
        pass
    return identificador


def read_media_decision(root: str | Path, post_id: int) -> dict[str, Any]:
    """Última decisão de mídia do post (vazio quando não há).

    Continua devolvendo UM registro (o último) porque é o que os gates seguintes
    precisam para se autoclassificar; o histórico completo fica em
    :func:`read_media_decisions`.
    """
    if not post_id:
        return {}
    registros = read_media_decisions(root, post_id)
    return registros[-1] if registros else {}


def read_media_decision_by_id(
    root: str | Path, post_id: int, decision_id: str
) -> dict[str, Any]:
    """Decisão do ledger pelo seu ``decision_id`` (o ÚNICO dado autoritativo).

    O `media_plan` traz `decision_id` e, opcionalmente, um texto `decision` — mas
    esse texto é do AGENTE e pode estar errado (cópia/edição). Para MEDIR, o
    `decision`, o `score_gap`, o `coverage` e o `selected_url` têm de vir daqui:
    caso contrário o evento do item AAA podia sair rotulado com o `score_gap` da
    ÚLTIMA decisão do post (BBB), envenenando `decision_quality` e a calibração
    da margem.

    Procura nas entradas DESTE post e nas não atribuídas (``post_id = 0``, caso de
    listicle sem `--post-id`) — nunca em outro post, para não "resolver" um id
    trocado. Devolve ``{}`` quando o id não existe.
    """
    if not decision_id:
        return {}
    identificador = str(decision_id)
    alvos = {str(int(post_id or 0)), "0"}
    for registro in reversed(_ler_decisoes(root)):
        if str(registro.get("decision_id") or "") != identificador:
            continue
        if str(registro.get("post_id") or "0") in alvos:
            return registro
    return {}


def attribution_of(root: str | Path, post_id: int, decision_id: str) -> str:
    """Estado da ATRIBUIÇÃO de um item: resolved | missing | invalid.

    * ``resolved`` — o `decision_id` existe no ledger do post;
    * ``missing`` — o item não trouxe `decision_id`;
    * ``invalid`` — trouxe um id que NÃO está no ledger (erro de cópia/invenção).

    É o que permite medir a COBERTURA da atribuição e não olhar `auto` x `choose`
    enviesado por itens que nunca foram atribuídos.
    """
    if not decision_id:
        return "missing"
    return "resolved" if read_media_decision_by_id(root, post_id, decision_id) else "invalid"


def usage_cost_usd(
    input_tokens: int,
    output_tokens: int,
    *,
    price_in_per_1m: float,
    price_out_per_1m: float,
) -> float | None:
    """Custo em USD de UMA chamada direta, do preco por 1M tokens.

    Devolve ``None`` quando nenhum preco esta configurado: o evento direto NAO
    inventa custo — quem consome (``cost_guard``) marca o total como parcial.
    Gravado no proprio evento (``model_cost_usd``) para que a medicao use o preco
    vigente no momento da chamada, e nao o de hoje.
    """
    if price_in_per_1m <= 0 and price_out_per_1m <= 0:
        return None
    entrada = int(input_tokens or 0)
    saida = int(output_tokens or 0)
    return round(
        (entrada * float(price_in_per_1m) + saida * float(price_out_per_1m)) / 1_000_000,
        8,
    )


def append_telemetry(root: str | Path, event: str, **fields: Any) -> None:
    """Registra um evento do pipeline no telemetry.jsonl central (fail-soft).

    Todo evento carrega a ORIGEM da execução (``run_source``/``session_id``/
    ``cron_job_id``): sem ela não é possível separar cron de execução manual e o
    KPI de contexto mistura os dois universos.
    """
    contexto = run_context()
    for chave, valor in contexto.items():
        fields.setdefault(chave, valor)
    # Batch metadata is deliberately opt-in through the process environment:
    # single-post runs keep their historical shape, while batch orchestration
    # can correlate every internal event without changing domain APIs.
    batch_id = str(os.environ.get("UNICORNIO_BATCH_ID") or "").strip()
    batch_stage = str(os.environ.get("UNICORNIO_BATCH_STAGE") or "").strip()
    if batch_id:
        fields.setdefault("batch_id", batch_id)
    if batch_stage:
        fields.setdefault("batch_stage", batch_stage)
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
    batch_id: str | None = None,
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
    first_pass_blocked = 0
    # Cobertura da ATRIBUIÇÃO de decisão: resolved | missing | invalid (por ITEM).
    atribuicao: dict[str, int] = {"resolved": 0, "missing": 0, "invalid": 0}
    # Planos com mais de uma decisão: característica do plano, não falha.
    planos_mistos = 0
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
        "vision_api_requests": 0,
        "vision_input_tokens": 0,
        "vision_cached_tokens": 0,
        "vision_output_tokens": 0,
        "vision_errors": 0,
        "vision_requests_without_usage": 0,
        "vision_low_requests": 0,
        "vision_high_requests": 0,
    }
            # Balanço por ORIGEM (cron x manual x teste) e qualidade POR DECISÃO.
    por_origem: dict[str, dict[str, int]] = {}
    batches: dict[str, dict[str, Any]] = {}
    economics = {
        "hermes_model_requests": 0,
        "editorial_model_requests": 0,
        "editorial_posts_generated": 0,
        "vision_provider_requests": 0,
        "vision_images_examined": 0,
        "tool_calls": 0,
        "external_http_requests": 0,
        "model_cost_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
    }
    decisao_qualidade: dict[str, dict[str, Any]] = {}
    # Sessões RAIZ que produziram os eventos da fatia filtrada: é por elas que o
    # numerador (tokens/custo do state.db) é cruzado com o denominador (READY) —
    # sem isso o KPI mistura "READY novos" com "tokens de 24h".
    sessoes_filtradas: set[str] = set()
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
            record_batch_id = str(record.get("batch_id") or "").strip()
            if batch_id is not None and batch_id != record_batch_id:
                continue
            if record_batch_id:
                batch = batches.setdefault(
                    record_batch_id,
                    {"events": 0, "stages": {}, "posts": set(), "max_size": 0},
                )
                batch["events"] += 1
                stage = str(record.get("batch_stage") or record.get("command") or "unknown")
                stages = batch["stages"]
                stages[stage] = int(stages.get(stage, 0)) + 1
                batch_size = record.get("batch_size")
                if isinstance(batch_size, int):
                    batch["max_size"] = max(int(batch["max_size"]), batch_size)
                batch_post = record.get("post_id")
                if isinstance(batch_post, int):
                    batch["posts"].add(batch_post)
            raiz_evento = str(record.get("root_session_id") or record.get("session_id") or "")
            if raiz_evento:
                sessoes_filtradas.add(raiz_evento)
            counts[event] = counts.get(event, 0) + 1
            if event in {"cmd_output", "tool_call"}:
                economics["tool_calls"] += 1
            if event in {"hermes_model_request", "model_request", "editorial_model_request"}:
                requests = int(record.get("requests") or record.get("count") or 1)
                is_editorial = event == "editorial_model_request" or str(record.get("stage") or "") == "editorial"
                if not is_editorial:
                    economics["hermes_model_requests"] += max(1, requests)
                if is_editorial:
                    economics["editorial_model_requests"] += max(1, requests)
                    generated = record.get("posts_generated", record.get("batch_size", record.get("posts", 0)))
                    if isinstance(generated, int):
                        economics["editorial_posts_generated"] += max(0, generated)
            if event == "vision_api_request":
                economics["vision_provider_requests"] += 1
                batch_size = record.get("batch_size")
                if isinstance(batch_size, int):
                    economics["vision_images_examined"] += max(0, batch_size)
            for field, target in (
                ("external_http_requests", "external_http_requests"),
                ("input_tokens", "input_tokens"),
                ("output_tokens", "output_tokens"),
            ):
                value = record.get(field)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    economics[target] += int(value)
            cost = record.get("model_cost_usd", record.get("cost_usd"))
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                economics["model_cost_usd"] += float(cost)
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
            if event == "apply_blocked" and record.get("first_pass"):
                # Bloqueio na PRIMEIRA tentativa: entra no denominador da taxa de
                # sucesso de primeira tentativa (junto com os READY de primeira).
                first_pass_blocked += 1
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
            if event == "vision_api_request":
                # UMA requisição HTTP = UM evento (low e high contam): é a
                # unidade que reconcilia com o custo de visão do provedor.
                midia["vision_api_requests"] += 1
                midia["vision_calls"] += 1
                for campo, chave in (
                    ("input_tokens", "vision_input_tokens"),
                    ("cached_tokens", "vision_cached_tokens"),
                    ("output_tokens", "vision_output_tokens"),
                ):
                    valor = record.get(campo)
                    if isinstance(valor, int):
                        midia[chave] += valor
                if str(record.get("error") or "").strip():
                    midia["vision_errors"] += 1
                if not isinstance(record.get("input_tokens"), int):
                    # Sem `usage` (ex.: HTTP 500): a requisição conta, mas os
                    # tokens ficam LOWER BOUND — o total passa a ser PARCIAL.
                    midia["vision_requests_without_usage"] += 1
                if str(record.get("detail") or "") == "high":
                    midia["vision_high_requests"] += 1
                else:
                    midia["vision_low_requests"] += 1
            elif event == "vision_call":
                # Formato anterior (sem usage): conta como chamada para não
                # quebrar a série histórica, sem inflar os tokens.
                midia["vision_calls"] += 1
            # COBERTURA da atribuição: só eventos POR ITEM entram na taxa. O
            # agregado do post NÃO é item — contá-lo fazia 100% dos itens
            # atribuídos aparecer como 90,9% num listicle de 10 itens.
            if event == "media_validate_result" and isinstance(
                record.get("item_index"), int
            ):
                estado = str(record.get("attribution") or "")
                if estado in ("resolved", "missing", "invalid"):
                    atribuicao[estado] += 1
            # `mixed` é CARACTERÍSTICA do plano (mais de uma decisão), não falha de
            # atribuição: fica em outra dimensão.
            if event == "media_validate_result" and str(
                record.get("decision_scope") or ""
            ) == "mixed":
                planos_mistos += 1
            # QUALIDADE por decisão: cruza a decisão de busca (auto/choose/reuse)
            # com o que os gates fizeram depois. É a prova que falta de que a
            # economia de julgamento NÃO piorou a imagem escolhida.
            decisao = record.get("decision")
            # DEFESA EM DUAS CAMADAS: um evento de apply cuja atribuição é
            # explicitamente missing/invalid NÃO entra em `decision_quality` —
            # sem isso, um `media_plan: []` com histórico "auto" contava como
            # `decision_quality.auto.apply_ready`, contaminando justamente a
            # comparação auto x choose.
            atribuicao_evento = str(record.get("decision_attribution") or "")
            if isinstance(decisao, str) and decisao and atribuicao_evento not in (
                "missing", "invalid"
            ):
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
                        "first_pass_attempts": 0,
                        "first_pass_ready": 0,
                        "first_pass_blocked": 0,
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
                        bucket_decisao["first_pass_ready"] += 1
                        # `first_pass_attempts` conta SÓ primeiras tentativas: um
                        # READY na 2ª tentativa não é primeira tentativa (antes o
                        # denominador inflava e a taxa de sucesso mentia).
                        bucket_decisao["first_pass_attempts"] += 1
                elif event == "apply_blocked":
                    motivos = record.get("failure_reasons")
                    nomes = motivos if isinstance(motivos, list) else []
                    if any(str(nome) in _MEDIA_GATES for nome in nomes):
                        bucket_decisao["apply_media_blocks"] += 1
                    else:
                        bucket_decisao["apply_other_blocks"] += 1
                    if record.get("first_pass"):
                        bucket_decisao["first_pass_blocked"] += 1
                        bucket_decisao["first_pass_attempts"] += 1
            if event == "apply_ready":
                # `run_sources.ready` precisa contar de verdade (antes ficava
                # sempre 0 e o diagnóstico por origem mentia).
                bucket_origem["ready"] += 1
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
    batch_summary = {
        batch_id: {
            **{key: value for key, value in bucket.items() if key != "posts"},
            "posts": sorted(bucket["posts"]),
        }
        for batch_id, bucket in batches.items()
    }
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
            # NOME CORRETO do que a conta mede: DOS READY, quantos foram de
            # primeira. Não é taxa de sucesso de primeira tentativa (um post
            # bloqueado na primeira não aparece no denominador).
            "ready_first_pass_share": (
                round(first_pass_ready / ready_with_first_pass, 4)
                if ready_with_first_pass else None
            ),
            # TAXA DE SUCESSO de primeira tentativa: das PRIMEIRAS tentativas
            # (ready + blocked), quantas viraram READY. É a métrica que faltava.
            "first_pass_attempts": first_pass_ready + first_pass_blocked,
            "first_pass_blocked": first_pass_blocked,
            "first_pass_success_rate": (
                round(first_pass_ready / (first_pass_ready + first_pass_blocked), 4)
                if (first_pass_ready + first_pass_blocked) else None
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
        "batches": batch_summary,
        "economics": {
            **economics,
            "model_cost_usd": round(float(economics["model_cost_usd"]), 8),
            "posts_per_editorial_request": (
                round(economics["editorial_posts_generated"] / economics["editorial_model_requests"], 4)
                if economics["editorial_model_requests"] else None
            ),
            "images_per_vision_request": (
                round(economics["vision_images_examined"] / economics["vision_provider_requests"], 4)
                if economics["vision_provider_requests"] else None
            ),
            "vision_candidates_per_ready": (
                round(economics["vision_images_examined"] / len(ready_posts), 4)
                if ready_posts else None
            ),
        },
        "root_sessions": sorted(sessoes_filtradas),
        # COBERTURA da atribuição (POR ITEM): quantos itens tiveram a decisão
        # resolvida pelo ledger, quantos não trouxeram id e quantos trouxeram id
        # inexistente. `mixed_plan_count` é outra coisa: planos com mais de uma
        # decisão (não rotuláveis como um todo). Sem essa separação a taxa caía
        # artificialmente (10 itens atribuídos + 1 plano misto = 90,9%).
        "decision_attribution": {
            **atribuicao,
            "itens": sum(atribuicao.values()),
            "decision_attribution_rate": (
                round(atribuicao["resolved"] / sum(atribuicao.values()), 4)
                if sum(atribuicao.values()) else None
            ),
            "mixed_plan_count": planos_mistos,
        },
        "decision_quality": {
            decisao: {
                **numeros,
                # "ficou tão bom quanto antes?" por decisão: itens rejeitados por
                # preflight (absoluto e por evento), taxa de BLOQUEIO DE MÍDIA no
                # apply e sucesso de PRIMEIRA TENTATIVA. Se `auto` piorar isso, a
                # hipótese da margem (EDITOR_AUTO_SCORE_MARGIN) cai.
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
                # `ready_first_pass_share`: dos posts que ficaram READY, quantos
                # ficaram na primeira. NÃO é taxa de sucesso (um post bloqueado na
                # primeira não aparece aqui).
                "ready_first_pass_share": (
                    round(numeros["apply_first_pass_ready"] / numeros["apply_ready"], 4)
                    if numeros["apply_ready"] else None
                ),
                # `first_pass_success_rate`: das PRIMEIRAS tentativas, quantas
                # viraram READY (1 READY + 10 bloqueados => 9,1%, e não 100%).
                "first_pass_success_rate": (
                    round(numeros["first_pass_ready"] / numeros["first_pass_attempts"], 4)
                    if numeros["first_pass_attempts"] else None
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


def _is_sensitive(key: str, value: Any = None) -> bool:
    """Campo sensível: nome com termo de credencial E valor TEXTUAL.

    O filtro existe para não gravar segredos em claro. Mas ele descartava
    silenciosamente CONTADORES cujo nome contém "token" (``input_tokens``,
    ``cached_tokens``, ``output_tokens``): a telemetria de visão saía sem os
    tokens e a conta nunca fechava. Número/bool/None não são credencial; texto
    com nome de credencial continua bloqueado.
    """
    lowered = key.lower()
    if not any(part in lowered for part in _SENSITIVE_PARTS):
        return False
    if value is None or isinstance(value, (int, float, bool)):
        return False
    return True
