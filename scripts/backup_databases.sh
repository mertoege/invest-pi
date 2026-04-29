#!/usr/bin/env bash
#
# backup_databases.sh — Daily SQLite-Backup mit gzip-Rotation.
#
# Backup-Strategie:
#   1. Lokal: sqlite3 .backup pro DB → gzip → data/backups/<YYYY-MM-DD>/
#      Rotation: keep 14 Tage, danach geloescht
#   2. Cloud (optional): wenn RESTIC_REPOSITORY + RESTIC_PASSWORD gesetzt sind,
#      restic-Push nach jedem lokalen Backup
#
# systemd-Timer: täglich 03:30
#
set -uo pipefail

REPO_DIR="${REPO_DIR:-/home/investpi/invest-pi}"
DATA_DIR="$REPO_DIR/data"
BACKUP_DIR="$DATA_DIR/backups"
RETENTION_DAYS=14
LOG="$REPO_DIR/logs/backup.log"

mkdir -p "$BACKUP_DIR" "$REPO_DIR/logs"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "[$(ts)] $*" | tee -a "$LOG" >&2; }

DATE="$(date -u +%Y-%m-%d)"
TODAY_DIR="$BACKUP_DIR/$DATE"
mkdir -p "$TODAY_DIR"

log "starting backup → $TODAY_DIR"

# Pro DB: sqlite3 .backup → gzip
TOTAL_BYTES=0
for db in market.db patterns.db alerts.db learning.db trading.db; do
    src="$DATA_DIR/$db"
    [ -f "$src" ] || { log "  skip $db (not exists)"; continue; }

    dst="$TODAY_DIR/$db"
    if sqlite3 "$src" ".backup '$dst'" 2>>"$LOG"; then
        gzip -9 "$dst" 2>>"$LOG"
        sz=$(stat -c%s "$dst.gz" 2>/dev/null || echo 0)
        TOTAL_BYTES=$((TOTAL_BYTES + sz))
        log "  ✔ $db → $(du -h "$dst.gz" | cut -f1)"
    else
        log "  ✘ $db backup failed"
    fi
done

log "total backup size: $((TOTAL_BYTES / 1024)) KB"

# Restic (optional, wenn konfiguriert)
if [[ -n "${RESTIC_REPOSITORY:-}" ]] && [[ -n "${RESTIC_PASSWORD:-}" ]]; then
    if command -v restic >/dev/null 2>&1; then
        log "  restic backup → $RESTIC_REPOSITORY"
        if restic backup "$TODAY_DIR" --quiet 2>>"$LOG"; then
            log "  ✔ restic ok"
            restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --quiet 2>>"$LOG" || true
        else
            log "  ✘ restic failed (see log)"
        fi
    else
        log "  restic not installed — skip cloud backup"
    fi
fi

# Rotation: alte Backups loeschen
find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -mtime +"$RETENTION_DAYS" -exec rm -rf {} + 2>>"$LOG" || true

log "backup done"
