#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
SCHEDULE="${1:-every 30m}"
SKILL_NAME="unicorniohater-editor"
SKILL_DIR="$HERMES_HOME/skills/$SKILL_NAME"
SCRIPTS_DIR="$HERMES_HOME/scripts"
MONITOR_SCRIPT="$SCRIPTS_DIR/unicornio-editor-monitor.sh"

mkdir -p "$SKILL_DIR" "$SCRIPTS_DIR"
cp "$ROOT/hermes/SKILL.md" "$SKILL_DIR/SKILL.md"
# Referencias do skill (politica de imagens + pitfalls de operacao) — o
# SKILL.md aponta para elas; sem copiar o agente tentaria ler arquivos que
# nao existem (chamadas desperdicadas).
if [ -d "$ROOT/hermes/references" ]; then
  cp -r "$ROOT/hermes/references" "$SKILL_DIR/"
fi
# Monitor: script barato e ESTAVEL consumido pelo --monitor-script do Hermes.
# Enquanto a saida (hash) nao muda, o agente LLM nao e acordado — idle custa
# zero tokens. Ele precisa residir sob $HERMES_HOME/scripts/ (exigencia do
# hermes cron) e rodar do workdir do projeto para achar .env e o CLI.
# Substitui o placeholder pelo caminho real do projeto (o script vive sob
# $HERMES_HOME/scripts/, fora do workdir, entao nao pode inferir ROOT sozinho).
sed "s|@PROJECT_ROOT@|$ROOT|g" "$ROOT/hermes/monitor.sh" > "$MONITOR_SCRIPT"
chmod +x "$MONITOR_SCRIPT"

PROMPT='Process the next small batch of WordPress posts with status pending. Follow the unicorniohater-editor skill. Operate in write mode: apply the editorial JSON (content + SEO meta), always keeping status pending. Create snapshots, skip irrelevant or uncertain content, validate every editorial JSON result, and report JSON outcomes. Never change a post status and never expose credentials.'

hermes cron create "$SCHEDULE" "$PROMPT" \
  --name "UnicornioHater editorial pending" \
  --skill "$SKILL_NAME" \
  --workdir "$ROOT" \
  --monitor-script "$MONITOR_SCRIPT"
