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

# ────────────────────────────────────────────────────────────
# Modus 2: einen Dauerlaeufer neu starten (2026-07-27)
#
# Hintergrund: auto_pull startet nach einem Deploy nichts neu — bei den
# oneshot-Timern stimmt das auch, die holen sich den neuen Code beim naechsten
# Lauf. Die Webapp laeuft aber durch und behielt ihren alten Code, teils ueber
# Wochen. Jede Webapp-Aenderung brauchte bisher einen manuellen Neustart.
#
# Sicherheit: NUR die fest verdrahtete Liste unten ist erlaubt. Dieses Skript
# laeuft via NOPASSWD-sudoers als root, und sein Argument kommt aus einem Repo,
# das automatisch von GitHub zieht — ein freier Unit-Name waere hier faktisch
# eine Root-Shell fuer jeden mit Schreibrechten aufs Repo.
# ────────────────────────────────────────────────────────────
RESTARTABLE="invest-pi-webapp invest-pi-terminal"

# ────────────────────────────────────────────────────────────
# Modus 3: einen NEUEN Timer scharf schalten (2026-08-09)
#
# Hintergrund: der File-Sync oben kopiert neue Units und macht daemon-reload,
# aktiviert sie aber nicht — ein frisch ausgerollter Timer liegt danach als
# "disabled/inactive" da und feuert nie. Das faellt erst auf, wenn die erwartete
# Meldung ausbleibt, also potenziell Monate spaeter (beim Abschaltregel-Waechter
# waere genau das der Fall gewesen).
#
# Bewusst NICHT automatisch fuer alle Units: mehrere Timer sind absichtlich
# deaktiviert (z.B. invest-pi-score als Referenz der stillgelegten Score-Aera).
# Ein Auto-Enable haette die wieder scharf gemacht.
#
# Sicherheit: gleiche Regel wie bei --restart — nur die fest verdrahtete Liste.
# Das Argument kommt aus einem Repo, das automatisch von GitHub zieht.
# ────────────────────────────────────────────────────────────
ENABLEABLE="invest-pi-abschalt-check.timer"

if [ "${1:-}" = "--enable" ]; then
    unit="${2:-}"
    case " $ENABLEABLE " in
        *" $unit "*)
            systemctl enable --now "$unit"
            exit $?
            ;;
        *)
            echo "systemd_sync: '$unit' steht nicht auf der Scharfschalt-Liste" >&2
            exit 1
            ;;
    esac
fi

# ────────────────────────────────────────────────────────────
# Modus 4: einen Timer stilllegen (2026-08-10)
#
# Hintergrund: Der Depot-/Sparplan-Teil ist an DepotPi uebergeben (siehe
# src/common/depot_stillgelegt.py). Abschalten heisst `systemctl disable --now`
# und braucht root — dieses Repo laeuft als `investpi` und darf per sudo nur
# dieses Wrapper-Skript aufrufen. Ohne diesen Modus haette Mert die Befehle von
# Hand tippen muessen, und ein vergessener Timer feuert still weiter.
#
# Sicherheit: gleiche Regel wie oben — nur die fest verdrahtete Liste. Bewusst
# eng gehalten: es duerfen NUR die beiden uebergebenen Depot-Timer abgeschaltet
# werden, kein freier Unit-Name (das Argument kommt aus einem Repo, das
# automatisch von GitHub zieht).
# ────────────────────────────────────────────────────────────
DISABLEABLE="invest-pi-monthly-dca.timer invest-pi-dca-watchdog.timer"

if [ "${1:-}" = "--disable" ]; then
    unit="${2:-}"
    case " $DISABLEABLE " in
        *" $unit "*)
            systemctl disable --now "$unit"
            exit $?
            ;;
        *)
            echo "systemd_sync: '$unit' steht nicht auf der Stilllege-Liste" >&2
            exit 1
            ;;
    esac
fi

if [ "${1:-}" = "--restart" ]; then
    unit="${2:-}"
    case " $RESTARTABLE " in
        *" $unit "*)
            systemctl restart "$unit"
            exit $?
            ;;
        *)
            echo "systemd_sync: '$unit' steht nicht auf der Neustart-Liste" >&2
            exit 1
            ;;
    esac
fi

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
