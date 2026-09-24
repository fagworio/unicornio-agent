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

3. **Custo DIRETO** — o pipeline novo fala com o provedor por conta propria em
   duas etapas (``editorial_model_request`` e ``vision_api_request``). Essas
   chamadas NAO entram no accounting do Hermes (nem em ``sessions``, nem em
   ``session_model_usage``), entao um teto que olhasse so o Hermes mediria menos
   do que gastou. O teto de USD incide sobre ``grand_total_cost_usd``
   (main + auxiliar + editorial direto + visao direta) e os limites de VOLUME
   sobre ``grand_total_requests`` / ``grand_total_prompt_tokens``.
   Preco nunca fica escondido no codigo: vem do ambiente (por provider/modelo) em
   ``EDITORIAL_INPUT_COST_PER_1M_USD`` / ``EDITORIAL_OUTPUT_COST_PER_1M_USD`` e
   ``EDITOR_VISION_INPUT_COST_PER_1M_USD`` / ``EDITOR_VISION_OUTPUT_COST_PER_1M_USD``.
   Sem preco configurado o custo direto fica INDETERMINADO (``None``, com
   ``cost_partial``/``direct_unpriced_requests``) — nunca se inventa um numero.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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


# ── Chamadas DIRETAS ao provedor (fora do Hermes) ──────────────────────────
# Cada evento destes e UMA requisicao HTTP feita pelo pipeline. O Hermes nao as
# ve nem cobra por elas: se o guard nao ler a telemetria, o teto de USD mede
# menos do que foi gasto (era o furo do canary: editorial direto + visao direta
# fora do teto).
DIRECT_EDITORIAL_EVENT = "editorial_model_request"
DIRECT_VISION_EVENT = "vision_api_request"

# Nome dos precos no AMBIENTE (USD por 1M tokens: input, output). Preco por
# provider/modelo — trocar de modelo e mudar o .env, nunca o codigo.
PRECO_EDITORIAL_ENV = ("EDITORIAL_INPUT_COST_PER_1M_USD", "EDITORIAL_OUTPUT_COST_PER_1M_USD")
PRECO_VISAO_ENV = ("EDITOR_VISION_INPUT_COST_PER_1M_USD", "EDITOR_VISION_OUTPUT_COST_PER_1M_USD")


def _preco_do_ambiente(nomes: tuple[str, str]) -> tuple[float, float]:
    """``(input, output)`` em USD por 1M tokens; ``(0, 0)`` = nao configurado."""
    valores: list[float] = []
    for nome in nomes:
        bruto = os.environ.get(nome, "")
        try:
            valores.append(max(0.0, float(str(bruto).strip() or 0.0)))
        except (TypeError, ValueError):
            valores.append(0.0)
    return float(valores[0]), float(valores[1])


def _custo_da_requisicao(
    record: dict, *, preco_in: float, preco_out: float
) -> tuple[float | None, bool]:
    """``(custo_usd, precificado)`` de UMA requisicao direta.

    Preferencia: ``model_cost_usd`` gravado pelo produtor na hora da chamada (o
    preco vigente quando o evento foi escrito). Senao, calcula com o preco do
    ambiente. Sem evento nem ambiente com preco => ``(None, False)``: o custo
    fica INDETERMINADO e o total sai marcado como parcial.
    """
    gravado = record.get("model_cost_usd")
    if isinstance(gravado, (int, float)) and not isinstance(gravado, bool):
        return float(gravado), True
    if preco_in <= 0 and preco_out <= 0:
        return None, False
    entrada = record.get("input_tokens")
    saida = record.get("output_tokens")
    entrada = int(entrada) if isinstance(entrada, int) else 0
    saida = int(saida) if isinstance(saida, int) else 0
    return round((entrada * preco_in + saida * preco_out) / 1_000_000, 8), True


