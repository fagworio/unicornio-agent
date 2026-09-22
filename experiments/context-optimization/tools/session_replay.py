#!/usr/bin/env python3
"""Replay offline de uma sessao ja encerrada (Experimento B) — somente leitura.

Le o ``state.db`` em modo somente leitura e responde a pergunta do experimento B:
*o custo esta no payload das tools ou na conversa/sessao do Hermes?*

Por sessao, imprime:

    1. numeros EXATOS   — requests, prompt tokens (input + cache_read + cache_write),
                          cache_read/write, output, reasoning, tool_calls, custo e as
                          chamadas AUXILIARES (session_model_usage com task != '')
    2. composicao       — bytes da conversa por papel e o custo FIXO por request
                          (prompt de sistema)
    3. custo decomposto — bytes x reenvios: cada mensagem e reenviada em todos os
                          requests seguintes, entao `bytes * reenvios` e o peso real de
                          cada componente no total (stdout de tool, abertura com SKILL,
                          prompt de sistema, historico)
    4. calibracao       — `bytes_por_token` implicito e o RESIDUO por request que nao e
                          explicado por prompt de sistema + mensagens (candidato:
                          payload extra da requisicao, ex.: schemas de tools)

CUIDADOS METODOLOGICOS (nao remover — sem eles o numero mente):

* ``sessions.input_tokens`` e apenas o input NAO cacheado; o prompt real e
  ``input + cache_read + cache_write`` (mesma definicao de `session_metrics.py`).
* Chamadas auxiliares (vision, aprovacao, compressao, titulo) ficam em
  ``session_model_usage`` com ``task != ''``; olhar so a tabela ``sessions`` as esconde.
* ``messages.token_count`` esta vazio nesta base: o crescimento por request aqui e um
  PROXY em bytes de conteudo. O total exato em tokens vem dos campos acima.

Stdlib apenas; nao importa o pacote `unicornio_editor` e nao escreve nada.

Uso:
    python3 tools/session_replay.py [--state-db PATH] [--prefix cron_9e39343dc6f5_]
        [session_id ...] [--limit N] [--top N] [--bytes-per-token 3.5] [--json]
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import pathlib
import sqlite3
import sys
from typing import Any

STATE_DB_DEFAULT = pathlib.Path.home() / ".hermes" / "state.db"


def abrir(state_db: pathlib.Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)


def sessoes_alvo(conn: sqlite3.Connection, prefixo: str | None, ids: list[str],
                 limite: int) -> list[str]:
    if ids:
        return ids
    if prefixo:
        linhas = conn.execute(
            "SELECT id FROM sessions WHERE id LIKE ? ORDER BY started_at DESC LIMIT ?",
            (prefixo + "%", limite)).fetchall()
        return [r[0] for r in linhas]
    return [r[0] for r in conn.execute(
        "SELECT id FROM sessions ORDER BY started_at DESC LIMIT ?", (limite,)).fetchall()]


def uso_auxiliar(conn: sqlite3.Connection, sessao: str) -> dict[str, Any]:
    aux: dict[str, Any] = {"chamadas": 0, "tokens": 0, "tarefas": {}}
    for tarefa, chamadas, entrada, saida, cache_r, cache_w in conn.execute(
        "SELECT task, COALESCE(SUM(api_call_count),0), COALESCE(SUM(input_tokens),0), "
        "COALESCE(SUM(output_tokens),0), COALESCE(SUM(cache_read_tokens),0), "
        "COALESCE(SUM(cache_write_tokens),0) FROM session_model_usage WHERE session_id=? "
        "GROUP BY task", (sessao,)):
        if str(tarefa or "") == "":
            continue  # main-loop: ja contado na tabela sessions
        tokens = int(entrada) + int(cache_r) + int(cache_w) + int(saida)
        aux["chamadas"] += int(chamadas)
        aux["tokens"] += tokens
        aux["tarefas"][str(tarefa)] = {"chamadas": int(chamadas), "tokens": tokens}
    return aux


def prompt_sistema_bytes(conn: sqlite3.Connection, sessao: str) -> int | None:
    linha = conn.execute("SELECT system_prompt_hash FROM sessions WHERE id=?", (sessao,)).fetchone()
    if not linha or not linha[0]:
        return None
    tam = conn.execute("SELECT LENGTH(prompt) FROM system_prompts WHERE hash=?",
                       (linha[0],)).fetchone()
    return int(tam[0]) if tam else None


def conversa(conn: sqlite3.Connection, sessao: str, requests: int) -> dict[str, Any]:
    """Sequencia de mensagens + peso de reenvio de cada uma.

    Cada mensagem introduzida ANTES do request *i* participa dos requests i..R; logo
    `reenvios = requests - (assistants anteriores)`. Somar `bytes * reenvios` da o peso
    real de cada componente — foi isso que mostrou que o corte de payload de tool nao
    se limita ao tamanho do JSON: ele se multiplica pelo numero de requests seguintes.
    """
    por_papel: dict[str, dict[str, int]] = collections.defaultdict(lambda: {"msgs": 0, "bytes": 0})
    peso_papel: dict[str, int] = collections.defaultdict(int)
    peso_tool: dict[str, int] = collections.defaultdict(int)
    entregas: list[tuple[int, str, int, str]] = []
    vistos: dict[str, int] = {}
    compactadas = 0
    assistants = 0
    abertura_user = 0
    peso_abertura = 0
    trajetoria: list[int] = []
    acumulado = 0
    for mid, role, tool_name, content, compacted in conn.execute(
        "SELECT id, role, tool_name, COALESCE(content,''), COALESCE(compacted,0) "
        "FROM messages WHERE session_id=? ORDER BY id", (sessao,)):
        bruto = content.encode("utf-8")
        tamanho = len(bruto)
        reenvios = max(1, requests - assistants) if requests else 1
        por_papel[role]["msgs"] += 1
        por_papel[role]["bytes"] += tamanho
        peso_papel[role] += tamanho * reenvios
        compactadas += int(compacted or 0)
        if role == "user" and abertura_user == 0:
            abertura_user = tamanho
            peso_abertura = tamanho * reenvios
        if role == "tool":
            digest = hashlib.sha256(bruto).hexdigest()
            vistos.setdefault(digest, tamanho)
            entregas.append((int(mid), str(tool_name or ""), tamanho, digest))
            peso_tool[str(tool_name or "tool")] += tamanho * reenvios
        acumulado += tamanho
        if role == "assistant":
            assistants += 1
            trajetoria.append(acumulado)
    bytes_total_tools = sum(t[2] for t in entregas)
    return {
        "por_papel": {k: dict(v) for k, v in por_papel.items()},
        "peso_por_papel": dict(peso_papel),
        "peso_por_tool": dict(peso_tool),
        "maiores_tool": sorted(entregas, key=lambda t: -t[2]),
        "tool_entregas": len(entregas),
        "tool_distintas": len(vistos),
        "tool_repetidas": len(entregas) - len(vistos),
        "tool_bytes_total": bytes_total_tools,
        "tool_bytes_unicos": sum(vistos.values()),
        "tool_bytes_repetidos": bytes_total_tools - sum(vistos.values()),
        "compactadas": compactadas,
        "assistants": assistants,
        "abertura_user_bytes": abertura_user,
        "peso_abertura": peso_abertura,
        "trajetoria": trajetoria,
        "bytes_total": acumulado,
    }


def analisar(conn: sqlite3.Connection, sessao: str, bytes_por_token: float) -> dict[str, Any]:
    linha = conn.execute(
        "SELECT api_call_count, input_tokens, output_tokens, cache_read_tokens, "
        "cache_write_tokens, reasoning_tokens, tool_call_count, estimated_cost_usd, "
        "message_count, started_at, ended_at FROM sessions WHERE id=?", (sessao,)).fetchone()
    if not linha:
        return {"session_id": sessao, "erro": "sessao ausente no state.db"}
    (requests, entrada, saida, cache_r, cache_w, reasoning, tool_calls, custo,
     mensagens_n, inicio, fim) = linha
    requests = int(requests or 0)
    entrada, cache_r, cache_w = int(entrada or 0), int(cache_r or 0), int(cache_w or 0)
    prompt_main = entrada + cache_r + cache_w
    aux = uso_auxiliar(conn, sessao)
    prompt_total = prompt_main + aux["tokens"]
    info = conversa(conn, sessao, requests)
    sistema = prompt_sistema_bytes(conn, sessao)
    # custo decomposto (bytes x reenvios)
    peso_conv = sum(info["peso_por_papel"].values())
    peso_tools = sum(info["peso_por_tool"].values())
    peso_sistema = (sistema or 0) * requests
    peso_total = peso_conv + peso_sistema
    implicito = None
    residuo = None
    if prompt_main and peso_total:
        implicito = round(peso_total / prompt_main, 2)
        # residuo por request: tokens do prompt que o prompt de sistema + mensagens
        # nao explicam, na hipotese declarada de bytes_por_token
        explicado = peso_total / bytes_por_token
        residuo = round((prompt_main - explicado) / requests, 1) if requests else None
    duracao = (float(fim) - float(inicio)) if (fim and inicio) else None
    return {
        "session_id": sessao,
        "requests": requests,
        "prompt_tokens_main": prompt_main,
        "prompt_tokens_aux": aux["tokens"],
        "prompt_tokens_grand_total": prompt_total,
        "input_tokens": entrada,
        "cache_read_tokens": cache_r,
        "cache_write_tokens": cache_w,
        "cache_read_share": round(100.0 * cache_r / prompt_main, 2) if prompt_main else None,
        "output_tokens": int(saida or 0),
        "reasoning_tokens": int(reasoning or 0),
        "tool_calls": int(tool_calls or 0),
        "cost_usd": round(float(custo or 0), 6),
        "duracao_s": round(duracao, 1) if duracao else None,
        "mensagens": int(mensagens_n or 0),
        "aux": aux,
        "conversa": info,
        "prompt_sistema_bytes": sistema,
        "prompt_por_request": round(prompt_total / requests, 1) if requests else None,
        "custo_decomposto": {
            "bytes_por_token_assumido": bytes_por_token,
            "peso_conversa": peso_conv,
            "peso_tools": peso_tools,
            "peso_abertura_skill": info["peso_abertura"],
            "peso_prompt_sistema": peso_sistema,
            "peso_total": peso_total,
            "share_tools_pct": round(100.0 * peso_tools / peso_total, 2) if peso_total else None,
            "share_abertura_pct": (round(100.0 * info["peso_abertura"] / peso_total, 2)
                                   if peso_total else None),
            "share_prompt_sistema_pct": (round(100.0 * peso_sistema / peso_total, 2)
                                         if peso_total else None),
        },
        "bytes_por_token_implicito": implicito,
        "residuo_tokens_por_request": residuo,
    }


def imprimir(d: dict[str, Any], top: int) -> None:
    if d.get("erro"):
        print(f"# {d['session_id']}: {d['erro']}")
        return
    conv, custo = d["conversa"], d["custo_decomposto"]
    print(f"# {d['session_id']}")
    print(f"requests (api_call_count): {d['requests']} | duracao: {d['duracao_s']}s | "
          f"mensagens: {d['mensagens']} | tool_calls: {d['tool_calls']}")
    print(f"prompt tokens: main {d['prompt_tokens_main']:,} "
          f"(input {d['input_tokens']:,} + cache_read {d['cache_read_tokens']:,} "
          f"+ cache_write {d['cache_write_tokens']:,}) | aux {d['prompt_tokens_aux']:,} "
          f"| GRAND TOTAL {d['prompt_tokens_grand_total']:,}")
    print(f"cache_read = {d['cache_read_share']}% do prompt main "
          f"(reenvio do MESMO contexto a cada request)")
    print(f"output {d['output_tokens']:,} | reasoning {d['reasoning_tokens']:,} | "
          f"custo US$ {d['cost_usd']}")
    print(f"auxiliar: {d['aux']['chamadas']} chamadas {d['aux']['tarefas'] or '{}'}")
    print(f"prompt por request: {d['prompt_por_request']:,} tokens | "
          f"prompt de sistema: {d['prompt_sistema_bytes']:,} B (fixo por request)")
    print(f"conversa (bytes): total {conv['bytes_total']:,} | por papel {conv['por_papel']}")
    print(f"tool stdout: {conv['tool_entregas']} entregas, {conv['tool_distintas']} distintas, "
          f"{conv['tool_repetidas']} repetidas | bytes {conv['tool_bytes_total']:,} "
          f"| unicos {conv['tool_bytes_unicos']:,} | repetidos {conv['tool_bytes_repetidos']:,}")
    traj = conv["trajetoria"]
    if traj:
        print(f"crescimento proxy: 1o request {traj[0]:,} B -> ultimo {traj[-1]:,} B "
              f"(x{traj[-1] / traj[0]:.1f}); pico {max(traj):,} B | compactadas "
              f"{conv['compactadas']}")
    print("custo decomposto (bytes x reenvios — peso real de cada componente):")
    print(f"   tools stdout       {custo['peso_tools']:>14,}  {custo['share_tools_pct']}%")
    print(f"   abertura (SKILL)   {custo['peso_abertura_skill']:>14,}  {custo['share_abertura_pct']}%"
          f"  ({conv['abertura_user_bytes']:,} B x {d['requests']} requests)")
    print(f"   prompt de sistema  {custo['peso_prompt_sistema']:>14,}  "
          f"{custo['share_prompt_sistema_pct']}%")
    print(f"   TOTAL              {custo['peso_total']:>14,}")
    print(f"calibracao: bytes_por_token implicito {d['bytes_por_token_implicito']} | "
          f"residuo {d['residuo_tokens_por_request']} tokens/request nao explicados por "
          f"sistema+mensagens (hipotese {custo['bytes_por_token_assumido']} B/token)")
    print("maiores entregas de tool:")
    for mid, tool_name, tamanho, digest in conv["maiores_tool"][:top]:
        print(f"   msg {mid} {tool_name:12s} {tamanho:8,d} B  sha256:{digest[:16]}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session_id", nargs="*")
    ap.add_argument("--state-db", type=pathlib.Path, default=STATE_DB_DEFAULT)
    ap.add_argument("--prefix", default="cron_9e39343dc6f5_")
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--bytes-per-token", type=float, default=3.5)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if not args.state_db.is_file():
        print(f"state.db nao encontrado: {args.state_db}", file=sys.stderr)
        return 2
    conn = abrir(args.state_db)
    try:
        alvos = sessoes_alvo(conn, args.prefix if not args.session_id else None,
                             args.session_id, args.limit)
        dados = [analisar(conn, s, args.bytes_per_token) for s in alvos]
    finally:
        conn.close()
    if args.json:
        print(json.dumps(dados, ensure_ascii=False, indent=2))
    else:
        for d in dados:
            imprimir(d, args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
