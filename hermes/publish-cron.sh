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

LOG="work/publish-window.log"
mkdir -p "$ROOT/work"
PYTHON_BIN="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi
WINDOW_STARTED_AT="$(date --iso-8601=seconds)"
WINDOW_UUID="$($PYTHON_BIN -c 'import uuid; print(uuid.uuid4().hex)')"
export PUBLISH_WINDOW_ID="publish-${WINDOW_STARTED_AT//[:+]/}-${WINDOW_UUID}"
export PUBLISH_CRON_TRIGGERED_AT="$WINDOW_STARTED_AT"

write_gate_report() {
  local reason="$1"
  local code="$2"
  WINDOW_REASON="$reason" WINDOW_EXIT_CODE="$code" "$PYTHON_BIN" - <<'PY' | tee "$LOG" >/dev/null
import json, os
print(json.dumps({
    "window_id": os.environ["PUBLISH_WINDOW_ID"],
    "window_started_at": os.environ["PUBLISH_CRON_TRIGGERED_AT"],
    "cron_triggered": True,
    "cron_triggered_at": os.environ["PUBLISH_CRON_TRIGGERED_AT"],
    "ready_at_window": None,
    "ready_post_ids": [],
    "pending_current": None,
    "published": 0,
    "published_new": 0,
    "published_reconciled": 0,
    "posts": [],
    "blocked_or_skipped": 0,
    "blocked_posts": [],
    "retryable_count": 0,
    "retryable_posts": [],
    "error_count": 0,
    "error_posts": [],
    "zero_publication_reason": os.environ["WINDOW_REASON"],
    "exit_code": int(os.environ["WINDOW_EXIT_CODE"]),
}, ensure_ascii=False))
PY
  "$PYTHON_BIN" "$ROOT/hermes/relatorio_publicacao.py" < "$LOG" || true
}

finalize_window_report() {
  local wrapper_code="$1"
  local cli_code="$2"
  local fallback_classification="$3"
  "$PYTHON_BIN" - "$ROOT" "$LOG" "$PUBLISH_WINDOW_ID" "$wrapper_code" "$cli_code" "$fallback_classification" <<'PY'
import json, os, re, sys, tempfile
from pathlib import Path
root, log_path, window_id, wrapper_code, cli_code, fallback = sys.argv[1:]
log = Path(log_path)
raw = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
report = None
report_valid = False
try:
    report = json.loads(raw)
    report_valid = isinstance(report, dict)
except (json.JSONDecodeError, TypeError, ValueError):
    report = None
if not report_valid:
    report = {
        "window_id": window_id,
        "window_started_at": os.environ.get("PUBLISH_CRON_TRIGGERED_AT"),
        "cron_triggered": True,
        "ready_at_window": None,
        "ready_post_ids": None,
        "published": 0,
        "published_new": 0,
        "published_reconciled": 0,
        "posts": [],
        "blocked_posts": [],
        "retryable_posts": [],
        "error_posts": [{"reason": "publish-ready failed after three attempts"}],
        "error_count": 1,
        "retryable_count": 0,
        "zero_publication_reason": "PUBLISHER_INTERNAL_ERROR",
    }
report["window_id"] = window_id
report["wrapper_exit_code"] = int(wrapper_code)
report["cli_exit_code"] = int(report.get("cli_exit_code", cli_code))
report["exit_code"] = int(report["cli_exit_code"])
if report_valid and report.get("zero_publication_reason") in {"MAINTENANCE_LOCK", "PUBLISH_DISABLED"}:
    final = report["zero_publication_reason"]
elif not report_valid:
    final = fallback
elif int(wrapper_code) != 0:
    final = "PUBLISHER_INTERNAL_ERROR"
elif int(cli_code) == 75:
    final = "PARTIAL_SUCCESS" if int(report.get("published", 0) or 0) else "RETRYABLE_FAILURE"
else:
    final = report.get("zero_publication_reason") or "PUBLISHED"
report["final_classification"] = final
history = Path(root) / "work" / "publish-windows"
history.mkdir(parents=True, exist_ok=True)
def atomic_write(path, value):
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
atomic_write(history / f"{window_id}.json", report)
atomic_write(log, report)
PY
}

if [[ -f "$ROOT/work/v2-maintenance.lock" ]]; then
  echo "Publicacao bloqueada: reset administrativo V2 em andamento" >&2
  write_gate_report "MAINTENANCE_LOCK" 75
  finalize_window_report 75 75 "MAINTENANCE_LOCK"
  exit 75
fi

if [[ "${PUBLISH_ENABLED:-false}" != "true" ]]; then
  echo "Publicacao desabilitada: PUBLISH_ENABLED precisa ser true" >&2
  write_gate_report "PUBLISH_DISABLED" 0
  finalize_window_report 0 0 "PUBLISH_DISABLED"
  exit 0
fi

export EDITOR_DRY_RUN=false

# PUBLISH_LIMIT=5 = teto de cinco publicacoes efetivas por janela.
# Menos de cinco READY sao publicados sem aguardar completar o lote; os
# excedentes permanecem READY para a proxima janela.
export PUBLISH_LIMIT=5

ATTEMPT=1
while [ "$ATTEMPT" -le 3 ]; do
  if out="$("$ROOT/.venv/bin/unicornio-editor" publish-ready 2>&1)"; then
    publish_status=0
  else
    publish_status=$?
  fi

  if [ "$publish_status" -eq 0 ] || [ "$publish_status" -eq 75 ]; then
    # Exit 75 significa processamento parcial; não repetir dentro da janela.
    printf '%s\n' "$out" > "$LOG"
    finalize_window_report 0 "$publish_status" "PARTIAL_SUCCESS"
    if ! "$PYTHON_BIN" "$ROOT/hermes/relatorio_publicacao.py" < "$LOG"; then
      printf '[%s] falha ao gerar relatorio\n' "$(date -Iseconds)" >&2
    fi
    exit 0
  fi

  # Um erro estruturado pode ter ocorrido depois de uma escrita REST. Não
  # repetir: preservar a evidência e fechar a janela com erro.
  if printf '%s\n' "$out" | "$PYTHON_BIN" -c 'import json, sys; value = json.load(sys.stdin); sys.exit(0 if isinstance(value, dict) else 1)' 2>/dev/null; then
    printf '%s\n' "$out" > "$LOG"
    finalize_window_report 1 "$publish_status" "PUBLISHER_INTERNAL_ERROR"
    "$PYTHON_BIN" "$ROOT/hermes/relatorio_publicacao.py" < "$LOG" || true
    exit 1
  fi

  printf 'publish-ready falhou (tentativa %s/3): %s\n' "$ATTEMPT" "$out" >&2
  [ "$ATTEMPT" -lt 3 ] && sleep 60
  ATTEMPT=$((ATTEMPT + 1))
done
printf 'publish-ready falhou apos 3 tentativas\n' >&2
printf '%s\n' "${out:-publish-ready sem saída estruturada}" > "$LOG"
finalize_window_report 1 "${publish_status:-1}" "PUBLISHER_INTERNAL_ERROR"
"$PYTHON_BIN" "$ROOT/hermes/relatorio_publicacao.py" < "$LOG" || true
exit 1
