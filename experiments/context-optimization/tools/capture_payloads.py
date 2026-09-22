#!/usr/bin/env python3
"""Captura FIXTURES reais do Experimento A — somente leitura no projeto.

Extrai o stdout real que o agente viu (mensagens ``role='tool'`` do ``state.db``) para
``fixtures/<sessao>/``, com cabecalho de proveniencia. E a materia-prima do
Experimento A: sem payload real nao da para medir reducao de bytes nem provar
equivalencia de decisao.

Por que do state.db: a telemetria grava apenas o TAMANHO de cada stdout
(``cmd_output.bytes``); o conteudo entregue ao LLM esta nas mensagens da sessao. Este
script reconcilia os dois lados (o inventario de bytes vem da telemetria; o texto vem
daqui).

SEGREDOS: qualquer linha com aparencia de credencial
(``ALGO_TOKEN=...``, ``*_PASSWORD=...``, ``*_API_KEY=...``) tem o VALOR substituido por
``<REDACTADO>`` antes de gravar. O numero de redacoes vai no cabecalho e no index.json.
Sem isso a fixture vira um vazamento versionado (e o CI do repo reprova no step de
secret scan).

Uso:
    python3 tools/capture_payloads.py --session cron_9e39343dc6f5_20260922_064523
        [--state-db PATH] [--out fixtures/<nome>] [--min-bytes 200]
        [--tool terminal] [--limit 0] [--keep-wrapper]
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import re
import sqlite3
import sys

STATE_DB_DEFAULT = pathlib.Path.home() / ".hermes" / "state.db"
FIXTURES_DEFAULT = pathlib.Path(__file__).resolve().parents[1] / "fixtures"
SEGREDO = re.compile(
    r"^(\s*[A-Z][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE_KEY)\s*[:=]\s*)"
    r"(.+?)\s*$", re.MULTILINE)


def redigir(texto: str) -> tuple[str, int]:
    n = 0

    def _sub(m: re.Match[str]) -> str:
        nonlocal n
        valor = m.group(2).strip().strip("'\"")
        if not valor or valor.startswith(("${", "<")):
            return m.group(0)
        n += 1
        return f"{m.group(1)}<REDACTADO>"

    return SEGREDO.sub(_sub, texto), n


def extrair_stdout(conteudo: str) -> tuple[str, dict]:
    """Desembrulha o resultado JSON do tool ``terminal`` ({"output": "..."}).

    O agente consumiu o stdout cru; o wrapper (exit_code/error) fica como metadado do
    cabecalho, nao como parte do payload medido.
    """
    try:
        dados = json.loads(conteudo)
    except ValueError:
        return conteudo, {}
    if isinstance(dados, dict) and isinstance(dados.get("output"), str):
        meta = {k: v for k, v in dados.items() if k != "output"}
        return dados["output"], meta
    return conteudo, {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", required=True)
    ap.add_argument("--state-db", type=pathlib.Path, default=STATE_DB_DEFAULT)
    ap.add_argument("--out", type=pathlib.Path, default=None)
    ap.add_argument("--min-bytes", type=int, default=200,
                    help="ignora entregas menores (ruido de comando curto)")
    ap.add_argument("--tool", default=None, help="filtra por tool_name (ex.: terminal)")
    ap.add_argument("--limit", type=int, default=0, help="0 = todas")
    ap.add_argument("--keep-wrapper", action="store_true")
    args = ap.parse_args()
    if not args.state_db.is_file():
        print(f"state.db nao encontrado: {args.state_db}", file=sys.stderr)
        return 2
    destino = args.out or (FIXTURES_DEFAULT / args.session)
    destino.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(f"file:{args.state_db}?mode=ro", uri=True)
    try:
        linhas = conn.execute(
            "SELECT id, tool_name, COALESCE(content,''), timestamp FROM messages "
            "WHERE session_id=? AND role='tool' ORDER BY id", (args.session,)).fetchall()
    finally:
        conn.close()
    index = {
        "sessao": args.session,
        "state_db": str(args.state_db),
        "capturado_em_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "filtro_tool": args.tool,
        "min_bytes": args.min_bytes,
        "entregas": [],
    }
    gravados = 0
    for mid, tool_name, conteudo, ts in linhas:
        if args.tool and str(tool_name or "") != args.tool:
            continue
        payload, meta = extrair_stdout(conteudo)
        if not args.keep_wrapper and meta:
            bruto = payload
        else:
            bruto = conteudo
        if len(bruto.encode("utf-8")) < args.min_bytes:
            continue
        redigido, redacoes = redigir(bruto)
        quando = (dt.datetime.fromtimestamp(float(ts), dt.timezone.utc)
                  .strftime("%Y-%m-%dT%H:%M:%SZ") if ts else "-")
        digest = hashlib.sha256(redigido.encode("utf-8")).hexdigest()
        nome = f"{mid}_{tool_name or 'tool'}.txt"
        primeira = redigido.strip().splitlines()[0][:120] if redigido.strip() else ""
        cabecalho = [
            "# FIXTURE do Experimento A (payload real entregue ao LLM)",
            f"# origem: {args.state_db} (somente leitura), sessao {args.session}",
            f"# mensagem: {mid} | papel: tool | tool: {tool_name or '-'} | ts: {quando}",
            f"# bytes do payload: {len(redigido.encode('utf-8'))} | sha256: {digest}",
            f"# redacoes de credencial: {redacoes}",
            f"# metadata do wrapper: {json.dumps(meta, ensure_ascii=False)}",
            f"# primeira linha: {primeira}",
            "# ---- payload a partir daqui ----",
        ]
        (destino / nome).write_text("\n".join(cabecalho) + "\n" + redigido, encoding="utf-8")
        index["entregas"].append({
            "arquivo": nome, "mensagem": int(mid), "tool": tool_name,
            "ts": quando, "payload_bytes": len(redigido.encode("utf-8")),
            "sha256": digest, "redacoes": redacoes, "primeira_linha": primeira,
        })
        gravados += 1
        if args.limit and gravados >= args.limit:
            break
    (destino / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    total = sum(e["payload_bytes"] for e in index["entregas"])
    print(f"fixtures em {destino}: {gravados} entregas, {total:,} B no total")
    for e in index["entregas"][:20]:
        print(f"  {e['payload_bytes']:7,d} B  {e['arquivo']:32s} redacoes={e['redacoes']}")
    if gravados > 20:
        print(f"  ... (+{gravados - 20})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
