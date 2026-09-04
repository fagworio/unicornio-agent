#!/usr/bin/env bash
# Cron de publicacao do UnicornioHater.
# Publica posts pending READY (manifest SHA-256) ou legado revalidado pelo
# checklist (gate duplo: PUBLISH_ENABLED=true + EDITOR_DRY_RUN=false, apenas
# neste script; o .env continua dry-run para o pipeline editorial).
# Silencioso quando nao ha nada a publicar (padrao watchdog).
#
# Plano de publicacao (America/Sao_Paulo): janelas efetivas de publicacao.
#   00:00 | 08:00 | 12:00 | 18:00 | 21:00
# A janela publica TODOS os posts READY disponiveis (o publish-ready itera
# em ciclos ate esgotar a fila; PUBLISH_LIMIT=0 = sem teto por janela).
# O numero 5 (EDITOR_BATCH_LIMIT) e apenas o tamanho do LOTE de processamento
# do editorial, nao o teto de publicacao da janela.
#
# Robustez (janelas nao falham): falha transitória de API/Cloudflare nao
# derruba a janela — o comando e re-executado com backoff (3 tentativas) e o
# resultado (ou o erro final) e gravado em work/publish-window.log.
set -uo pipefail
ROOT="${UNICORNIO_AGENT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT"
set -a
source .env
set +a
export EDITOR_DRY_RUN=false
export PUBLISH_ENABLED=true

# PUBLISH_LIMIT=0 = sem teto por janela: o publish-ready publica TODOS os
# posts READY disponiveis (pagina a fila completa). O cron so dispara nos
# horarios de janela (00|08|12|18|21) — fora deles o script nao roda.
export PUBLISH_LIMIT=0

LOG="work/publish-window.log"
ATTEMPT=1
while [ "$ATTEMPT" -le 3 ]; do
  if out="$("$ROOT/.venv/bin/unicornio-editor" publish-ready 2>&1)"; then
    # Watchdog pattern: stdout vazio = nada a publicar (silencioso).
    if [ -n "$out" ]; then
      # JSON cru vai para o log (auditoria); o Telegram recebe o relatorio
      # legivel (posts id/titulo/data/status + custo de tokens das ultimas 24h).
      printf '%s\n' "$out" | tee "$LOG" | "$ROOT/.venv/bin/python" "$ROOT/hermes/relatorio_publicacao.py"
    fi
    exit 0
  fi
  printf 'publish-ready falhou (tentativa %s/3): %s\n' "$ATTEMPT" "$out" >&2
  printf '[%s] publish-ready tentativa %s/3 FALHOU: %s\n' "$(date -Iseconds)" "$ATTEMPT" "$out" >> "$LOG"
  [ "$ATTEMPT" -lt 3 ] && sleep 60
  ATTEMPT=$((ATTEMPT + 1))
done
printf 'publish-ready falhou apos 3 tentativas' >&2
exit 1
