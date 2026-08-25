#!/usr/bin/env bash
# Monitor do cron editorial (unicorniohater-editor).
#
# Saida ESTAVEL (sem timestamps) consumida pelo `--monitor-script` do Hermes
# cron: o Hermes hasheia a saida exata; enquanto ela nao muda, o agente LLM
# NAO e acordado (idle custa zero tokens). A linha muda SOMENTE quando ha
# trabalho elegivel real:
#   * pending recente nao processado (id)
#   * rework BLOCKED fora de cooldown (id)  — so reativa com progresso
#   * rework em cooldown codificado como id@next_retry_at (minuto) — o hash
#     so muda quando o cooldown realmente expira, nunca a cada tick.
#
# Este arquivo e um TEMPLATE: o cron-install.sh substitui @PROJECT_ROOT@ pelo
# caminho real do projeto ao copiar para $HERMES_HOME/scripts/.
set -euo pipefail
ROOT="@PROJECT_ROOT@"
cd "$ROOT"
set -a
# shellcheck disable=SC1091
source ./.env
set +a
exec "$ROOT/.venv/bin/unicornio-editor" queue --monitor --root "$ROOT"
