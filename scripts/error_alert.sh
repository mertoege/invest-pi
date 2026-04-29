#!/usr/bin/env bash
#
# error_alert.sh — Wird via systemd OnFailure aufgerufen wenn ein
# invest-pi-* service fehlschlaegt. Schickt einen Telegram-Push mit
# Service-Name + last journal-lines.
#
# Rate-Limit: max 1 Push pro Service pro Stunde (Lockfile in /tmp).
#
# Aufruf: bash error_alert.sh <service-name>
#         z.B. bash error_alert.sh invest-pi-score.service
#
set -uo pipefail

SERVICE="${1:-unknown.service}"
REPO_DIR="${REPO_DIR:-/home/investpi/invest-pi}"
LOCK_DIR="/tmp/invest-pi-error-locks"
mkdir -p "$LOCK_DIR"
LOCK="$LOCK_DIR/${SERVICE}.lock"

# Cooldown: 1h pro Service
if [ -f "$LOCK" ]; then
    age=$(($(date +%s) - $(stat -c %Y "$LOCK")))
    if [ "$age" -lt 3600 ]; then
        exit 0   # zu früh, skip
    fi
fi
touch "$LOCK"

# .env laden
if [ -f "$REPO_DIR/.env" ]; then
    set -a
    source "$REPO_DIR/.env"
    set +a
fi

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
    exit 0   # kein Telegram konfiguriert
fi

# Letzte 8 Zeilen aus dem service-Log
JOURNAL=$(journalctl -u "$SERVICE" --no-pager -n 8 2>/dev/null | \
          tail -8 | sed 's/[<>&]//g' | head -c 1500)

HOSTNAME=$(hostname)
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

TEXT="❌ <b>Service-Failure · ${HOSTNAME}</b>%0A<code>${SERVICE}</code> ist fehlgeschlagen.%0A%0A<b>Letzte Logs:</b>%0A<pre>${JOURNAL//$'\n'/%0A}</pre>%0A<i>Cooldown: 1h bevor Re-Alert</i>"

curl -sS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "parse_mode=HTML" \
    --data-urlencode "text=${TEXT//\%0A/$'\n'}" \
    >/dev/null 2>&1 || true
