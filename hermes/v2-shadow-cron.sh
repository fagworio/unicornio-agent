#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
set -a
. "$ROOT/.env"
set +a
: "${V2_SHADOW_POST_IDS:?set V2_SHADOW_POST_IDS='114893 114895'}"
read -r -a IDS <<< "$V2_SHADOW_POST_IDS"
exec "$ROOT/.venv/bin/unicornio-editor" v2-shadow "${IDS[@]}" \
  --root "$ROOT" \
  --output-dir "${V2_SHADOW_OUTPUT_DIR:-$ROOT/work/v2-shadow/snapshots}"
