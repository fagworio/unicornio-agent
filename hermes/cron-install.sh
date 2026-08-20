#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
SCHEDULE="${1:-every 30m}"
SKILL_NAME="unicorniohater-editor"
SKILL_DIR="$HERMES_HOME/skills/$SKILL_NAME"

mkdir -p "$SKILL_DIR"
cp "$ROOT/hermes/SKILL.md" "$SKILL_DIR/SKILL.md"

PROMPT='Process the next small batch of WordPress posts with status pending. Follow the unicorniohater-editor skill. Operate in write mode: apply the editorial JSON (content + SEO meta), always keeping status pending. Create snapshots, skip irrelevant or uncertain content, validate every editorial JSON result, and report JSON outcomes. Never change a post status and never expose credentials.'

hermes cron create "$SCHEDULE" "$PROMPT" \
  --name "UnicornioHater editorial pending" \
  --skill "$SKILL_NAME" \
  --workdir "$ROOT"
