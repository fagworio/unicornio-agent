#!/usr/bin/env python3
"""Watchdog de fila do UnicornioHater (script-only, zero tokens de LLM).

Detecta tres riscos de operacao, silencioso quando tudo esta saudavel
(padrao watchdog):

1. FILA BLOQUEADA — posts editados (editorial.latest.json) que nao publicam
   ha dias, ou zero publicacoes nas ultimas 24h com fila pendente.
2. SEM ALIMENTACAO — nenhum post NOVO nas ultimas FEED_HOURS. O alimentador
   e o usuario `redacao` (lote diario de ~8 posts); se ele parar, a fila
   seca, o pipeline fica ocioso e o watchdog antigo ficava justamente em
   silencio (pending=0 nao aciona nenhum dos checks acima). Este e o alerta
   que faltava para "o servico esta se alimentando?".
3. INCERTOS — posts em uncertain.json aguardando revisao humana.

Versionado em hermes/watchdog_fila.py (o work/ e gitignored: antes este
script existia so la e nao tinha historico).
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path("/www/wwwroot/hermes/unicornio-agent")
sys.path.insert(0, str(ROOT / "src"))
from unicornio_editor.config import load_config  # noqa: E402
from unicornio_editor.wordpress import WordPressClient  # noqa: E402
from unicornio_editor.workflow import build_queue_report  # noqa: E402

# O lote de entrada e diario; 36h sem NENHUM post novo ja e anomalia.
# Ajustavel por env para teste (FEED_HOURS=0 simula o alimentador parado).
FEED_HOURS = int(os.environ.get("FEED_HOURS", "36"))

cfg = load_config()
client = WordPressClient(cfg)

report = build_queue_report(client, ROOT, per_page=50)
pending = report["pending"]
edited = report["edited"]
uncertain = report["uncertain"]

now = datetime.now(timezone.utc)
since = (now - timedelta(hours=24)).isoformat()
try:
    published_24h = client._request(
        "GET", "/posts",
        {"status": "publish", "after": since, "per_page": 100, "_fields": "id"},
    )
except Exception as exc:
    print(f"watchdog: ERRO ao consultar publicados: {exc}")
    sys.exit(1)
published_count = len(published_24h) if isinstance(published_24h, list) else -1

# Entrada: qualquer post (de qualquer status) criado nas ultimas FEED_HOURS.
feed_since = (now - timedelta(hours=FEED_HOURS)).isoformat()
try:
    recentes = client._request(
        "GET", "/posts",
        {"status": "any", "after": feed_since, "per_page": 100, "_fields": "id"},
    )
except Exception as exc:
    print(f"watchdog: ERRO ao consultar entrada de posts: {exc}")
    sys.exit(1)
feed_count = len(recentes) if isinstance(recentes, list) else -1

# idade do editorial mais antigo editado (dias desde a ultima modificacao)
oldest_days = 0
if edited:
    latest_files = []
    for d in (ROOT / "backups").iterdir():
        f = d / "editorial.latest.json"
        if f.is_file():
            latest_files.append(f)
    if latest_files:
        oldest = min(f.stat().st_mtime for f in latest_files)
        oldest_days = int((now.timestamp() - oldest) // 86400)

problems = []
if pending and edited and published_count == 0:
    problems.append(
        f"FILA BLOQUEADA: {edited} post(s) editado(s) aguardando publicacao ha ~{oldest_days} dia(s) "
        f"e NENHUM post publicado nas ultimas 24h (checklist bloqueando? rode o checklist para ver os motivos)"
    )
elif pending and edited and published_count > 0:
    problems.append(
        f"atencao: {edited} post(s) editado(s) ha ~{oldest_days} dia(s) sem publicar "
        f"(publicados 24h: {published_count})"
    )
if feed_count == 0:
    problems.append(
        f"SEM ALIMENTACAO: nenhum post novo (qualquer status) nas ultimas {FEED_HOURS}h "
        "— o alimentador (usuario 'redacao') parou? Sem entrada a fila seca e o pipeline fica ocioso"
    )
if uncertain:
    # Separa DECIDIDOS de PENDENTES: o discard grava uncertain.json com
    # discarded=true (triagem feita). Usa os IDs do WP (report) — varrer
    # backups/ contaria tambem arquivos antigos de posts ja publicados.
    pendentes = 0
    for pid in report.get("uncertain_ids") or []:
        f = ROOT / "backups" / str(pid) / "uncertain.json"
        try:
            if not json.loads(f.read_text()).get("discarded"):
                pendentes += 1
        except (OSError, ValueError):
            pendentes += 1
    if pendentes:
        problems.append(f"{pendentes} post(s) em uncertain.json aguardando revisao humana")

if problems:
    print("watchdog fila: " + " | ".join(problems))
else:
    pass  # saudavel: silencioso
