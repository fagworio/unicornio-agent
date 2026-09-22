#!/usr/bin/env python3
"""Freios do cron editorial: custo em USD E orcamento de CONTEXTO.

Dois motivos para existir:

1. **Custo (USD)** — quando o banco Hermes persiste o ID do cron, a atribuicao
   exige o ID exato do job; usar ``cwd`` nesse caso pode ocultar custo quando o
   diretorio armazenado pelo Hermes diverge do diretorio do deploy. Em versoes
   legadas sem essa coluna, usa ``cwd``/``git_repo_root`` do projeto. Sem uma
   atribuicao segura retorna ``allow``.

2. **Contexto** — um limite financeiro NAO percebe regressao de contexto: com
   ``deepseek-flash`` 75 milhoes de tokens podem custar ~US$ 1. Por isso o
   guard tambem mede ``requests``, ``input_tokens`` e os bytes de stdout que o
   pipeline devolveu ao modelo (``tool_context_bytes``, somados de
   ``work/telemetry.jsonl``). Estourado qualquer limite, o monitor mantem uma
   saida estavel e NAO acorda o LLM: a proxima janela comeca limpa. O checklist
   NUNCA e simplificado para caber no orcamento.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

_JOB_COLUMNS = ("cron_job_id", "job_id")
_PROJECT_COLUMNS = ("cwd", "git_repo_root")


def _project_filter(columns: set[str], project_root: str) -> tuple[str, tuple[str, ...]] | None:
    """Return a safe per-project SQL predicate supported by this Hermes schema."""
    if not project_root:
        return None
    fields = [field for field in _PROJECT_COLUMNS if field in columns]
    if not fields:
        return None
    predicate = " OR ".join(f"{field} = ?" for field in fields)
    return f"({predicate})", tuple(project_root for _ in fields)


def _attribution(
    columns: set[str], job_id: str, project_root: str
) -> tuple[str, tuple[str, ...], str] | None:
    """Predicado de atribuicao (job exato ou projeto) + escopo legivel.

    Ordem: (1) coluna de job do schema moderno; (2) prefixo do id da sessao
    (``cron_<job_id>_...``) — no schema atual as sessoes de cron tem cwd NULL e
    nao ha coluna de job, e sem isto a medicao ficava indisponivel (um teto que
    nunca mede nunca bloqueia); (3) diretorio do projeto (legado).
    """
    job_column = next((name for name in _JOB_COLUMNS if name in columns), None)
    if job_column:
        # Um banco moderno permite a medicao exata. Nao faca fallback por
        # diretorio quando o ID nao foi configurado: uma divergencia de cwd
        # transformaria gasto desconhecido em um enganoso "$0.000".
        if not job_id:
            return None
        return f"{job_column} = ?", (job_id,), f"job editorial {job_id}"
    if job_id and "id" in columns:
        return (
            "id LIKE ?",
            (f"cron_{job_id}_%",),
            f"job editorial {job_id} (prefixo do id da sessao)",
        )
    project_filter = _project_filter(columns, project_root)
    if project_filter is None:
        return None
    predicate, params = project_filter
    return predicate, params, "projeto editorial (cwd/git_repo_root)"


def _sessions_columns(db: sqlite3.Connection) -> set[str]:
    return {row[1] for row in db.execute("PRAGMA table_info(sessions)")}


def cost_measurement_in_last_24h(
    state_db: Path, job_id: str = "", project_root: str = ""
) -> tuple[float, int, str] | None:
    """Return ``(cost, runs, scope)`` with exact-job or project attribution."""
    usage = usage_measurement_in_last_24h(state_db, job_id, project_root)
    if usage is None:
        return None
    return usage["cost_usd"], usage["runs"], usage["scope"]


def usage_measurement_in_last_24h(
    state_db: Path,
    job_id: str = "",
    project_root: str = "",
    *,
    hours: int = 24,
) -> dict | None:
    """Custo E volume de CONTEXTO das sessoes da janela (fail-soft).

    ``prompt_tokens`` e a medida honesta do contexto: o Hermes cobra/relê
    ``input_tokens + cache_read_tokens + cache_write_tokens`` a cada request, e o
    que domina o gasto e justamente o cache-read (a conversa inteira relida).
    Um limite que olhasse so ``input_tokens`` ficaria cego para a situacao
    "pouco input novo + dezenas de milhoes relidos do cache" — exatamente o
    problema que este guard existe para pegar. Os buckets seguem expostos
    separadamente para diagnostico.
    """
    if not state_db.is_file():
        return None
    try:
        db = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
        columns = _sessions_columns(db)
        atribuicao = _attribution(columns, job_id, project_root)
        if atribuicao is None:
            db.close()
            return None
        predicate, params, scope = atribuicao
        # Colunas de VOLUME podem nao existir em bancos legados: a medicao de
        # custo nunca pode passar a devolver "desconhecido" por isso.
        def _soma(coluna: str) -> str:
            return (
                f"COALESCE(SUM({coluna}),0)" if coluna in columns else "0"
            )

        row = db.execute(
            "SELECT COALESCE(SUM(estimated_cost_usd),0), COUNT(*), "
            f"{_soma('api_call_count')}, {_soma('input_tokens')}, "
            f"{_soma('output_tokens')}, {_soma('cache_read_tokens')}, "
            f"{_soma('cache_write_tokens')} "
            "FROM sessions WHERE source='cron' "
            f"AND started_at > strftime('%s','now') - ? AND {predicate}",
            (int(hours) * 3600, *params),
        ).fetchone()
        db.close()
    except sqlite3.Error:
        return None
    entrada = int(row[3] or 0)
    cache_read = int(row[5] or 0)
    cache_write = int(row[6] or 0)
    return {
        "cost_usd": float(row[0] or 0),
        "runs": int(row[1] or 0),
        "requests": int(row[2] or 0),
        "input_tokens": entrada,
        "output_tokens": int(row[4] or 0),
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "prompt_tokens": entrada + cache_read + cache_write,
        "scope": scope,
    }


def cost_in_last_24h(
    state_db: Path, job_id: str = "", project_root: str = ""
) -> tuple[float, int] | None:
    """Backward-compatible cost/runs accessor used by scripts and tests."""
    measured = cost_measurement_in_last_24h(state_db, job_id, project_root)
    return None if measured is None else measured[:2]


def context_bytes_in_last_24h(
    telemetry_path: Path, *, hours: int = 24
) -> tuple[int, int] | None:
    """Bytes de stdout que o pipeline devolveu ao modelo na janela.

    Le ``work/telemetry.jsonl`` e soma ``cmd_output.bytes`` dos comandos
    recentes; devolve ``(bytes, comandos)`` ou ``None`` quando o arquivo nao
    existe (sem medicao = sem bloqueio).
    """
    if not telemetry_path.is_file():
        return None
    limite = datetime.now(timezone.utc) - timedelta(hours=int(hours))
    total = 0
    comandos = 0
    try:
        for line in telemetry_path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("event") != "cmd_output":
                continue
            ts = record.get("ts")
            if isinstance(ts, str) and ts:
                try:
                    quando = datetime.fromisoformat(ts)
                except ValueError:
                    continue
                if quando.tzinfo is None:
                    quando = quando.replace(tzinfo=timezone.utc)
                if quando < limite:
                    continue
            size = record.get("bytes")
            if isinstance(size, int):
                total += size
                comandos += 1
    except OSError:
        return None
    return total, comandos


def _decision(limites: dict[str, float], medidos: dict[str, float]) -> tuple[str, str]:
    """Block no primeiro limite configurado (``> 0``) que foi atingido."""
    for nome, limite in limites.items():
        if limite and limite > 0 and medidos.get(nome, 0) >= limite:
            return "block", nome
    return "allow", "within_budget"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-db", type=Path, required=True)
    parser.add_argument("--job-id", default="")
    parser.add_argument("--project-root", default="")
    parser.add_argument("--limit", type=float, default=0.0, help="limite de custo em USD")
    parser.add_argument("--limit-requests", type=float, default=0.0,
                        help="limite de requests (api_call_count) na janela")
    parser.add_argument("--limit-prompt-tokens", type=float, default=0.0,
                        help="limite de PROMPT tokens (input + cache_read + cache_write): "
                        "e a medida real do contexto relido a cada request")
    parser.add_argument("--limit-input-tokens", type=float, default=0.0,
                        help="DEPRECATED/alias: hoje conta prompt tokens (input+cache), "
                        "nao apenas input novo")
    parser.add_argument("--limit-context-bytes", type=float, default=0.0,
                        help="limite dos bytes de contexto devolvidos ao LLM")
    parser.add_argument("--telemetry", type=Path, default=None,
                        help="work/telemetry.jsonl (mede tool_context_bytes)")
    parser.add_argument("--hours", type=int, default=24)
    args = parser.parse_args()

    limites = {
        "cost_usd": args.limit,
        "requests": args.limit_requests,
        # O limite de tokens e de PROMPT (input + cache): olhar so `input_tokens`
        # deixava o guard cego para "pouco input novo + milhoes relidos do cache".
        "prompt_tokens": args.limit_prompt_tokens or args.limit_input_tokens,
        "context_bytes": args.limit_context_bytes,
    }
    if not any(valor and valor > 0 for valor in limites.values()):
        print(json.dumps({"decision": "allow", "reason": "limit_disabled"}))
        return 0
    usage = usage_measurement_in_last_24h(
        args.state_db, args.job_id.strip(), args.project_root.strip(), hours=args.hours
    )
    if usage is None:
        print(json.dumps({"decision": "allow", "reason": "attribution_unavailable"}))
        return 0
    medidos = {
        "cost_usd": usage["cost_usd"],
        "requests": usage["requests"],
        "prompt_tokens": usage["prompt_tokens"],
    }
    escopo = usage["scope"]
    if args.limit_context_bytes > 0:
        if args.telemetry is None:
            print(json.dumps({"decision": "allow", "reason": "context_unmeasurable"}))
            return 0
        medicao = context_bytes_in_last_24h(args.telemetry, hours=args.hours)
        if medicao is None:
            print(json.dumps({"decision": "allow", "reason": "context_unmeasurable"}))
            return 0
        medidos["context_bytes"] = medicao[0]
        escopo = f"{escopo} + {args.telemetry}"
    decision, motivo = _decision(limites, medidos)
    print(
        json.dumps(
            {
                "decision": decision,
                "reason": motivo,
                "scope": escopo,
                "runs": usage["runs"],
                "measured": {chave: round(valor, 4) for chave, valor in medidos.items()},
                # Buckets separados: o total de prompt tokens e a soma, mas o
                # diagnostico precisa saber DE ONDE ele veio (input novo x cache).
                "tokens": {
                    "input": usage["input_tokens"],
                    "cache_read": usage["cache_read_tokens"],
                    "cache_write": usage["cache_write_tokens"],
                    "prompt": usage["prompt_tokens"],
                    "output": usage["output_tokens"],
                },
                "limits": {chave: valor for chave, valor in limites.items() if valor},
            },
            ensure_ascii=False,
        )
    )
    return 10 if decision == "block" else 0


if __name__ == "__main__":
    raise SystemExit(main())
