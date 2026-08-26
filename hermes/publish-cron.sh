#!/usr/bin/env bash
# Cron de publicacao do UnicornioHater.
# Publica posts pending READY (manifest SHA-256) ou legado revalidado pelo
# checklist (gate duplo: PUBLISH_ENABLED=true + EDITOR_DRY_RUN=false, apenas
# neste script; o .env continua dry-run para o pipeline editorial).
# Silencioso quando nao ha nada a publicar (padrao watchdog).
#
# Plano de publicacao (America/Sao_Paulo): ~40 posts/dia
#   00:00 -> 5 | 08:00 -> 7 | 12:00 -> 8 | 18:00 -> 10 | 21:00 -> 10
# (backlog novo chega entre 03:30 e 05:00; as janelas da manha/noite
#  publicam o lote fresco, e 00:00 drena o resto do dia anterior)
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

HOUR="$(date +%H)"
case "$HOUR" in
  00) PUBLISH_LIMIT=5 ;;
  08) PUBLISH_LIMIT=7 ;;
  12) PUBLISH_LIMIT=8 ;;
  18) PUBLISH_LIMIT=10 ;;
  21) PUBLISH_LIMIT=10 ;;
  *)  PUBLISH_LIMIT=0 ;;
esac
export PUBLISH_LIMIT

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
