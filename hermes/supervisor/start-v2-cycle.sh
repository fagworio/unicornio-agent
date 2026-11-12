#!/usr/bin/env bash
set -euo pipefail

UNIT="unicornio-editorial-v2.service"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-/usr/bin/systemctl}"

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
    if ! "$SYSTEMCTL_BIN" start --no-block "$UNIT"; then
      printf 'blocked: systemd rejected start request (%s)\n' "$UNIT" >&2
      exit 1
    fi
    printf 'state=inactive action=start_requested unit=%s\n' "$UNIT"
    ;;
  *)
    printf 'blocked: unknown active state=%s unit=%s\n' "${active_state:-unknown}" "$UNIT" >&2
    exit 2
    ;;
esac
