#!/usr/bin/env bash
#
# auto_pull.sh — Pi-side Push-to-Deploy mit Auto-Rollback.
#
# systemd-Timer ruft das alle 2 Min auf. Wenn der Repo-HEAD von dem aktuellen
# Working-Copy abweicht: pull + smoke-test. Wenn Smoke-Test fail: revert.
#
# Voraussetzung:
#   - Repo-Clone in /home/investpi/invest-pi mit eingebettetem PAT in .git/config
#   - .env mit allen API-Keys gesetzt
#   - test_smoke.py + test_trading.py muessen importierbar sein
#
set -uo pipefail

REPO_DIR="${REPO_DIR:-/home/investpi/invest-pi}"
LOG_DIR="${LOG_DIR:-/home/investpi/invest-pi/logs}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/auto_pull.log"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "[$(ts)] $*" | tee -a "$LOG" >&2; }

cd "$REPO_DIR" || { log "REPO_DIR $REPO_DIR not found"; exit 1; }

# Ignoriere CRLF-Phantom-Modifications
git config core.autocrlf false

git fetch --quiet origin main 2>>"$LOG" || { log "fetch failed"; exit 1; }

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    # nichts neues
    exit 0
fi

log "new commits detected: $LOCAL → $REMOTE"

# Snapshot fuer Rollback
PRE_COMMIT="$LOCAL"

# Stash any local non-commited changes (z.B. _status/snapshot.json wenn von uns selbst geschrieben)
git stash push --include-untracked -m "auto_pull-pre-pull-$(ts)" >/dev/null 2>&1 || true

# Pull mit rebase. Bei Conflict: hard-reset auf origin/main.
# Begruendung: lokale "ahead"-Commits sind nur status_push-Snapshots, die werden
# beim naechsten Tick eh neu geschrieben. Lieber clean sync als broken state.
if ! git pull --rebase --no-edit --quiet 2>>"$LOG"; then
    log "pull failed, recovering via hard reset to origin/main"
    git rebase --abort 2>/dev/null || true
    git reset --hard origin/main --quiet 2>>"$LOG" || {
        log "hard reset failed too — manual intervention needed"
        git stash pop 2>/dev/null || true
        exit 1
    }
    git stash pop 2>/dev/null || true
    log "recovered: HEAD now $(git rev-parse --short HEAD)"
fi

# Smoke test (kein network, schneller als 30s)
log "running smoke tests..."
if PYTHONDONTWRITEBYTECODE=1 INVEST_PI_DATA_DIR=/tmp/invest-pi-pull-test python3 -B tests/test_smoke.py >>"$LOG" 2>&1; then
    log "smoke test OK"
else
    log "smoke test FAILED — ROLLBACK to $PRE_COMMIT"
    git reset --hard "$PRE_COMMIT" --quiet 2>>"$LOG"
    git stash pop 2>/dev/null || true
    # Telegram-Notification kommt in Phase 3
    exit 2
fi

# systemd-Files synchronisieren (nur wenn sich was geaendert hat)
SYSTEMD_SRC="$REPO_DIR/scripts/systemd"
SYSTEMD_DST="/etc/systemd/system"
SYSTEMD_CHANGED=0
if [ -d "$SYSTEMD_SRC" ]; then
    for f in "$SYSTEMD_SRC"/*.service "$SYSTEMD_SRC"/*.timer; do
        [ -f "$f" ] || continue
        target="$SYSTEMD_DST/$(basename "$f")"
        if [ ! -f "$target" ] || ! cmp -s "$f" "$target"; then
            sudo cp "$f" "$target" 2>/dev/null && SYSTEMD_CHANGED=1
        fi
    done
    if [ "$SYSTEMD_CHANGED" -eq 1 ]; then
        sudo systemctl daemon-reload 2>/dev/null
        log "systemd-Files synced + daemon-reload"
    fi
fi

# Type=oneshot Services laufen eh erst beim naechsten Timer-Trigger,
# also kein restart noetig.
log "pull+smoke OK, new HEAD: $(git rev-parse --short HEAD)"
