#!/usr/bin/env bash
#
# status_push.sh — Pi schreibt Status-Snapshot ins Git-Repo.
#
# Pattern aus PokéPi: alle 2 Min ein _status/snapshot.json mit System-State,
# damit Claude (von ueberall) den Pi-Status via git fetch + show lesen kann.
#
# Output: _status/snapshot.json (im Repo) mit:
#   - timestamp
#   - git_commit
#   - services (systemctl is-active fuer alle invest-pi-* services)
#   - equity (cash, positions, total) aus letztem equity_snapshot
#   - prediction_counts (total, last_7d, last_24h)
#   - last_runs (score, strategy, sync, outcomes)
#   - hardware (cpu_temp, disk_pct, mem_pct, load)
#
set -uo pipefail

REPO_DIR="${REPO_DIR:-/home/investpi/invest-pi}"
DATA_DIR="${DATA_DIR:-$REPO_DIR/data}"
LOG_DIR="${LOG_DIR:-$REPO_DIR/logs}"
mkdir -p "$LOG_DIR" "$REPO_DIR/_status"
LOG="$LOG_DIR/status_push.log"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "[$(ts)] $*" | tee -a "$LOG" >&2; }

cd "$REPO_DIR" || { log "REPO_DIR not found"; exit 1; }

# Snapshot bauen via Python (sauberer als bash + jq)
python3 - <<'PYEOF' > "$REPO_DIR/_status/snapshot.json.tmp" 2>>"$LOG"
import json, os, subprocess, sqlite3, datetime as dt
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/home/investpi/invest-pi/data"))

def sh(cmd, default=""):
    try:
        return subprocess.check_output(cmd, shell=True, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return default

def query_one(db, sql, default=None):
    if not db.exists(): return default
    try:
        conn = sqlite3.connect(db)
        row = conn.execute(sql).fetchone()
        conn.close()
        return row
    except Exception:
        return default

snap = {
    "timestamp_utc": dt.datetime.utcnow().isoformat(timespec="seconds"),
    "git": {
        "commit":  sh("git rev-parse --short HEAD"),
        "branch":  sh("git rev-parse --abbrev-ref HEAD"),
        "dirty":   bool(sh("git status --porcelain")),
    },
    "services": {},
    "equity": {},
    "predictions": {},
    "last_runs": {},
    "hardware": {},
}

# Services
for svc in ["score", "strategy", "sync", "outcomes", "auto-pull", "status-push"]:
    name = f"invest-pi-{svc}.timer"
    snap["services"][svc] = sh(f"systemctl is-active {name}", default="?")

# Equity (latest snapshot from trading.db)
trading = DATA_DIR / "trading.db"
row = query_one(trading,
    "SELECT cash_eur, positions_value_eur, total_eur, source, timestamp "
    "FROM equity_snapshots ORDER BY timestamp DESC LIMIT 1")
if row:
    snap["equity"] = {
        "cash_eur":            round(row[0], 2),
        "positions_value_eur": round(row[1], 2),
        "total_eur":           round(row[2], 2),
        "source":              row[3],
        "as_of":               row[4],
    }

# Open positions count
pos_row = query_one(trading,
    "SELECT COUNT(*) FROM positions WHERE source='paper'")
snap["equity"]["open_positions"] = pos_row[0] if pos_row else 0

# Predictions
learning = DATA_DIR / "learning.db"
total_row = query_one(learning, "SELECT COUNT(*) FROM predictions")
last_24h  = query_one(learning,
    "SELECT COUNT(*) FROM predictions WHERE created_at >= datetime('now','-1 day')")
last_7d   = query_one(learning,
    "SELECT COUNT(*) FROM predictions WHERE created_at >= datetime('now','-7 day')")
correct   = query_one(learning,
    "SELECT COUNT(*) FROM predictions WHERE outcome_correct = 1")
incorrect = query_one(learning,
    "SELECT COUNT(*) FROM predictions WHERE outcome_correct = 0")
pending   = query_one(learning,
    "SELECT COUNT(*) FROM predictions WHERE outcome_correct IS NULL AND outcome_json IS NULL")

snap["predictions"] = {
    "total":     total_row[0] if total_row else 0,
    "last_24h":  last_24h[0]  if last_24h else 0,
    "last_7d":   last_7d[0]   if last_7d else 0,
    "correct":   correct[0]   if correct else 0,
    "incorrect": incorrect[0] if incorrect else 0,
    "pending":   pending[0]   if pending else 0,
}

# Last runs (from systemd journal — last successful run timestamp)
for svc, label in [("score", "score_portfolio"),
                    ("strategy", "run_strategy"),
                    ("sync", "sync_positions"),
                    ("outcomes", "track_outcomes")]:
    last_run = sh(f"systemctl show invest-pi-{svc}.service -p ExecMainExitTimestamp --value")
    snap["last_runs"][label] = last_run or None

# Hardware
cpu_temp = sh("cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null", default="0")
try: cpu_c = int(cpu_temp) / 1000
except: cpu_c = None
disk = sh("df / | awk 'NR==2 {print $5}' | tr -d '%'")
mem  = sh("free | awk '/Mem:/ {printf \"%.0f\", ($3/$2)*100}'")
load = sh("cat /proc/loadavg | awk '{print $1}'")

snap["hardware"] = {
    "cpu_temp_c": cpu_c,
    "disk_pct":   int(disk) if disk.isdigit() else None,
    "mem_pct":    int(mem)  if mem.isdigit() else None,
    "load_1min":  float(load) if load else None,
}

print(json.dumps(snap, indent=2, sort_keys=True))
PYEOF

if [ ! -s "$REPO_DIR/_status/snapshot.json.tmp" ]; then
    log "snapshot generation failed"
    rm -f "$REPO_DIR/_status/snapshot.json.tmp"
    exit 1
fi

mv "$REPO_DIR/_status/snapshot.json.tmp" "$REPO_DIR/_status/snapshot.json"

# Commit + push (mit pull --rebase als Schutz gegen Race mit Claude-Pushes)
git config core.autocrlf false
git config user.email "investpi-bot@$(hostname).local"
git config user.name  "Invest-Pi-Bot"

git add _status/snapshot.json

if ! git diff --cached --quiet; then
    git -c commit.gpgsign=false commit -q -m "status: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

    # Race-Schutz vor Push: erst rebase auf remote
    git pull --rebase --no-edit --quiet 2>>"$LOG" || {
        log "pull --rebase fehlgeschlagen, abort push"
        git rebase --abort 2>/dev/null || true
        exit 1
    }

    if ! git push --quiet 2>>"$LOG"; then
        # Falls non-fast-forward: force-with-lease
        log "push fehlgeschlagen, retry with force-with-lease"
        git push --force-with-lease --quiet 2>>"$LOG" || {
            log "force-with-lease auch fehlgeschlagen, give up this round"
            exit 1
        }
    fi
fi
