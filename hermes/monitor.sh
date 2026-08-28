#!/usr/bin/env bash
# Monitor do cron editorial (unicorniohater-editor).
#
# Saida consumida pelo `--monitor-script` do Hermes cron: o Hermes hasheia a
# saida exata; enquanto ela nao muda, o agente LLM NAO e acordado (idle custa
# zero tokens).
#
# LOOP DE PRODUCAO (politica do dono): enquanto existir trabalho pendente
# (posts pending nao processados / rework fora de cooldown), a linha carrega um
# token mutavel (`tick=<epoch>`) — o hash muda a cada execucao e o agente e
# acordado a cada tick para CONTINUAR preparando os posts para as proximas
# janelas de publicacao. Quando nao ha trabalho, a saida e "0" (estavel) e o
# agente dorme (zero custo). Erro de API vira "ERROR" estavel (sem spam).
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

out="$("$ROOT/.venv/bin/unicornio-editor" queue --monitor --root "$ROOT" 2>/dev/null)" || out="ERROR"
case "${out:-0}" in
  ""|"0"|"ERROR")
    printf '%s\n' "${out:-0}"
    ;;
  *)
    printf '%s tick=%s\n' "$out" "$(date +%s)"
    ;;
esac
