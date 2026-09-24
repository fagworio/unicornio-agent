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
# FREIO DE ORCAMENTO SEM MUDAR O HASH: quando o guard bloqueia (custo/contexto),
# a saida NAO pode virar uma mensagem nova — a transicao de assinatura normal
# para qualquer texto de bloqueio (e o JSON do guard, que muda a cada janela)
# seria uma MUDANCA de saida e acordaria justamente a execucao que o freio quer
# evitar. Aqui o bloqueio apenas REPETE a ultima saida efetiva (arquivo
# work/monitor_effective_output) e manda o detalhe para log.
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

EFFECTIVE_FILE="$ROOT/work/monitor_effective_output"
BUDGET_LOG="$ROOT/work/monitor-budget.log"
BUDGET_STATE_FILE="$ROOT/work/monitor-budget-state"

# Estado separado para detectar blocked -> allowed sem imprimir o JSON mutável
# do guard. O primeiro tick liberado muda a época e acorda o Hermes; depois a
# assinatura volta a ficar estável.
budget_epoch=0
budget_blocked=0
if [ -s "$BUDGET_STATE_FILE" ]; then
  read -r budget_epoch budget_blocked < "$BUDGET_STATE_FILE" || true
  budget_epoch="${budget_epoch:-0}"
  budget_blocked="${budget_blocked:-0}"
fi
case "$budget_epoch" in *[!0-9]*|'') budget_epoch=0 ;; esac
case "$budget_blocked" in 1) ;; *) budget_blocked=0 ;; esac

salvar_estado_budget() {
  mkdir -p "$ROOT/work" 2>/dev/null || true
  printf '%s %s\n' "$budget_epoch" "$budget_blocked" > "$BUDGET_STATE_FILE" 2>/dev/null || true
}

# Repete a ultima assinatura efetiva (bloqueio NAO altera o hash). Sem arquivo
# ainda, a saida e "0" (estavel).
emitir_assinatura_congelada() {
  if [ -s "$EFFECTIVE_FILE" ]; then
    cat "$EFFECTIVE_FILE"
  else
    printf '0\n'
  fi
}

# Freios do cron editorial (opcionais). A atribuição por ID de job exato é
# obrigatória no banco moderno; quando não há limite configurado ou a medição é
# ambígua, o guard permite seguir — nunca pausamos a operação por uma medição
# dupla. Além do custo em USD, o guard vigia VOLUME (requests, prompt tokens =
# input + cache e os bytes de contexto devolvidos ao modelo): com deepseek-flash
# o dólar não percebe uma regressão de contexto.
if [ "${HERMES_EDITORIAL_DAILY_COST_LIMIT_USD:-0}" != "0" ] \
  || [ "${HERMES_EDITORIAL_WINDOW_REQUEST_LIMIT:-0}" != "0" ] \
  || [ "${HERMES_EDITORIAL_WINDOW_PROMPT_TOKEN_LIMIT:-0}" != "0" ] \
  || [ "${HERMES_EDITORIAL_WINDOW_INPUT_TOKEN_LIMIT:-0}" != "0" ] \
  || [ "${HERMES_EDITORIAL_WINDOW_CONTEXT_BYTES_LIMIT:-0}" != "0" ]; then
  guard_out="$("$ROOT/.venv/bin/python" "$ROOT/hermes/cost_guard.py" \
    --state-db "${HERMES_STATE_DB:-$HOME/.hermes/state.db}" \
    --job-id "${HERMES_EDITORIAL_CRON_JOB_ID:-}" \
    --project-root "$ROOT" \
    --limit "${HERMES_EDITORIAL_DAILY_COST_LIMIT_USD:-0}" \
    --limit-requests "${HERMES_EDITORIAL_WINDOW_REQUEST_LIMIT:-0}" \
    --limit-prompt-tokens "${HERMES_EDITORIAL_WINDOW_PROMPT_TOKEN_LIMIT:-0}" \
    --limit-input-tokens "${HERMES_EDITORIAL_WINDOW_INPUT_TOKEN_LIMIT:-0}" \
    --limit-context-bytes "${HERMES_EDITORIAL_WINDOW_CONTEXT_BYTES_LIMIT:-0}" \
    --telemetry "$ROOT/work/telemetry.jsonl" 2>/dev/null)" || guard_status=$?
  if [ "${guard_status:-0}" -eq 10 ]; then
    # Detalhe vai para LOG (nao para stdout: stdout e assinatura, nao relatorio).
    mkdir -p "$ROOT/work" 2>/dev/null || true
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$guard_out" >> "$BUDGET_LOG" 2>/dev/null || true
    budget_blocked=1
    salvar_estado_budget
    emitir_assinatura_congelada
    exit 0
  fi
fi

out="$("$ROOT/.venv/bin/unicornio-editor" queue --monitor --root "$ROOT" 2>/dev/null)" || out="ERROR"
out="${out:-0}"
if [ "$budget_blocked" = "1" ]; then
  budget_blocked=0
  budget_epoch=$((budget_epoch + 1))
fi
salvar_estado_budget
if [ "$budget_epoch" -gt 0 ]; then
  out="${out}|budget_epoch=${budget_epoch}"
fi
mkdir -p "$ROOT/work" 2>/dev/null || true
printf '%s\n' "$out" > "$EFFECTIVE_FILE" 2>/dev/null || true
printf '%s\n' "$out"
