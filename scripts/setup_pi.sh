#!/usr/bin/env bash
#
# setup_pi.sh — Idempotenter One-Shot-Installer für Invest-Pi.
#
# Voraussetzung: läuft auf einem Raspberry Pi (Debian/Bookworm) als sudo/root.
# Source-Dir ist der Ordner, in dem dieses Skript liegt (z.B. /tmp/invest-pi-staging).
#
# Was passiert:
#   1. apt-Pakete (python3-pip, python3-venv, sqlite3) sicherstellen
#   2. System-User 'investpi' anlegen falls fehlt
#   3. /home/investpi/invest-pi/ als Ziel-Verzeichnis
#   4. Source-Files dorthin kopieren (rsync, idempotent)
#   5. Python-Dependencies via pip --break-system-packages
#   6. data/ Verzeichnis + Permissions
#   7. .env-Template kopieren (überschreibt KEINE existierende .env)
#   8. systemd-Service- und Timer-Files installieren (aber NICHT enablen)
#   9. Smoke-Test als investpi-User
#
# Aufruf:
#   sudo bash scripts/setup_pi.sh
#
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
   echo "ERROR: dieses Skript muss mit sudo gestartet werden." >&2
   exit 1
fi

SOURCE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TARGET_USER="investpi"
TARGET_HOME="/home/${TARGET_USER}"
TARGET_DIR="${TARGET_HOME}/invest-pi"

echo "════════════════════════════════════════════════════════"
echo " Invest-Pi · Pi-Setup"
echo " Source: ${SOURCE_DIR}"
echo " Target: ${TARGET_DIR}"
echo "════════════════════════════════════════════════════════"

# ─── 1. apt-Pakete ────────────────────────────────────────────
echo ""
echo "[1/9] Apt-Pakete sicherstellen..."
apt-get update -qq
apt-get install -y -qq python3-pip python3-venv sqlite3 rsync git >/dev/null

# ─── 2. User anlegen ──────────────────────────────────────────
echo "[2/9] User '${TARGET_USER}' sicherstellen..."
if id "${TARGET_USER}" &>/dev/null; then
    echo "      User existiert bereits"
else
    useradd -m -s /bin/bash "${TARGET_USER}"
    echo "      User angelegt"
fi

# ─── 3+4. Verzeichnis + rsync ─────────────────────────────────
echo "[3/9] Ziel-Verzeichnis ${TARGET_DIR}..."
mkdir -p "${TARGET_DIR}"

echo "[4/9] Source-Files synchronisieren (rsync)..."
rsync -a \
    --exclude=".git" \
    --exclude="__pycache__" \
    --exclude="*.pyc" \
    --exclude="invest-pi-extracted" \
    --exclude=".env" \
    --exclude="data/" \
    "${SOURCE_DIR}/" "${TARGET_DIR}/"

# ─── 5. Python-Dependencies ───────────────────────────────────
echo "[5/9] Python-Dependencies installieren..."
sudo -u "${TARGET_USER}" pip install \
    --break-system-packages \
    --quiet \
    --user \
    -r "${TARGET_DIR}/requirements.txt"

# alpaca-py separat damit es nicht zwingend in requirements.txt sein muss
sudo -u "${TARGET_USER}" pip install --break-system-packages --quiet --user alpaca-py

# ─── 6. data-Verzeichnis ──────────────────────────────────────
echo "[6/9] data/ Verzeichnis..."
mkdir -p "${TARGET_DIR}/data"
chown -R "${TARGET_USER}:${TARGET_USER}" "${TARGET_DIR}"

# ─── 7. .env-Template ─────────────────────────────────────────
echo "[7/9] .env Template..."
if [[ -f "${TARGET_DIR}/.env" ]]; then
    echo "      .env existiert bereits — bleibt unverändert"
else
    cp "${TARGET_DIR}/.env.example" "${TARGET_DIR}/.env"
    chown "${TARGET_USER}:${TARGET_USER}" "${TARGET_DIR}/.env"
    chmod 600 "${TARGET_DIR}/.env"
    echo "      .env aus Template angelegt — DU MUSST DIE KEYS NOCH EINTRAGEN"
fi

# ─── 8. systemd Files ─────────────────────────────────────────
echo "[8/9] systemd Service- und Timer-Files installieren..."
if [[ -d "${SOURCE_DIR}/scripts/systemd" ]]; then
    cp ${SOURCE_DIR}/scripts/systemd/*.service /etc/systemd/system/ 2>/dev/null || true
    cp ${SOURCE_DIR}/scripts/systemd/*.timer   /etc/systemd/system/ 2>/dev/null || true
    systemctl daemon-reload
    echo "      Services + Timer kopiert (NICHT enabled — siehe README)"
fi

# ─── 9. Smoke-Test ────────────────────────────────────────────
echo "[9/9] Smoke-Test als ${TARGET_USER}-User..."
cd "${TARGET_DIR}"
if sudo -u "${TARGET_USER}" PYTHONDONTWRITEBYTECODE=1 python3 -B tests/test_smoke.py 2>&1 | tail -3; then
    echo "      Smoke-Test ✔"
else
    echo "      Smoke-Test ✘ — Setup nicht komplett"
    exit 1
fi

echo ""
echo "════════════════════════════════════════════════════════"
echo "  Setup fertig."
echo ""
echo "  NÄCHSTE SCHRITTE (manuell):"
echo "  1. .env editieren mit Alpaca-Keys:"
echo "       sudo -u ${TARGET_USER} nano ${TARGET_DIR}/.env"
echo ""
echo "  2. Sync-Test gegen Alpaca-Paper:"
echo "       sudo -u ${TARGET_USER} bash -c 'cd ${TARGET_DIR} && python3 scripts/sync_positions.py'"
echo "       (sollte ~10000 USD Paper-Balance zeigen)"
echo ""
echo "  3. systemd-Timer aktivieren (wenn alles passt):"
echo "       sudo systemctl enable --now invest-pi-score.timer"
echo "       sudo systemctl enable --now invest-pi-sync.timer"
echo "       sudo systemctl enable --now invest-pi-outcomes.timer"
echo "       sudo systemctl enable --now invest-pi-strategy.timer"
echo ""
echo "  4. Status prüfen:"
echo "       systemctl list-timers 'invest-pi-*'"
echo "════════════════════════════════════════════════════════"
