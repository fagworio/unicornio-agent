#!/usr/bin/env bash
# Cron de publicacao do UnicornioHater.
# Publica posts pending READY (manifest SHA-256) ou legado revalidado pelo
# checklist (gate duplo: PUBLISH_ENABLED=true + EDITOR_DRY_RUN=false, apenas
# neste script; o .env continua dry-run para o pipeline editorial).
# Silencioso quando nao ha nada a publicar (padrao watchdog).
#
# Plano de publicacao (America/Sao_Paulo): janelas efetivas de publicacao.
#   00:00 | 08:00 | 12:00 | 18:00 | 21:00
# A janela publica ate 5 posts READY disponiveis. Se houver menos de 5,
# publica todos imediatamente; nao existe espera para completar o limite.
# Os posts READY restantes permanecem para a proxima janela.
#
# Robustez (janelas nao falham): falha transitória de API/Cloudflare nao
# derruba a janela — o comando e re-executado com backoff (3 tentativas) e o
# resultado (ou o erro final) e gravado em work/publish-window.log.
set -euo pipefail
ROOT="${UNICORNIO_AGENT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT"
if [[ ! -f "$ROOT/.env" ]]; then
  echo "Configuracao ausente: $ROOT/.env" >&2
  exit 1
fi
set -a
source .env
set +a

if [[ -f "$ROOT/work/v2-maintenance.lock" ]]; then
  echo "Publicacao bloqueada: reset administrativo V2 em andamento" >&2
  exit 75
fi

if [[ "${PUBLISH_ENABLED:-false}" != "true" ]]; then
  echo "Publicacao desabilitada: PUBLISH_ENABLED precisa ser true" >&2
  exit 0
fi

export EDITOR_DRY_RUN=false

# PUBLISH_LIMIT=5 = teto de cinco publicacoes efetivas por janela.
# Menos de cinco READY sao publicados sem aguardar completar o lote; os
# excedentes permanecem READY para a proxima janela.
export PUBLISH_LIMIT=5

LOG="work/publish-window.log"
ATTEMPT=1
while [ "$ATTEMPT" -le 3 ]; do
  if out="$("$ROOT/.venv/bin/unicornio-editor" publish-ready 2>&1)"; then
    publish_status=0
  else
    publish_status=$?
  fi

  if [ "$publish_status" -eq 0 ] || [ "$publish_status" -eq 75 ]; then
    # A saída 75 significa que o lote foi processado, mas alguns posts ficaram
    # em retry. Os sucessos parciais devem ser relatados agora; repetir o lote
    # dentro da mesma janela só cria leituras/escritas desnecessárias. Os
    # retries permanecem para a próxima janela natural.
    if [ -n "$out" ]; then
      if ! printf '%s\n' "$out" | tee "$LOG" >/dev/null; then
        printf '[%s] falha ao salvar o resultado da janela\n' "$(date -Iseconds)" >> "$LOG" || true
      fi
      if ! printf '%s\n' "$out" | "$ROOT/.venv/bin/python" "$ROOT/hermes/relatorio_publicacao.py"; then
        printf '[%s] falha ao gerar relatorio; publish-ready nao sera repetido\n' "$(date -Iseconds)" >> "$LOG" || true
      fi
    fi
    if [ "$publish_status" -eq 75 ]; then
      printf '[%s] resultado parcial; retries adiados para a proxima janela\n' "$(date -Iseconds)" >> "$LOG"
    fi
    exit 0
  fi
  printf 'publish-ready falhou (tentativa %s/3): %s\n' "$ATTEMPT" "$out" >&2
  printf '[%s] publish-ready tentativa %s/3 FALHOU: %s\n' "$(date -Iseconds)" "$ATTEMPT" "$out" >> "$LOG"
  [ "$ATTEMPT" -lt 3 ] && sleep 60
  ATTEMPT=$((ATTEMPT + 1))
done
printf 'publish-ready falhou apos 3 tentativas' >&2
exit 1
