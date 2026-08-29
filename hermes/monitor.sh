#!/usr/bin/env bash
# Monitor do cron editorial (unicorniohater-editor).
#
# Saida consumida pelo `--monitor-script` do Hermes cron: o Hermes hasheia a
# saida exata; enquanto ela nao muda, o agente LLM NAO e acordado (idle custa
# zero tokens).
#
# O monitor devolve exclusivamente a assinatura estavel da fila. O Hermes
# acorda o agente quando essa assinatura muda: post novo, post processado ou
# cooldown de rework expirado. Nunca inclua hora/tick aqui: isso acordaria o
# LLM a cada polling mesmo sem progresso e transforma backlog parado em custo
# recorrente. Quando nao ha trabalho, a saida e "0"; erro de API vira "ERROR";
# ambos sao estaveis e nao geram spam.
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

# Freio de custo opcional, com atribuição EXATA por ID de job. Sem limite ou
# quando a versão do Hermes não expõe a coluna de job, o guard permite seguir;
# nunca pausamos a operação por uma medição ambígua.
if [ "${HERMES_EDITORIAL_DAILY_COST_LIMIT_USD:-0}" != "0" ]; then
  guard_out="$("$ROOT/.venv/bin/python" "$ROOT/hermes/cost_guard.py" \
    --state-db "${HERMES_STATE_DB:-$HOME/.hermes/state.db}" \
    --job-id "${HERMES_EDITORIAL_CRON_JOB_ID:-}" \
    --limit "${HERMES_EDITORIAL_DAILY_COST_LIMIT_USD}" 2>/dev/null)" || guard_status=$?
  if [ "${guard_status:-0}" -eq 10 ]; then
    printf '%s\n' "BUDGET_EXHAUSTED ${guard_out}"
    exit 0
  fi
fi

out="$("$ROOT/.venv/bin/unicornio-editor" queue --monitor --root "$ROOT" 2>/dev/null)" || out="ERROR"
printf '%s\n' "${out:-0}"
