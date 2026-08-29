#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
# Cinco posts por sessão amortizam o prompt/skill do agente; duas horas ainda
# preservam boa cadência editorial sem manter uma sessão LLM a cada meia hora.
SCHEDULE="${1:-every 2h}"
SKILL_NAME="unicorniohater-editor"
JOB_NAME="UnicornioHater editorial pending"
SKILL_DIR="$HERMES_HOME/skills/$SKILL_NAME"
SCRIPTS_DIR="$HERMES_HOME/scripts"
MONITOR_SCRIPT="$SCRIPTS_DIR/unicornio-editor-monitor.sh"
JOBS_FILE="$HERMES_HOME/cron/jobs.json"

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

# O Hermes exige monitor_script como NOME relativo a $HERMES_HOME/scripts/
# (caminho absoluto e rejeitado no create). O script ja foi copiado para la.
MONITOR_BASENAME="$(basename "$MONITOR_SCRIPT")"

# ── Idempotencia: NAO duplicar jobs ────────────────────────────────────────
# O `hermes cron create` SEMPRE cria um job novo; rodar o install de novo
# duplicava os crons. Aqui lemos `~$HERMES_HOME/cron/jobs.json` e:
#   * nenhum job com JOB_NAME  -> cria;
#   * um job com JOB_NAME      -> EDITA (atualiza schedule/prompt/skill/monitor);
#   * varios com JOB_NAME      -> remove os excedentes e edita o primeiro
#                                 (limpa duplicatas deixadas por installs antigos).
JOBS_JSON=$(cat "$JOBS_FILE" 2>/dev/null || echo '{"jobs":[]}')

# Extrai os ids (em ordem) dos jobs cujo name == JOB_NAME.
mapfile -t MATCH_IDS < <(OPENAI_JOBS_JSON="$JOBS_JSON" OPENAI_JOB_NAME="$JOB_NAME" python3 - <<'PYEOF'
import json, os
try:
    data = json.loads(os.environ.get("OPENAI_JOBS_JSON", "{}"))
except ValueError:
    data = {}
name = os.environ.get("OPENAI_JOB_NAME", "")
ids = [j.get("id", "") for j in (data.get("jobs") or []) if (j.get("name") or "") == name]
for i in ids:
    print(i)
PYEOF
)

if [ "${#MATCH_IDS[@]}" -eq 0 ]; then
  echo "cron: nenhum job \"$JOB_NAME\" — criando..."
  hermes cron create "$SCHEDULE" "$PROMPT" \
    --name "$JOB_NAME" \
    --skill "$SKILL_NAME" \
    --workdir "$ROOT" \
    --monitor-script "$MONITOR_BASENAME"
else
  PRIMARY="${MATCH_IDS[0]}"
  if [ "${#MATCH_IDS[@]}" -gt 1 ]; then
    echo "cron: ${#MATCH_IDS[@]} jobs duplicados \"$JOB_NAME\" — removendo excedentes e mantendo $PRIMARY..."
    for dup in "${MATCH_IDS[@]:1}"; do
      echo "cron: removendo duplicado $dup"
      hermes cron remove "$dup" || echo "cron: falha ao remover $dup (ignore)"
    done
  else
    echo "cron: job \"$JOB_NAME\" ja existe ($PRIMARY) — atualizando..."
  fi
  hermes cron edit "$PRIMARY" \
    --schedule "$SCHEDULE" \
    --prompt "$PROMPT" \
    --skill "$SKILL_NAME" \
    --workdir "$ROOT" \
    --monitor-script "$MONITOR_BASENAME"
  echo "cron: job \"$JOB_NAME\" atualizado ($PRIMARY)"
fi
