#!/usr/bin/env python3
"""Criterio A x B (Experimento A vs B) — somente leitura.

Cruza as duas fontes reais e diz QUAL experimento atacar primeiro, com limiares
DECLARADOS no codigo em vez de "achismo".

Importa o modelo de peso do `session_replay.py` (bytes x reenvios): o peso de um
componente NAO e o tamanho do seu texto, e sim `bytes x numero de requests seguintes`,
porque o mesmo contexto e reenviado a cada request (cache_read ~98-99% do prompt).

Metricas por sessao do cron:
    requests, prompt tokens (grand total), tool_context_bytes (telemetria),
    cache_read_share, tool_weighted_share (peso do stdout de tool no custo),
    peso da abertura (SKILL injetada), peso do prompt de sistema.

Regra v0 (provisoria, revisavel — a primeira sessao VALIDA decide):

    tool_weighted_share >= 50%            -> A primeiro (payload das tools)
    cache_read_share >= 90% e share < 50% -> B primeiro (sessao/replay do Hermes)
    nenhum dos dois                       -> SPLIT: medir os dois antes de cortar

Ha ainda um terceiro item, de MEDICAO e nao de otimizacao: a fracao do prompt por
request que o texto visivel no state.db nao explica (abaixo o teste de sensibilidade
por densidade assumida). Enquanto isso nao for medido, cortar bytes e apostar.

Respeita a fatia oficial: por default filtra ``run_source=cron`` e ``--job-id``.
Sem evento do cron na janela imprime "AMOSTRA OFICIAL VAZIA" e NAO recomenda — o
estado esperado enquanto o experimento estiver ARMADO.

Uso:
    python3 tools/ab_criterion.py [--telemetry PATH] [--state-db PATH] [--hours 24]
        [--job-id 9e39343dc6f5] [--prefix cron_9e39343dc6f5_] [--tool-share-threshold 50]
        [--cache-share-threshold 90] [--include-historical] [--json]
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import pathlib
import sqlite3
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import session_replay as sr  # noqa: E402  (ferramenta irma do mesmo diretorio)

TELEMETRY_DEFAULT = pathlib.Path("/www/wwwroot/hermes/unicornio-agent/work/telemetry.jsonl")
STATE_DB_DEFAULT = pathlib.Path.home() / ".hermes" / "state.db"
JOB_DEFAULT = "9e39343dc6f5"
DENSIDADES = (2.0, 2.5, 3.0, 3.5)


def parse_ts(valor: Any) -> dt.datetime | None:
    if not isinstance(valor, str) or not valor:
        return None
    try:
        stamp = dt.datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)


def fatia_telemetria(caminho: pathlib.Path, limite: dt.datetime | None,
                     run_source: str | None, job_id: str | None) -> dict[str, Any]:
    bytes_total = 0
    por_comando: dict[str, int] = collections.defaultdict(int)
    ready: set[int] = set()
    eventos = instrumentados = 0
    with caminho.open(encoding="utf-8", errors="replace") as fh:
        for linha in fh:
            linha = linha.strip()
            if not linha:
                continue
            try:
                rec = json.loads(linha)
            except ValueError:
                continue
            stamp = parse_ts(rec.get("ts"))
            if limite is not None and (stamp is None or stamp < limite):
                continue
            if run_source is not None and rec.get("run_source") != run_source:
                continue
            if job_id is not None and str(rec.get("cron_job_id") or "") != job_id:
                continue
            eventos += 1
            if rec.get("run_source"):
                instrumentados += 1
            if rec.get("event") == "apply_ready" and isinstance(rec.get("post_id"), int):
                ready.add(int(rec["post_id"]))
            if rec.get("event") == "cmd_output":
                comando, tamanho = rec.get("command"), rec.get("bytes")
                if isinstance(comando, str) and isinstance(tamanho, int):
                    bytes_total += tamanho
                    por_comando[comando] += tamanho
    return {"eventos": eventos, "instrumentados": instrumentados, "ready": ready,
            "bytes": bytes_total, "por_comando": dict(por_comando)}


def sessoes_na_janela(caminho: pathlib.Path, prefixo: str,
                      limite: dt.datetime | None) -> list[str]:
    conn = sqlite3.connect(f"file:{caminho}?mode=ro", uri=True)
    try:
        ids = []
        for sid, inicio in conn.execute(
                "SELECT id, started_at FROM sessions WHERE id LIKE ? ORDER BY started_at",
                (prefixo + "%",)):
            quando = dt.datetime.fromtimestamp(float(inicio), dt.timezone.utc) if inicio else None
            if limite is not None and (quando is None or quando < limite):
                continue
            ids.append(sid)
    finally:
        conn.close()
    return ids


def decidir(tool_share: float | None, cache_share: float | None,
            limiar_tool: float, limiar_cache: float) -> tuple[str, str]:
    if tool_share is None or cache_share is None:
        return ("INSUFICIENTE",
                "falta denominador (payload de tool e/ou prompt): nao recomenda nada")
    if tool_share >= limiar_tool:
        return ("A primeiro",
                f"stdout de tool pesa {tool_share:.1f}% do custo por request "
                f"(>= {limiar_tool}%): payload e a maior massa identificada")
    if cache_share >= limiar_cache:
        return ("B primeiro",
                f"cache_read e {cache_share:.1f}% do prompt e tools pesam {tool_share:.1f}%: "
                "o custo e reenvio de contexto/sessao, nao payload")
    return ("SPLIT",
            f"tools {tool_share:.1f}% e cache_read {cache_share:.1f}%: nenhum dos dois "
            "domina — medir os dois antes de cortar")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--telemetry", type=pathlib.Path, default=TELEMETRY_DEFAULT)
    ap.add_argument("--state-db", type=pathlib.Path, default=STATE_DB_DEFAULT)
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--job-id", default=JOB_DEFAULT)
    ap.add_argument("--prefix", default="cron_9e39343dc6f5_")
    ap.add_argument("--tool-share-threshold", type=float, default=50.0)
    ap.add_argument("--cache-share-threshold", type=float, default=90.0)
    ap.add_argument("--include-historical", action="store_true",
                    help="mostra tambem a fatia sem run_source (contaminada por execucao manual)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    limite = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=args.hours)
    oficial = fatia_telemetria(args.telemetry, limite, "cron", args.job_id)
    ids = sessoes_na_janela(args.state_db, args.prefix, limite)
    conn = sr.abrir(args.state_db)
    try:
        analises = [sr.analisar(conn, sid, 3.5) for sid in ids]
    finally:
        conn.close()
    analises = [a for a in analises if not a.get("erro")]
    ready = len(oficial["ready"])
    prompt_total = sum(a["prompt_tokens_grand_total"] for a in analises)
    requests = sum(a["requests"] for a in analises)
    cache_r = sum(a["cache_read_tokens"] for a in analises)
    cache_share = round(100.0 * cache_r / prompt_total, 2) if prompt_total else None
    pesos = [a["custo_decomposto"]["peso_tools"] for a in analises]
    totais = [a["custo_decomposto"]["peso_total"] for a in analises]
    tool_share = (round(100.0 * sum(pesos) / sum(totais), 2) if sum(totais) else None)
    sensibilidade = {}
    for densidade in DENSIDADES:
        explicado = sum(t / densidade for t in totais)
        nao_explicado = prompt_total - explicado
        sensibilidade[f"{densidade}_bytes_por_token"] = {
            "tokens_explicados": round(explicado),
            "nao_explicado_pct": round(100.0 * nao_explicado / prompt_total, 1) if prompt_total else None,
        }
    escolha, motivo = decidir(tool_share, cache_share, args.tool_share_threshold,
                             args.cache_share_threshold)
    dados = {
        "janela_horas": args.hours,
        "fatia_oficial_cron": {
            "eventos": oficial["eventos"], "eventos_instrumentados": oficial["instrumentados"],
            "ready_posts": ready, "tool_context_bytes": oficial["bytes"],
            "context_bytes_per_ready": round(oficial["bytes"] / ready) if ready else None,
        },
        "sessoes_do_cron_na_janela": [a["session_id"] for a in analises],
        "requests": requests,
        "prompt_tokens_grand_total": prompt_total,
        "prompt_tokens_per_ready": round(prompt_total / ready) if ready else None,
        "requests_per_ready": round(requests / ready, 1) if ready else None,
        "cache_read_share_pct": cache_share,
        "tool_weighted_share_pct": tool_share,
        "limiares": {"tool_share_pct": args.tool_share_threshold,
                     "cache_read_share_pct": args.cache_share_threshold},
        "sensibilidade_densidade": sensibilidade,
        "recomendacao": escolha,
        "motivo": motivo,
        "amostra_oficial_vazia": oficial["eventos"] == 0 and not analises,
    }
    if args.include_historical:
        hist = fatia_telemetria(args.telemetry, limite, None, None)
        dados["historico_sem_run_source"] = {
            "eventos": hist["eventos"], "ready_posts": len(hist["ready"]),
            "tool_context_bytes": hist["bytes"],
            "aviso": "mistura execucao manual; NAO e baseline oficial",
        }
    if args.json:
        print(json.dumps(dados, ensure_ascii=False, indent=2))
        return 0
    print(f"# Criterio A x B — janela {args.hours}h, job {args.job_id}")
    if dados["amostra_oficial_vazia"]:
        print("AMOSTRA OFICIAL VAZIA (sem evento run_source=cron e sem sessao do job na")
        print("janela). Experimento ARMADO: nao ha o que decidir ainda. Rode depois da")
        print("primeira sessao cron VALIDA (ver hermes/references/congelamento-experimento.md).")
        if not args.include_historical:
            print("--include-historical mostra a fatia contaminada apenas como referencia.")
            return 0
    print(f"eventos da fatia oficial: {oficial['eventos']} "
          f"(instrumentados {oficial['instrumentados']}) | READY unicos: {ready}")
    print(f"sessoes do job na janela: {len(analises)} | requests: {requests} | "
          f"prompt tokens (grand total): {prompt_total:,}")
    print(f"tool_context_bytes_per_ready : {dados['fatia_oficial_cron']['context_bytes_per_ready']}")
    print(f"prompt_tokens_per_ready      : {dados['prompt_tokens_per_ready']}")
    print(f"requests_per_ready           : {dados['requests_per_ready']}")
    print(f"cache_read_share             : {cache_share}%")
    print(f"tool_weighted_share          : {tool_share}% (peso do stdout de tool no custo)")
    print(f"recomendacao                 : {escolha} — {motivo}")
    print("sensibilidade: fracao do prompt NAO explicada por sistema+mensagens")
    for chave, valores in sensibilidade.items():
        print(f"   {chave:24s} explicados {valores['tokens_explicados']:>12,} "
              f"| nao explicado {valores['nao_explicado_pct']}%")
    if "historico_sem_run_source" in dados:
        h = dados["historico_sem_run_source"]
        print(f"[referencia, NAO oficial] eventos {h['eventos']} | READY {h['ready_posts']} | "
              f"bytes {h['tool_context_bytes']:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
