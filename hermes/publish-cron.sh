#!/usr/bin/env bash
# Cron de publicacao do UnicornioHater.
# Publica posts pending que passaram no checklist pre-publicacao (gate duplo:
# PUBLISH_ENABLED=true + EDITOR_DRY_RUN=false, apenas neste script; o .env
# continua dry-run para o pipeline editorial). Silencioso quando nao ha
# nada a publicar (padrao watchdog).
#
# Plano de publicacao (America/Sao_Paulo):
#   00:00 -> 5 posts | 08:00 -> 2 | 12:00 -> 2 | 18:00 -> 3 | 21:00 -> 3
# (backlog novo chega entre 03:30 e 05:00; as janelas da manha/noite
#  publicam o lote fresco, e 00:00 drena o resto do dia anterior)
set -euo pipefail
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
  08) PUBLISH_LIMIT=2 ;;
  12) PUBLISH_LIMIT=2 ;;
  18) PUBLISH_LIMIT=3 ;;
  21) PUBLISH_LIMIT=3 ;;
  *)  PUBLISH_LIMIT=0 ;;
esac
export PUBLISH_LIMIT

exec .venv/bin/unicornio-editor publish-ready
