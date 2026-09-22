"""Metricas POR POST e POR SESSAO (P0 da auditoria de contexto).

A telemetria por comando (``work/telemetry.jsonl``) responde "quanto contexto
cada etapa colocou na conversa"; ela sozinha NAO responde a pergunta que
importa: **quantos tokens custou cada READY**. Para isso e preciso cruzar:

* o que o pipeline produziu (``apply_ready``, ``apply_blocked``, bytes de
  contexto por comando e por post) — o ledger do proprio repo; e
* o que a SESSAO do Hermes gastou (requests, tokens de entrada/saida, custo) —
  ``state.db``, lido em modo somente-leitura.

O resultado sao as metricas centrais de economia, comparaveis entre mudancas de
configuracao: ``tokens_per_ready``, ``tokens_per_post_touched``,
``requests_per_ready`` e ``tool_context_bytes_per_ready``. Sem elas, "75
milhoes de tokens" nao aponta o culpado.
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

    Atribuicao EXATA por id de job quando o banco expoe a coluna; senao, por
    diretorio do projeto. Sem atribuicao segura devolve ``None`` — reportar
    ``$0.000``/``0 requests`` de uma medicao ambigua seria pior que nao medir.
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
            f"{_soma('estimated_cost_usd')}, "
            f"{_soma('tool_call_count')} "
            "FROM sessions WHERE " + " AND ".join(where),
            params,
        ).fetchone()
        db.close()
    except sqlite3.Error:
        return None
    return {
        "sessions": int(row[0] or 0),
        "requests": int(row[1] or 0),
        "input_tokens": int(row[2] or 0),
        "output_tokens": int(row[3] or 0),
        "cache_read_tokens": int(row[4] or 0),
        "cost_usd": round(float(row[5] or 0), 6),
        "tool_calls": int(row[6] or 0),
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


def session_metrics(
    root: str | Path,
    *,
    state_db: str | Path | None = None,
    job_id: str = "",
    project_root: str = "",
    hours: int = 24,
) -> dict[str, Any]:
    """Metricas centrais de custo por post/por sessao."""
    resumo = read_telemetry_summary(root, hours=int(hours))
    producao = resumo.get("production") or {}
    ready = int(producao.get("unique_ready_posts") or 0)
    tocados = int(producao.get("unique_touched_posts") or 0)
    bytes_contexto = int(resumo.get("context_bytes_total") or 0)
    banco = Path(
        state_db
        or Path.home() / ".hermes" / "state.db"
    )
    hermes = _hermes_totals(
        banco, hours=max(1, int(hours)), job_id=job_id, project_root=project_root
    )
    entradas: float | None = None
    gasto: float | None = None
    requests: int | None = None
    if hermes:
        # O custo de contexto esta no que o modelo RELÊ a cada request: entrada
        # nova + cache leitura. Somar os dois e a medida honesta do volume.
        entradas = int(hermes["input_tokens"]) + int(hermes["cache_read_tokens"])
        gasto = float(hermes["cost_usd"])
        requests = int(hermes["requests"])
    return {
        "window_hours": int(hours),
        "telemetry": {
            "ready": ready,
            "posts_touched": tocados,
            "context_bytes_total": bytes_contexto,
            "context_bytes_by_command": resumo.get("context_bytes_by_command") or {},
            "context_bytes_by_post": resumo.get("context_bytes_by_post") or {},
            "post_context_detail": resumo.get("post_context_detail") or {},
        },
        "hermes_sessions": hermes,
        "derived": {
            "tokens_per_ready": _ratio(entradas, ready),
            "tokens_per_post_touched": _ratio(entradas, tocados),
            "requests_per_ready": _ratio(requests, ready),
            "requests_per_post_touched": _ratio(requests, tocados),
            "tool_context_bytes_per_ready": _ratio(bytes_contexto, ready),
            "tool_context_bytes_per_post_touched": _ratio(bytes_contexto, tocados),
            "cost_per_ready_usd": (
                round(gasto / ready, 6) if gasto is not None and ready else None
            ),
        },
    }


__all__ = ["session_metrics"]
