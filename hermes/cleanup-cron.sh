#!/usr/bin/env bash
# Cron de limpeza do UnicornioHater (script-only, padrao watchdog).
# Remove APENAS artefatos inequivocamente sem utilidade:
#   1. work/drafts/*.json            — rascunhos editoriais antigos (default 7 dias)
#   2. work/*.json e work/*.log      — artefatos de apply antigos (default 30 dias)
#   3. backups/<id>/<epoch>.json     — snapshots numericos antigos (default 7 dias)
# Preserva SEMPRE (usados pelo pipeline): prepared.json, editorial.latest.json,
# uncertain.json, editorial_skip_*.json em backups/, alem de .env, src/, hermes/.
# Silencioso quando nada foi removido; imprime resumo quando removeu algo.
# Seguranca: CLEANUP_DRY_RUN=1 lista sem apagar; idades configuráveis via
# CLEANUP_DRAFTS_DAYS / CLEANUP_WORK_DAYS / CLEANUP_SNAPSHOT_DAYS.
set -euo pipefail

ROOT="${UNICORNIO_AGENT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT"

DRAFTS_DAYS="${CLEANUP_DRAFTS_DAYS:-7}"
WORK_DAYS="${CLEANUP_WORK_DAYS:-30}"
SNAPSHOT_DAYS="${CLEANUP_SNAPSHOT_DAYS:-7}"
DRY_RUN="${CLEANUP_DRY_RUN:-0}"

removed=0
removals=()

# remove_if_old <days> <file...> — remove apenas arquivos com mais de N dias.
remove_if_old() {
    local days="$1"
    shift
    local f
    for f in "$@"; do
        [ -f "$f" ] || continue
        if [ -n "$(find "$f" -maxdepth 0 -mtime "+${days}" 2>/dev/null)" ]; then
            removals+=("$f")
            removed=$((removed + 1))
            if [ "$DRY_RUN" != "1" ]; then
                rm -f -- "$f"
            fi
        fi
    done
}

# 1. Rascunhos editoriais antigos (qualquer nome, diretorio dedicado).
if [ -d work/drafts ]; then
    while IFS= read -r f; do
        remove_if_old "$DRAFTS_DAYS" "$f"
    done < <(find work/drafts -maxdepth 1 -type f -name '*.json' 2>/dev/null)
fi

# 2. Artefatos de apply/logs na raiz de work/ (nao entra em subdiretorios).
#    Preserva work/keyart_cache.json (cache de key arts reutilizaveis).
if [ -d work ]; then
    while IFS= read -r f; do
        case "$f" in
            work/keyart_cache.json) continue ;;
        esac
        remove_if_old "$WORK_DAYS" "$f"
    done < <(find work -maxdepth 1 -type f \( -name '*.json' -o -name '*.log' \) 2>/dev/null)
fi

# 3. Snapshots numericos antigos em backups/<id>/ — historico de dumps.
#    Arquivos nomeados (prepared.json, editorial.latest.json, uncertain.json,
#    editorial_skip_*.json) nao comecam com digito e ficam preservados.
if [ -d backups ]; then
    while IFS= read -r f; do
        remove_if_old "$SNAPSHOT_DAYS" "$f"
    done < <(find backups -mindepth 2 -maxdepth 2 -type f -name '[0-9]*.json' 2>/dev/null)
fi

if [ "$removed" -gt 0 ]; then
    if [ "$DRY_RUN" = "1" ]; then
        printf 'cleanup (DRY-RUN): %d arquivo(s) candidato(s), nada apagado\n' "$removed"
    else
        printf 'cleanup: %d arquivo(s) removido(s)\n' "$removed"
    fi
    printf '  %s\n' "${removals[@]}"
fi
exit 0
