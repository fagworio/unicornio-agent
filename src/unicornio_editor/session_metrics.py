"""Metricas POR POST e POR SESSAO (P0 da auditoria de contexto).

A telemetria por comando (``work/telemetry.jsonl``) responde "quanto contexto
cada etapa colocou na conversa"; ela sozinha NAO responde a pergunta que
importa: **quantos tokens custou cada READY**. Para isso e preciso cruzar:

* o que o pipeline produziu (``apply_ready``, ``apply_blocked``, bytes de
  contexto por comando e por post) — o ledger do proprio repo; e
* o que a SESSAO do Hermes gastou (requests, tokens, custo) — ``state.db``, lido
  em modo somente-leitura.

Nomes precisos importam aqui. O Hermes cobra/relê, a cada request,
``input + cache_read + cache_write`` (tudo isso e contexto de PROMPT). Chamar
isso de "tokens" esconderia que o volume e de contexto relido, nao de tokens
novos; e omitir ``cache_write`` subestimaria o total. Por isso o KPI sai como:

* ``prompt_tokens_per_ready`` — input + cache_read + cache_write (contexto lido);
* ``output_tokens_per_ready`` — tokens gerados;
* ``total_model_tokens_per_ready`` — prompt + output + reasoning.

E o KPI oficial usa APENAS a fatia do cron editorial (``run_source=cron`` +
id do job): execucao manual/verificacao entra no balanco por origem, separada,
para nao contaminar o before/after.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .observability import read_telemetry_summary

_JOB_COLUMNS = ("cron_job_id", "job_id")
_PROJECT_COLUMNS = ("cwd", "git_repo_root")


def _hermes_totals(
    state_db: Path,
    *,
    hours: int,
    job_id: str = "",
    project_root: str = "",
) -> dict[str, Any] | None:
    """Soma de requests/tokens/custo das sessoes da janela (fail-soft).

    Atribuicao EXATA por id de job quando o banco expoe a coluna; senao, pelo
    prefixo do id da sessao (``cron_<job_id>_...``); senao, por diretorio do
    projeto. Sem atribuicao segura devolve ``None`` — reportar ``$0.000``/
    ``0 requests`` de uma medicao ambigua seria pior que nao medir.
    """
    if not state_db.is_file():
        return None
    try:
        db = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
        columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
        where = ["started_at > strftime('%s','now') - ?"]
        params: list[Any] = [int(hours) * 3600]
        scope = "projeto editorial (cwd/git_repo_root)"
        job_column = next((name for name in _JOB_COLUMNS if name in columns), None)
        if job_column and job_id:
            where.append(f"{job_column} = ?")
            params.append(job_id)
            scope = f"job editorial {job_id}"
        elif job_id and "id" in columns:
            # O Hermes nomeia a sessao do cron como ``cron_<job_id>_<data>_<hora>``.
            # No schema ATUAL (sem coluna de job e com cwd NULL nas sessoes de
            # cron) e esse prefixo que identifica o job EXATAMENTE — sem ele a
            # medicao do cron ficava indisponivel e o teto diario nunca bloqueava.
            where.append("id LIKE ?")
            params.append(f"cron_{job_id}_%")
            scope = f"job editorial {job_id} (prefixo do id da sessao)"
        else:
            fields = [name for name in _PROJECT_COLUMNS if name in columns]
            if not fields or not project_root:
                db.close()
                return None
            predicate = " OR ".join(f"{name} = ?" for name in fields)
            where.append(f"({predicate})")
            params.extend(project_root for _ in fields)
        # Colunas de volume podem faltar em bancos legados: medir o que existe
        # (nunca transformar "coluna ausente" em medicao indisponivel).
        def _soma(coluna: str) -> str:
            return f"COALESCE(SUM({coluna}),0)" if coluna in columns else "0"

        row = db.execute(
            "SELECT COUNT(*), "
            f"{_soma('api_call_count')}, "
            f"{_soma('input_tokens')}, "
            f"{_soma('output_tokens')}, "
            f"{_soma('cache_read_tokens')}, "
            f"{_soma('cache_write_tokens')}, "
            f"{_soma('reasoning_tokens')}, "
            f"{_soma('estimated_cost_usd')}, "
            f"{_soma('tool_call_count')} "
            "FROM sessions WHERE " + " AND ".join(where),
            params,
        ).fetchone()
        db.close()
    except sqlite3.Error:
        return None
    entrada = int(row[2] or 0)
    cache_read = int(row[4] or 0)
    cache_write = int(row[5] or 0)
    saida = int(row[3] or 0)
    reasoning = int(row[6] or 0)
    return {
        "sessions": int(row[0] or 0),
        "requests": int(row[1] or 0),
        "input_tokens": entrada,
        "output_tokens": saida,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "reasoning_tokens": reasoning,
        "prompt_tokens": entrada + cache_read + cache_write,
        "total_model_tokens": entrada + cache_read + cache_write + saida + reasoning,
        "cost_usd": round(float(row[7] or 0), 6),
        "tool_calls": int(row[8] or 0),
        "scope": scope,
    }


def _ratio(numerador: float | None, denominador: int | None) -> float | None:
    """Razao; ``None`` quando falta o numerador ou o denominador.

    Sem atribuicao no state.db o numerador e desconhecido — devolver ``0.0``
    apresentaria "custo zero" onde a medicao simplesmente nao existe.
    """
    if numerador is None or not denominador:
        return None
    return round(float(numerador) / denominador, 2)


def _per_ready(
    hermes: dict[str, Any] | None, *,
    ready: int,
    tocados: int,
    bytes_contexto: int,
    media_economy: dict[str, Any],
) -> dict[str, Any]:
    """KPIs na unidade POR READY (o volume/tipo de posts da janela varia)."""
    prompt = hermes["prompt_tokens"] if hermes else None
    saida = hermes["output_tokens"] if hermes else None
    total = hermes["total_model_tokens"] if hermes else None
    requests = hermes["requests"] if hermes else None
    gasto = hermes["cost_usd"] if hermes else None
    return {
        # tool_context_bytes_per_ready e a metrica PRINCIPAL de contexto: mede o
        # que o pipeline DEVOLVEU ao modelo (nao o tamanho dos arquivos de
        # auditoria, que ficam em disco).
        "tool_context_bytes_per_ready": _ratio(bytes_contexto, ready),
        "tool_context_bytes_per_post_touched": _ratio(bytes_contexto, tocados),
        "prompt_tokens_per_ready": _ratio(prompt, ready),
        "prompt_tokens_per_post_touched": _ratio(prompt, tocados),
        "output_tokens_per_ready": _ratio(saida, ready),
        "total_model_tokens_per_ready": _ratio(total, ready),
        "requests_per_ready": _ratio(requests, ready),
        "requests_per_post_touched": _ratio(requests, tocados),
        "cost_per_ready_usd": (
            round(gasto / ready, 6) if gasto is not None and ready else None
        ),
        # Midia, mesma unidade: quantas buscas web, chamadas reais de visao e
        # candidatos examinados cada post pronto custou; e quanto veio do acervo.
        "local_reuse_rate": media_economy.get("local_reuse_rate"),
        "web_searches_per_ready": _ratio(media_economy.get("searches_with_web"), ready),
        "vision_calls_per_ready": _ratio(media_economy.get("vision_calls"), ready),
        "candidates_examined_per_ready": _ratio(media_economy.get("examined_total"), ready),
    }


def session_metrics(
    root: str | Path,
    *,
    state_db: str | Path | None = None,
    job_id: str = "",
    project_root: str = "",
    hours: int = 24,
) -> dict[str, Any]:
    """Metricas centrais de custo por post/por sessao (fatia oficial = cron)."""
    # KPI oficial: SOMENTE a fatia do cron editorial. A fatia manual/teste fica
    # no balanco por origem — sem essa separacao, o trabalho de quem investiga o
    # problema entrava no baseline e o before/after ficava invalido.
    resumo = read_telemetry_summary(
        root, hours=int(hours), run_source="cron",
        cron_job_id=job_id if job_id else None,
    )
    completo = read_telemetry_summary(root, hours=int(hours))
    producao = resumo.get("production") or {}
    ready = int(producao.get("unique_ready_posts") or 0)
    tocados = int(producao.get("unique_touched_posts") or 0)
    bytes_contexto = int(resumo.get("context_bytes_total") or 0)
    banco = Path(state_db or Path.home() / ".hermes" / "state.db")
    hermes = _hermes_totals(
        banco, hours=max(1, int(hours)), job_id=job_id, project_root=project_root
    )
    media_economy = resumo.get("media_economy") or {}
    origens = completo.get("run_sources") or {}
    nota = (
        "KPI oficial: somente eventos do cron editorial. Execucao manual/"
        "verificacao aparece em run_sources, fora do baseline."
    )
    if not sum((resumo.get("counts") or {}).values()) and origens.get("unknown"):
        # Eventos gravados ANTES da instrumentacao de origem não podem ser
        # atribuídos: em vez de mostrar zero (que parece "custo zero"), diga o
        # que aconteceu e quando a fatia oficial passa a encher.
        nota += (
            " ATENCAO: a fatia oficial esta vazia porque os eventos da janela sao"
            " anteriores a instrumentacao de origem (run_sources.unknown); ela"
            " passa a medir a partir da proxima execucao do cron."
        )
    return {
        "window_hours": int(hours),
        "official_slice": {
            "run_source": "cron",
            "cron_job_id": job_id or "",
            "telemetry_events": int(sum((resumo.get("counts") or {}).values())),
            "note": nota,
        },
        "telemetry": {
            "ready": ready,
            "posts_touched": tocados,
            "context_bytes_total": bytes_contexto,
            "context_bytes_by_command": resumo.get("context_bytes_by_command") or {},
            "context_bytes_by_post": resumo.get("context_bytes_by_post") or {},
            "post_context_detail": resumo.get("post_context_detail") or {},
            "media_economy": media_economy,
            "decision_quality": resumo.get("decision_quality") or {},
        },
        # Todas as origens (cron + manual + teste): mostra a contaminacao em vez
        # de esconde-la.
        "run_sources": completo.get("run_sources") or {},
        "all_sources": {
            "context_bytes_total": int(completo.get("context_bytes_total") or 0),
            "ready": int((completo.get("production") or {}).get("unique_ready_posts") or 0),
        },
        "hermes_sessions": hermes,
        "derived": _per_ready(
            hermes,
            ready=ready,
            tocados=tocados,
            bytes_contexto=bytes_contexto,
            media_economy=media_economy,
        ),
    }


__all__ = ["session_metrics"]
