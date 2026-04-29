#!/usr/bin/env bash
#
# systemd_sync.sh — Wrapper fuer auto_pull's systemd-File-Sync.
# Wird von auto_pull.sh via NOPASSWD-sudoers aufgerufen.
#
# Vergleicht /home/investpi/invest-pi/scripts/systemd/*.{service,timer}
# mit /etc/systemd/system/, kopiert bei Diff, daemon-reload.
#
# Returns: 0 wenn nichts geaendert, 1 wenn Files kopiert wurden.
#
set -uo pipefail

SRC="/home/investpi/invest-pi/scripts/systemd"
DST="/etc/systemd/system"
CHANGED=0

[ -d "$SRC" ] || exit 0

for f in "$SRC"/*.service "$SRC"/*.timer; do
    [ -f "$f" ] || continue
    target="$DST/$(basename "$f")"
    if [ ! -f "$target" ] || ! cmp -s "$f" "$target"; then
        cp "$f" "$target" && CHANGED=1
    fi
done

if [ "$CHANGED" -eq 1 ]; then
    systemctl daemon-reload
fi

exit 0