def direct_usage(
    telemetry_path: Path | None,
    *,
    event: str,
    hours: int = 24,
    job_id: str = "",
    run_source: str = "cron",
    price_in: float = 0.0,
    price_out: float = 0.0,
) -> dict[str, Any]:
    """Uso e custo de UMA familia de chamada DIRETA (editorial ou visao).

    Filtro obrigatorio por ``run_source``/``cron_job_id``: sem ele uma sessao
    MANUAL pesada bloquearia o cron.

    ATENCAO: no formato OpenAI, ``prompt_tokens`` JA inclui os tokens em cache —
    ``cached_tokens`` e informativo e NAO pode ser somado (duplo computo).
    """
    vazio: dict[str, Any] = {
        "requests": 0, "prompt_tokens": 0, "cached_tokens": 0,
        "output_tokens": 0, "cost_usd": None, "priced_requests": 0,
        "unpriced_requests": 0, "errors": 0, "without_usage": 0,
        "tokens_partial": False, "cost_partial": False, "measurable": False,
    }
    if telemetry_path is None or not Path(telemetry_path).is_file():
        return vazio
    limite = datetime.now(timezone.utc) - timedelta(hours=int(hours))
    dados = dict(vazio)
    dados["measurable"] = True
    custo_total = 0.0
    try:
        for line in Path(telemetry_path).read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("event") != event:
                continue
            if run_source and record.get("run_source") != run_source:
                continue
            if job_id and str(record.get("cron_job_id") or "") != str(job_id):
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
            dados["requests"] = int(dados["requests"]) + 1
            for campo, chave in (("input_tokens", "prompt_tokens"),
                                 ("cached_tokens", "cached_tokens"),
                                 ("output_tokens", "output_tokens")):
                valor = record.get(campo)
                if isinstance(valor, int):
                    dados[chave] = int(dados[chave]) + valor
            custo, precificado = _custo_da_requisicao(
                record, preco_in=price_in, preco_out=price_out
            )
            if precificado:
                dados["priced_requests"] = int(dados["priced_requests"]) + 1
                custo_total += float(custo or 0.0)
            else:
                # Requisicao direta que aconteceu e nao tem preco: o gasto
                # existe, so nao e conhecido. Fica visivel, nunca vira zero.
                dados["unpriced_requests"] = int(dados["unpriced_requests"]) + 1
            if str(record.get("error") or "").strip():
                dados["errors"] = int(dados["errors"]) + 1
            if not isinstance(record.get("input_tokens"), int):
                # Requisicao sem `usage` (ex.: HTTP 500): conta como request,
                # mas os tokens ficam LOWER BOUND — nao se estima consumo.
                dados["without_usage"] = int(dados["without_usage"]) + 1
    except OSError:
        return vazio
    dados["tokens_partial"] = int(dados["without_usage"]) > 0
    dados["cost_partial"] = int(dados["unpriced_requests"]) > 0
    if int(dados["priced_requests"]) > 0:
        dados["cost_usd"] = round(custo_total, 6)
    elif int(dados["requests"]) == 0 and (price_in > 0 or price_out > 0):
        # Nenhuma chamada na janela, mas o preco e conhecido: a camada custou zero.
        dados["cost_usd"] = 0.0
    return dados


def vision_direct_usage(
    telemetry_path: Path | None,
    *,
    hours: int = 24,
    job_id: str = "",
    run_source: str = "cron",
    price_in: float = 0.0,
    price_out: float = 0.0,
) -> dict[str, Any]:
    """Uso das chamadas DIRETAS de visão do Unicornio Agent (fora do Hermes).

    O Vision Gate fala com o provedor por conta própria (``urlopen``): essas
    chamadas NÃO entram no accounting do Hermes (nem em `sessions`, nem em
    `session_model_usage`), então sem esta leitura o "grand total" não é o total.
    """
    return direct_usage(
        telemetry_path, event=DIRECT_VISION_EVENT, hours=hours, job_id=job_id,
        run_source=run_source, price_in=price_in, price_out=price_out,
    )


def editorial_direct_usage(
    telemetry_path: Path | None,
    *,
    hours: int = 24,
    job_id: str = "",
    run_source: str = "cron",
    price_in: float = 0.0,
    price_out: float = 0.0,
) -> dict[str, Any]:
    """Uso das chamadas DIRETAS do editorial (``editorial_model_request``).

    O provider editorial novo (``editorial_provider``) faz UMA requisicao
    ``chat/completions`` por microbatch, sem tool loop: essa chamada tambem nao
    passa pelo Hermes e precisa entrar no teto de USD e nos limites de volume.
    """
    return direct_usage(
        telemetry_path, event=DIRECT_EDITORIAL_EVENT, hours=hours, job_id=job_id,
        run_source=run_source, price_in=price_in, price_out=price_out,
    )


