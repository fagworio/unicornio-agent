#!/usr/bin/env bash
set -euo pipefail

UNIT="unicornio-editorial-v2.service"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-/usr/bin/systemctl}"
ROOT="${UNICORNIO_EDITORIAL_ROOT:-/www/wwwroot/hermes/unicornio-agent}"
CONFIRM_TIMEOUT_SEC="${SUPERVISOR_START_CONFIRM_TIMEOUT_SEC:-15}"

if ! [[ "$CONFIRM_TIMEOUT_SEC" =~ ^[0-9]+$ ]]; then
  printf 'blocked: invalid confirmation timeout (%s)\n' "$CONFIRM_TIMEOUT_SEC" >&2
  exit 2
fi

file_hash() {
  local path="$1"
  if [[ -f "$path" ]]; then
    sha256sum "$path" | awk '{print $1}'
  else
    printf 'absent'
  fi
}

show_property() {
  "$SYSTEMCTL_BIN" show --property="$1" --value "$UNIT" 2>/dev/null
}

if [[ ! -x "$SYSTEMCTL_BIN" ]]; then
  printf 'blocked: systemctl executable unavailable\n' >&2
  exit 2
fi

if ! load_state="$("$SYSTEMCTL_BIN" show --property=LoadState --value "$UNIT" 2>/dev/null)"; then
  printf 'blocked: service state unavailable (%s)\n' "$UNIT" >&2
  exit 2
fi
if [[ "$load_state" != "loaded" ]]; then
  printf 'blocked: service is not installed (LoadState=%s)\n' "${load_state:-unknown}" >&2
  exit 2
fi

if ! active_state="$("$SYSTEMCTL_BIN" show --property=ActiveState --value "$UNIT" 2>/dev/null)"; then
  printf 'blocked: active state unavailable (%s)\n' "$UNIT" >&2
  exit 2
fi

case "$active_state" in
  active|activating|reloading|deactivating)
    printf 'state=%s action=monitor_only unit=%s\n' "$active_state" "$UNIT"
    exit 0
    ;;
  failed)
    printf 'state=failed action=manual_reset_required unit=%s\n' "$UNIT"
    exit 1
    ;;
  inactive)
    before_state_hash="$(file_hash "$ROOT/work/v2-cycle/state.json")"
    before_journal_hash="$(file_hash "$ROOT/work/v2-cycle/journal.jsonl")"
    if ! "$SYSTEMCTL_BIN" start --no-block "$UNIT"; then
      printf 'blocked: systemd rejected start request (%s)\n' "$UNIT" >&2
      exit 1
    fi
    deadline=$((SECONDS + CONFIRM_TIMEOUT_SEC))
    while (( SECONDS <= deadline )); do
      active_state="$(show_property ActiveState || true)"
      result="$(show_property Result || true)"
      main_pid="$(show_property MainPID || true)"
      exec_start="$(show_property ExecMainStartTimestamp || true)"
      if [[ "$active_state" == "failed" ]]; then
        printf 'state=failed action=manual_reset_required unit=%s result=%s\n' "$UNIT" "${result:-unknown}"
        exit 1
      fi
      if [[ "$active_state" == "activating" || "$active_state" == "active" || "$active_state" == "deactivating" ]]; then
        if [[ "${main_pid:-0}" != "0" && -n "$main_pid" ]] || [[ -n "$exec_start" ]]; then
          printf 'state=started status=running execution_confirmed=true unit=%s main_pid=%s\n' "$UNIT" "${main_pid:-unknown}"
          exit 0
        fi
      fi
      if [[ "$active_state" == "inactive" && -n "$exec_start" ]]; then
        after_state_hash="$(file_hash "$ROOT/work/v2-cycle/state.json")"
        after_journal_hash="$(file_hash "$ROOT/work/v2-cycle/journal.jsonl")"
        progress="false"
        if [[ "$before_state_hash" != "$after_state_hash" || "$before_journal_hash" != "$after_journal_hash" ]]; then
          progress="true"
        fi
        printf 'state=completed status=%s execution_confirmed=true checkpoint_changed=%s unit=%s\n' "${result:-unknown}" "$progress" "$UNIT"
        if [[ "$result" != "success" ]]; then
          exit 1
        fi
        exit 0
      fi
      sleep 1
    done
    printf 'state=start_unconfirmed action=manual_inspection_required unit=%s timeout_seconds=%s\n' "$UNIT" "$CONFIRM_TIMEOUT_SEC" >&2
    exit 1
    ;;
  *)
    printf 'blocked: unknown active state=%s unit=%s\n' "${active_state:-unknown}" "$UNIT" >&2
    exit 2
    ;;
esac
