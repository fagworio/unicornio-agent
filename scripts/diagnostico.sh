#!/usr/bin/env bash
# Diagnóstico barato da fila editorial (uma chamada; custo < $0.01).
# Economia de tokens: o agente NÃO deve explorar state.db/backups/logs — este
# script é a interface. Saída estável (sem timestamps) para uso interativo.
#
# Uso: scripts/diagnostico.sh
# Requer .env com as credenciais (o CLI lê env direto).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
set -a
# shellcheck disable=SC1091
source ./.env
set +a
echo "== fila =="
"$ROOT/.venv/bin/unicornio-editor" queue --root "$ROOT"
echo "== telemetria (blocagens) =="
"$ROOT/.venv/bin/unicornio-editor" telemetry --root "$ROOT"
