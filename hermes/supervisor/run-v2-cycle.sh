#!/usr/bin/env bash
set -euo pipefail

ROOT="${UNICORNIO_EDITORIAL_ROOT:-/www/wwwroot/hermes/unicornio-agent}"
if [[ ! -d "$ROOT" ]]; then
  printf 'blocked: project directory missing\n' >&2
  exit 2
fi
cd "$ROOT"
ROOT="$(pwd -P)"
if [[ ! -f "$ROOT/.env" ]]; then
  printf 'blocked: project .env missing\n' >&2
  exit 2
fi
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  printf 'blocked: project virtualenv Python unavailable\n' >&2
  exit 2
fi

set -a
. "$ROOT/.env"
set +a
export PYTHONPATH="$ROOT:$ROOT/src"
export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1
exec "$ROOT/.venv/bin/python" -m unicornio_editor.cli v2-cycle --root "$ROOT"
