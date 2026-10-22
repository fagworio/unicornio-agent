#!/usr/bin/env bash
set -euo pipefail

# Sincroniza somente o monitor instalado pelo Hermes. Não cria/edita/remove
# jobs, não carrega o .env de produção e não executa o ciclo Editorial.
if [ "$#" -ne 0 ]; then
  echo "uso: $0" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
TARGET_DIR="$HERMES_HOME/scripts"
TARGET="$TARGET_DIR/unicornio-editor-monitor.sh"
RENDERED="$TARGET.tmp.$$"
trap 'rm -f "$RENDERED"' EXIT

mkdir -p "$TARGET_DIR"
sed "s|@PROJECT_ROOT@|$ROOT|g" "$ROOT/hermes/monitor.sh" > "$RENDERED"
"$ROOT/hermes/validate-monitor.sh" "$RENDERED" "$ROOT"
bash -n "$RENDERED"

if [ -f "$TARGET" ]; then
  BACKUP="$TARGET.bak.$(date -u +%Y%m%dT%H%M%SZ).$$"
  cp -p "$TARGET" "$BACKUP"
  chmod --reference="$TARGET" "$RENDERED"
else
  BACKUP=""
  chmod 0755 "$RENDERED"
fi
if [ ! -x "$RENDERED" ]; then
  echo "monitor sync: rendered monitor is not executable" >&2
  exit 1
fi

# O temporário está no mesmo diretório do destino: mv preserva a substituição
# atômica do arquivo que o Hermes executa.
mv -f "$RENDERED" "$TARGET"
"$ROOT/hermes/validate-monitor.sh" "$TARGET" "$ROOT"
bash -n "$TARGET"
if [ ! -x "$TARGET" ]; then
  echo "monitor sync: installed monitor is not executable" >&2
  exit 1
fi

echo "monitor sincronizado: $TARGET"
if [ -n "$BACKUP" ]; then
  echo "backup preservado: $BACKUP"
fi
