#!/usr/bin/env bash
# Cron de publicacao do UnicornioHater.
# Publica posts pending que passaram no checklist pre-publicacao (gate duplo:
# PUBLISH_ENABLED=true + EDITOR_DRY_RUN=false, apenas neste script; o .env
# continua dry-run para o pipeline editorial). Silencioso quando nao ha
# nada a publicar (padrao watchdog).
set -euo pipefail
ROOT="${UNICORNIO_AGENT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT"
set -a
source .env
set +a
export EDITOR_DRY_RUN=false
export PUBLISH_ENABLED=true
exec .venv/bin/unicornio-editor publish-ready
