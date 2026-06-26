#!/usr/bin/env bash
# Installiert die KI-Swing-Trader systemd-Units (Phase 1 Schatten).
# Muss als root laufen:  sudo bash /home/investpi/invest-pi/scripts/install_ai_swing_timers.sh
set -euo pipefail

SRC=/home/investpi/invest-pi/scripts/systemd
DST=/etc/systemd/system

UNITS=(
  invest-pi-ai-swing.service
  invest-pi-ai-swing.timer
  invest-pi-ai-swing-outcomes.service
  invest-pi-ai-swing-outcomes.timer
)

echo "→ Kopiere Units nach $DST ..."
for u in "${UNITS[@]}"; do
  cp "$SRC/$u" "$DST/$u"
  echo "   $u"
done

echo "→ systemctl daemon-reload ..."
systemctl daemon-reload

echo "→ Timer aktivieren ..."
systemctl enable --now invest-pi-ai-swing.timer invest-pi-ai-swing-outcomes.timer

echo
echo "✓ Fertig. Aktive KI-Swing-Timer:"
systemctl list-timers 'invest-pi-ai-swing*' --all --no-pager
