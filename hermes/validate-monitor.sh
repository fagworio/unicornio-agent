#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="${1:-}"
EXPECTED_ROOT="${2:-}"

if [ -z "$SCRIPT_PATH" ] || [ ! -f "$SCRIPT_PATH" ]; then
  echo "monitor validation: rendered script is missing" >&2
  exit 2
fi

if grep -Fq '@PROJECT_ROOT@' "$SCRIPT_PATH"; then
  echo "monitor validation: unresolved @PROJECT_ROOT@ placeholder" >&2
  exit 1
fi

ROOT_VALUE="$(sed -n 's/^ROOT="\([^"]*\)"$/\1/p' "$SCRIPT_PATH" | head -n 1)"
if [ -z "$ROOT_VALUE" ] || [[ "$ROOT_VALUE" != /* ]]; then
  echo "monitor validation: ROOT must be an absolute path" >&2
  exit 1
fi

if [ -n "$EXPECTED_ROOT" ] && [ "$ROOT_VALUE" != "$EXPECTED_ROOT" ]; then
  echo "monitor validation: rendered ROOT does not match project root" >&2
  exit 1
fi

if ! grep -Fq 'queue --monitor --root "$ROOT"' "$SCRIPT_PATH"; then
  echo "monitor validation: queue monitor command is missing" >&2
  exit 1
fi

exit 0