def usage_measurement_in_last_24h(
    state_db: Path,
    job_id: str = "",
    project_root: str = "",
    *,
    hours: int = 24,
    telemetry_path: Path | None = None,
    editorial_price: tuple[float, float] | None = None,
    vision_price: tuple[float, float] | None = None,
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
        # Uso AUXILIAR do Hermes (vision/compressao/titulo/aprovacao): gravado em
        # session_model_usage com task != '' e FORA dos contadores de `sessions`.
        # O filtro por task é OBRIGATÓRIO: o main-loop TAMBÉM aparece em
        # session_model_usage com task='' (agregado espelhado em `sessions`), e
        # somá-lo aqui contava o custo do main-loop DUAS vezes.
        custo_aux = 0.0
        tokens_aux = 0
        chamadas_aux = 0
        try:
            aux_row = db.execute(
                "SELECT COALESCE(SUM(estimated_cost_usd),0), "
                "COALESCE(SUM(input_tokens + cache_read_tokens + cache_write_tokens),0), "
                "COALESCE(SUM(api_call_count),0) "
                "FROM session_model_usage "
                "WHERE COALESCE(task,'') != '' AND session_id IN ("
                "SELECT id FROM sessions WHERE source='cron' "
                f"AND started_at > strftime('%s','now') - ? AND {predicate})",
                (int(hours) * 3600, *params),
            ).fetchone()
            custo_aux = float((aux_row or [0])[0] or 0)
            tokens_aux = int((aux_row or [0, 0, 0])[1] or 0)
            chamadas_aux = int((aux_row or [0, 0, 0])[2] or 0)
        except sqlite3.Error:
            custo_aux, tokens_aux, chamadas_aux = 0.0, 0, 0
        db.close()
    except sqlite3.Error:
        return None
    entrada = int(row[3] or 0)
    cache_read = int(row[5] or 0)
    cache_write = int(row[6] or 0)
    custo_main = float(row[0] or 0)
    prompt_main = entrada + cache_read + cache_write
    # Camadas DIRETAS (fora do Hermes) fecham o "grand total": sem elas o total
    # nao e o total. Preco: por evento quando o produtor gravou
    # ``model_cost_usd``; senao do ambiente (por provider/modelo). Sem preco
    # conhecido o valor fica INDETERMINADO (None) + flag de total parcial.
    preco_editorial = editorial_price or _preco_do_ambiente(PRECO_EDITORIAL_ENV)
    preco_visao = vision_price or _preco_do_ambiente(PRECO_VISAO_ENV)
    editorial = editorial_direct_usage(
        telemetry_path, hours=hours, job_id=job_id, run_source="cron",
        price_in=preco_editorial[0], price_out=preco_editorial[1],
    )
    visao = vision_direct_usage(
        telemetry_path, hours=hours, job_id=job_id, run_source="cron",
        price_in=preco_visao[0], price_out=preco_visao[1],
    )
    visao_prompt = int(visao["prompt_tokens"])
    editorial_prompt = int(editorial["prompt_tokens"])
    main_requests = int(row[2] or 0)
    custo_editorial = editorial["cost_usd"]
    custo_visao = visao["cost_usd"]
    custo_direto = sum(
        float(valor) for valor in (custo_editorial, custo_visao) if valor is not None
    )
    grand_total_cost = round(custo_main + custo_aux + custo_direto, 6)
    cost_partial = bool(editorial["cost_partial"]) or bool(visao["cost_partial"])
    unpriced = int(editorial["unpriced_requests"]) + int(visao["unpriced_requests"])
    return {
        # Custo em CINCO fatias nomeadas + o total que o teto usa.
        "cost_main_hermes_usd": round(custo_main, 6),
        "cost_aux_hermes_usd": round(custo_aux, 6),
        "cost_editorial_direct_usd": custo_editorial,
        "cost_vision_direct_usd": custo_visao,
        "grand_total_cost_usd": grand_total_cost,
        # ``cost_usd`` e o alias historico: hoje e o GRAND TOTAL (era so
        # main + auxiliar, o que deixava o editorial/visao diretos fora do teto).
        "cost_usd": grand_total_cost,
        "cost_main_usd": round(custo_main, 6),
        "cost_aux_usd": round(custo_aux, 6),
        # Aliases historicos (mesmo valor, nome antigo): o relatorio antigo do
        # rodada6/7 le `cost_direct_vision_usd` e nao pode quebrar.
        "cost_direct_vision_usd": custo_visao,
        "cost_direct_editorial_usd": custo_editorial,
        # Ha requisicao direta sem preco conhecido => o total e um LOWER BOUND.
        "cost_partial": cost_partial,
        "direct_unpriced_requests": unpriced,
        "runs": int(row[1] or 0),
        # REQUESTS em quatro camadas + total.
        "main_requests": main_requests,
        "aux_requests": chamadas_aux,
        "direct_vision_requests": int(visao["requests"]),
        "direct_editorial_requests": int(editorial["requests"]),
        "grand_total_requests": (
            main_requests + chamadas_aux + int(visao["requests"]) + int(editorial["requests"])
        ),
        "requests": (
            main_requests + chamadas_aux + int(visao["requests"]) + int(editorial["requests"])
        ),
        "input_tokens": entrada,
        "output_tokens": int(row[4] or 0),
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        # PROMPT TOKENS em quatro camadas + total (o teto usa o grand total).
        "main_prompt_tokens": prompt_main,
        "aux_prompt_tokens": tokens_aux,
        "direct_vision_prompt_tokens": visao_prompt,
        "direct_editorial_prompt_tokens": editorial_prompt,
        "direct_vision_cached_tokens": int(visao["cached_tokens"]),
        "direct_vision_output_tokens": int(visao["output_tokens"]),
        "direct_editorial_cached_tokens": int(editorial["cached_tokens"]),
        "direct_editorial_output_tokens": int(editorial["output_tokens"]),
        "direct_vision_errors": int(visao["errors"]),
        "direct_editorial_errors": int(editorial["errors"]),
        "direct_vision_requests_without_usage": int(visao["without_usage"]),
        "direct_editorial_requests_without_usage": int(editorial["without_usage"]),
        # Ha requisicao sem `usage` => o total de tokens e LOWER BOUND.
        "direct_vision_tokens_partial": bool(visao["tokens_partial"]),
        "direct_editorial_tokens_partial": bool(editorial["tokens_partial"]),
        "observed_grand_total_tokens_partial": bool(
            visao["tokens_partial"] or editorial["tokens_partial"]
        ),
        "direct_vision_measurable": bool(visao["measurable"]),
        "direct_editorial_measurable": bool(editorial["measurable"]),
        "grand_total_prompt_tokens": prompt_main + tokens_aux + visao_prompt + editorial_prompt,
        "prompt_tokens": prompt_main + tokens_aux + visao_prompt + editorial_prompt,
        "editorial_price_per_1m_usd": list(preco_editorial),
        "vision_price_per_1m_usd": list(preco_visao),
        "scope": scope,
    }


def cost_in_last_24h(
    state_db: Path, job_id: str = "", project_root: str = ""
) -> tuple[float, int] | None:
    """Backward-compatible cost/runs accessor used by scripts and tests."""
    measured = cost_measurement_in_last_24h(state_db, job_id, project_root)
    return None if measured is None else measured[:2]


def context_bytes_in_last_24h(
    telemetry_path: Path,
    *,
    hours: int = 24,
    job_id: str = "",
    run_source: str = "cron",
) -> tuple[int, int] | None:
    """Bytes de stdout que o pipeline devolveu ao modelo na janela do CRON.

    Le ``work/telemetry.jsonl`` e soma ``cmd_output.bytes`` dos comandos
    recentes; devolve ``(bytes, comandos)`` ou ``None`` quando o arquivo nao
    existe (sem medicao = sem bloqueio).

    O filtro por origem/job é obrigatório: sem ele uma sessão MANUAL pesada
    (verificação, investigação do operador) bloquearia o cron — exatamente o
    mesmo defeito que a telemetria já não tem. Eventos antigos, sem
    ``run_source`` gravado, NÃO entram no teto (o limite existe para frear o
    cron; consumir orçamento com evento não atribuível seria pior).
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
            if run_source and record.get("run_source") != run_source:
                continue
            if job_id and str(record.get("cron_job_id") or "") != str(job_id):
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
    parser.add_argument("--price-editorial-in", type=float, default=None,
                        help="USD por 1M tokens de INPUT do editorial direto "
                        "(default: env EDITORIAL_INPUT_COST_PER_1M_USD)")
    parser.add_argument("--price-editorial-out", type=float, default=None,
                        help="USD por 1M tokens de OUTPUT do editorial direto "
                        "(default: env EDITORIAL_OUTPUT_COST_PER_1M_USD)")
    parser.add_argument("--price-vision-in", type=float, default=None,
                        help="USD por 1M tokens de INPUT da visao direta "
                        "(default: env EDITOR_VISION_INPUT_COST_PER_1M_USD)")
    parser.add_argument("--price-vision-out", type=float, default=None,
                        help="USD por 1M tokens de OUTPUT da visao direta "
                        "(default: env EDITOR_VISION_OUTPUT_COST_PER_1M_USD)")
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

    def _precos(entrada, saida, nomes: tuple[str, str]) -> tuple[float, float] | None:
        """Preco do CLI quando informado; o que faltar vem do ambiente."""
        if entrada is None and saida is None:
            return None
        env_in, env_out = _preco_do_ambiente(nomes)
        return (
            float(entrada) if entrada is not None else env_in,
            float(saida) if saida is not None else env_out,
        )

    usage = usage_measurement_in_last_24h(
        args.state_db,
        args.job_id.strip(),
        args.project_root.strip(),
        hours=args.hours,
        telemetry_path=args.telemetry,
        editorial_price=_precos(args.price_editorial_in, args.price_editorial_out,
                                PRECO_EDITORIAL_ENV),
        vision_price=_precos(args.price_vision_in, args.price_vision_out,
                             PRECO_VISAO_ENV),
    )
    if usage is None:
        print(json.dumps({"decision": "allow", "reason": "attribution_unavailable"}))
        return 0
    medidos = {
        # O teto de USD incide sobre o GRAND TOTAL (main + auxiliar + editorial
        # direto + visao direta). Olhar so o Hermes media menos do que gastou.
        "cost_usd": usage["grand_total_cost_usd"],
        "requests": usage["grand_total_requests"],
        "prompt_tokens": usage["grand_total_prompt_tokens"],
    }
    escopo = usage["scope"]
    if args.limit_context_bytes > 0:
        if args.telemetry is None:
            print(json.dumps({"decision": "allow", "reason": "context_unmeasurable"}))
            return 0
        medicao = context_bytes_in_last_24h(
            args.telemetry, hours=args.hours, job_id=args.job_id, run_source="cron"
        )
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
                # Custo separado por CAMADA: main Hermes, auxiliar Hermes,
                # editorial direto, visao direta e o grand total que o teto usa.
                # Camada sem preco conhecido vem `null` + `partial: true` — o
                # gasto existe, o numero e que nao e conhecido.
                "cost": {
                    "cost_main_hermes_usd": usage["cost_main_hermes_usd"],
                    "cost_aux_hermes_usd": usage["cost_aux_hermes_usd"],
                    "cost_editorial_direct_usd": usage["cost_editorial_direct_usd"],
                    "cost_vision_direct_usd": usage["cost_vision_direct_usd"],
                    "grand_total_cost_usd": usage["grand_total_cost_usd"],
                    "partial": usage["cost_partial"],
                    "unpriced_requests": usage["direct_unpriced_requests"],
                    "prices_per_1m_usd": {
                        "editorial": usage["editorial_price_per_1m_usd"],
                        "vision": usage["vision_price_per_1m_usd"],
                    },
                },
                # Buckets separados por CAMADA (main / auxiliar / visao direta /
                # editorial direto) e o total; o diagnostico precisa saber DE
                # ONDE vem cada token.
                "tokens": {
                    "main_prompt": usage["main_prompt_tokens"],
                    "aux_prompt": usage["aux_prompt_tokens"],
                    "direct_vision_prompt": usage["direct_vision_prompt_tokens"],
                    "direct_vision_cached": usage["direct_vision_cached_tokens"],
                    "direct_editorial_prompt": usage["direct_editorial_prompt_tokens"],
                    "direct_editorial_cached": usage["direct_editorial_cached_tokens"],
                    "grand_total_prompt": usage["grand_total_prompt_tokens"],
                    "input": usage["input_tokens"],
                    "cache_read": usage["cache_read_tokens"],
                    "cache_write": usage["cache_write_tokens"],
                    "output": usage["output_tokens"],
                    "partial": usage["observed_grand_total_tokens_partial"],
                },
                "requests": {
                    "main": usage["main_requests"],
                    "aux": usage["aux_requests"],
                    "direct_vision": usage["direct_vision_requests"],
                    "direct_editorial": usage["direct_editorial_requests"],
                    "grand_total": usage["grand_total_requests"],
                },
                "direct_vision_errors": usage["direct_vision_errors"],
                "direct_editorial_errors": usage["direct_editorial_errors"],
                "limits": {chave: valor for chave, valor in limites.items() if valor},
            },
            ensure_ascii=False,
        )
    )
    return 10 if decision == "block" else 0


if __name__ == "__main__":
    raise SystemExit(main())
