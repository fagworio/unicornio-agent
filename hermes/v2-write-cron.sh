#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
set -a
. "$ROOT/.env"
set +a
if [[ "${V2_WRITE_ENABLED:-false}" != "true" ]]; then
  printf '%s\n' '{"mode":"write","executed":false,"reason":"V2_WRITE_ENABLED is not true"}'
  exit 0
fi
: "${V2_WRITE_POST_ID:?set V2_WRITE_POST_ID for an explicit canary post}"
: "${V2_WRITE_EDITORIAL_FILE:?set V2_WRITE_EDITORIAL_FILE to a validated editorial JSON}"
exec "$ROOT/.venv/bin/unicornio-editor" v2-write "$V2_WRITE_POST_ID" "$V2_WRITE_EDITORIAL_FILE" \
  --root "$ROOT" --write
