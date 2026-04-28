#!/usr/bin/env bash
#
# setup_pi.sh — Idempotenter Pi-Installer (Git-Clone-basiert).
#
# Voraussetzungen:
#   - Pi mit Debian/Bookworm
#   - sudo-Rechte
#   - GitHub Personal Access Token mit repo-Scope
#
# Aufruf-Modi:
#
#   A) Frischer Pi (clone + setup):
#      sudo GITHUB_TOKEN=ghp_xxx GITHUB_USER=mertoege \
#           bash <(curl -sL https://raw.githubusercontent.com/mertoege/invest-pi/main/scripts/setup_pi.sh)
#
#   B) Re-Run nach Pull (z.B. setup_pi.sh wurde geupdated):
#      sudo bash /home/investpi/invest-pi/scripts/setup_pi.sh
#
# Was passiert:
#   1. apt-Pakete (python3-pip, sqlite3, rsync, git, curl)
#   2. System-User 'investpi' anlegen
#   3. Git-Clone in /home/investpi/invest-pi (mit eingebettetem Token)
#   4. Python-Dependencies via pip --break-system-packages --user
#   5. data/ Verzeichnis + Permissions
#   6. .env aus Template (falls noch nicht existiert)
#   7. systemd-Service- und Timer-Files installieren
#   8. Smoke-Test
#
# Was NICHT passiert (manuell):
#   - .env-Werte (ALPACA_API_KEY etc.) eintragen
#   - systemd-Timer enablen (erst nach .env-Verifizierung)
#
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
   echo "ERROR: dieses Skript muss mit sudo gestartet werden." >&2
   exit 1
fi

TARGET_USER="investpi"
TARGET_HOME="/home/${TARGET_USER}"
TARGET_DIR="${TARGET_HOME}/invest-pi"
GITHUB_USER="${GITHUB_USER:-mertoege}"
REPO_URL="https://github.com/${GITHUB_USER}/invest-pi.git"

# ─── 1. Apt-Pakete ────────────────────────────────────────────
echo "[1/8] Apt-Pakete..."
apt-get update -qq
apt-get install -y -qq python3-pip python3-venv sqlite3 rsync git curl jq >/dev/null

# ─── 2. User ──────────────────────────────────────────────────
echo "[2/8] User '${TARGET_USER}'..."
if id "${TARGET_USER}" &>/dev/null; then
    echo "      existiert bereits"
else
    useradd -m -s /bin/bash "${TARGET_USER}"
    echo "      angelegt"
fi

# ─── 3. Git-Clone ─────────────────────────────────────────────
echo "[3/8] Git-Clone..."
if [[ -d "${TARGET_DIR}/.git" ]]; then
    echo "      Repo existiert bereits — pull nur"
    sudo -u "${TARGET_USER}" -H bash -c "cd ${TARGET_DIR} && git pull --rebase --no-edit --quiet"
else
    if [[ -z "${GITHUB_TOKEN:-}" ]]; then
        echo "ERROR: GITHUB_TOKEN env-Variable nicht gesetzt." >&2
        echo "Aufruf: sudo GITHUB_TOKEN=ghp_xxx GITHUB_USER=mertoege bash <($0)" >&2
        exit 1
    fi
    AUTH_URL="https://oauth2:${GITHUB_TOKEN}@github.com/${GITHUB_USER}/invest-pi.git"
    sudo -u "${TARGET_USER}" -H git clone --quiet "${AUTH_URL}" "${TARGET_DIR}"
    sudo -u "${TARGET_USER}" -H bash -c "cd ${TARGET_DIR} && \
        git config core.autocrlf false && \
        git config user.email 'investpi-bot@${HOSTNAME:-pi}.local' && \
        git config user.name 'Invest-Pi-Bot'"
    echo "      cloned"
fi

# ─── 4. Python-Dependencies ───────────────────────────────────
echo "[4/8] Python-Dependencies..."
sudo -u "${TARGET_USER}" -H pip install --break-system-packages --user --quiet \
    -r "${TARGET_DIR}/requirements.txt"
sudo -u "${TARGET_USER}" -H pip install --break-system-packages --user --quiet alpaca-py

# ─── 5. data-Verzeichnis ──────────────────────────────────────
echo "[5/8] data-Verzeichnis..."
mkdir -p "${TARGET_DIR}/data" "${TARGET_DIR}/logs" "${TARGET_DIR}/_status"
chown -R "${TARGET_USER}:${TARGET_USER}" "${TARGET_DIR}/data" "${TARGET_DIR}/logs" "${TARGET_DIR}/_status"

# ─── 6. .env ──────────────────────────────────────────────────
echo "[6/8] .env Template..."
if [[ -f "${TARGET_DIR}/.env" ]]; then
    echo "      .env existiert — bleibt unveraendert"
else
    cp "${TARGET_DIR}/.env.example" "${TARGET_DIR}/.env"
    chown "${TARGET_USER}:${TARGET_USER}" "${TARGET_DIR}/.env"
    chmod 600 "${TARGET_DIR}/.env"
    echo "      angelegt — DU MUSST DIE KEYS NOCH EINTRAGEN"
fi

# ─── 7. systemd ───────────────────────────────────────────────
echo "[7/8] systemd Files..."
cp ${TARGET_DIR}/scripts/systemd/*.service /etc/systemd/system/ 2>/dev/null || true
cp ${TARGET_DIR}/scripts/systemd/*.timer   /etc/systemd/system/ 2>/dev/null || true
systemctl daemon-reload
echo "      installiert (NICHT enabled — siehe Abschluss)"

# ─── 8. Smoke-Test ────────────────────────────────────────────
echo "[8/8] Smoke-Test..."
cd "${TARGET_DIR}"
sudo -u "${TARGET_USER}" -H PYTHONDONTWRITEBYTECODE=1 python3 -B tests/test_smoke.py 2>&1 | tail -3

echo ""
echo "════════════════════════════════════════════════════════"
echo "  Setup fertig."
echo ""
echo "  NACHSTE SCHRITTE (manuell):"
echo "  1. .env editieren mit Alpaca-Keys + Telegram-Token:"
echo "       sudo -u ${TARGET_USER} nano ${TARGET_DIR}/.env"
echo ""
echo "  2. Sync-Test gegen Alpaca:"
echo "       sudo -u ${TARGET_USER} bash -c 'cd ${TARGET_DIR} && python3 scripts/sync_positions.py'"
echo ""
echo "  3. Wenn Sync OK: alle Timer aktivieren:"
echo "       for s in score sync outcomes strategy auto-pull status-push; do"
echo "         sudo systemctl enable --now invest-pi-\$s.timer"
echo "       done"
echo ""
echo "  4. Status pruefen:"
echo "       systemctl list-timers 'invest-pi-*'"
echo "       cat ${TARGET_DIR}/_status/snapshot.json | jq ."
echo "════════════════════════════════════════════════════════"
