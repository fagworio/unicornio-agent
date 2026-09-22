#!/usr/bin/env python3
"""Inventario de PAYLOAD por comando (Experimento A) — somente leitura.

Responde as perguntas do experimento A a partir da telemetria real do pipeline:

    * Qual comando devolve mais bytes?
    * Quantas vezes ele e chamado por READY?
    * Quanto do stdout... (o "quanto e necessario" e respondido pelo candidato
      em `payload-tools.md`, que compara o payload real com uma versao compacta)

Fonte: eventos ``cmd_output`` do ``work/telemetry.jsonl`` do projeto, gravados por
``_record_cmd_output`` (cli.py): ``bytes`` = tamanho do JSON *pretty-printed* que o
comando imprime, ou seja exatamente o texto que o LLM consome; ``kind`` = read ou
write; ``command`` = nome do subcomando.

NAO toca em producao (abre o arquivo com mode=ro implicito de leitura) e NAO importa
o pacote `unicornio_editor`. Stdlib apenas.

Uso:
    python3 tools/telemetry_payload_inventory.py [--telemetry PATH] [--hours N]
        [--run-source cron|manual] [--job-id ID] [--json]
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import pathlib
import sys
from typing import Any

TELEMETRY_DEFAULT = pathlib.Path("/www/wwwroot/hermes/unicornio-agent/work/telemetry.jsonl")


def parse_ts(valor: Any) -> dt.datetime | None:
    if not isinstance(valor, str) or not valor:
        return None
    texto = valor.replace("Z", "+00:00")
    try:
        stamp = dt.datetime.fromisoformat(texto)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)


def percentil(valores: list[int], frac: float) -> int:
    if not valores:
        return 0
    ordenado = sorted(valores)
    pos = min(len(ordenado) - 1, max(0, int(round(frac * (len(ordenado) - 1)))))
    return ordenado[pos]


def carregar(telemetria: pathlib.Path, horas: int | None, run_source: str | None,
             job_id: str | None) -> dict[str, Any]:
    limite = None
    if horas and horas > 0:
        limite = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=horas)
    por_comando: dict[str, list[int]] = collections.defaultdict(list)
    por_kind: collections.Counter = collections.Counter()
    instrumentados = 0
    ready: set[int] = set()
    instrumented_only: dict[str, int] = collections.defaultdict(int)
    with telemetria.open(encoding="utf-8", errors="replace") as fh:
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
            origem = rec.get("run_source")
            if run_source is not None and origem != run_source:
                continue
            if job_id is not None and str(rec.get("cron_job_id") or "") != job_id:
                continue
            if origem:
                instrumentados += 1
            evento = rec.get("event")
            if evento == "apply_ready" and isinstance(rec.get("post_id"), int):
                ready.add(int(rec["post_id"]))
            if evento != "cmd_output":
                continue
            comando = rec.get("command")
            tamanho = rec.get("bytes")
            if not isinstance(comando, str) or not isinstance(tamanho, int):
                continue
            por_comando[comando].append(tamanho)
            por_kind[str(rec.get("kind") or "read")] += tamanho
            if origem:
                instrumented_only[comando] += tamanho
    return {
        "por_comando": por_comando,
        "por_kind": por_kind,
        "instrumentados": instrumentados,
        "ready": ready,
        "instrumented_only": dict(instrumented_only),
    }


def imprimir(dados: dict[str, Any], *, rotulo_fatia: str) -> None:
    por_comando = dados["por_comando"]
    total = sum(sum(v) for v in por_comando.values())
    eventos = sum(len(v) for v in por_comando.values())
    ready = dados["ready"]
    print(f"# Inventario de payload por comando — fatia: {rotulo_fatia}")
    print(f"eventos cmd_output: {eventos} | bytes totais: {total} "
          f"({total / 1024:.1f} KB) | eventos com run_source: {dados['instrumentados']}")
    print(f"posts READY unicos na mesma fatia: {len(ready)}")
    if ready:
        print(f"context_bytes_per_ready: {total / len(ready):.0f} B "
              f"({total / len(ready) / 1024:.1f} KB)")
    print()
    cabecalho = f"{'comando':22s} {'eventos':>7s} {'total B':>11s} {'%':>6s} " \
                f"{'media':>8s} {'p50':>8s} {'p90':>8s} {'max':>9s} {'por READY':>10s}"
    print(cabecalho)
    print("-" * len(cabecalho))
    for comando, valores in sorted(por_comando.items(), key=lambda kv: -sum(kv[1])):
        soma = sum(valores)
        share = 100.0 * soma / total if total else 0.0
        por_ready = f"{soma / len(ready):.0f}" if ready else "-"
        print(f"{comando:22s} {len(valores):7d} {soma:11d} {share:5.1f}% "
              f"{soma / len(valores):8.0f} {percentil(valores, 0.5):8d} "
              f"{percentil(valores, 0.9):8d} {max(valores):9d} {por_ready:>10s}")
    print()
    print("por kind:", dict(sorted(dados["por_kind"].items())))
    if dados["instrumented_only"]:
        print("\nmesma tabela restrita aos eventos instrumentados (run_source presente):")
        for comando, soma in sorted(dados["instrumented_only"].items(), key=lambda kv: -kv[1]):
            print(f"  {comando:22s} {soma:9d} B")
    if total == 0:
        print("\nAVISO: nenhum evento cmd_output na fatia pedida (janela sem execucao?).")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--telemetry", type=pathlib.Path, default=TELEMETRY_DEFAULT)
    ap.add_argument("--hours", type=int, default=0, help="0 = arquivo inteiro")
    ap.add_argument("--run-source", default=None, choices=["cron", "manual"])
    ap.add_argument("--job-id", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if not args.telemetry.is_file():
        print(f"telemetria nao encontrada: {args.telemetry}", file=sys.stderr)
        return 2
    dados = carregar(args.telemetry, args.hours, args.run_source, args.job_id)
    rotulo = f"{args.telemetry} (horas={args.hours or 'todas'}, run_source={args.run_source or 'todos'}"
    rotulo += f", job={args.job_id or '-'})"
    if args.json:
        saida = {
            "fatia": rotulo,
            "total_bytes": sum(sum(v) for v in dados["por_comando"].values()),
            "ready_posts": len(dados["ready"]),
            "por_comando": {
                c: {
                    "eventos": len(v),
                    "bytes": sum(v),
                    "media": round(sum(v) / len(v)),
                    "p50": percentil(v, 0.5),
                    "p90": percentil(v, 0.9),
                    "max": max(v),
                }
                for c, v in sorted(dados["por_comando"].items(), key=lambda kv: -sum(kv[1]))
            },
            "por_kind": dict(dados["por_kind"]),
        }
        print(json.dumps(saida, ensure_ascii=False, indent=2))
    else:
        imprimir(dados, rotulo_fatia=rotulo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
