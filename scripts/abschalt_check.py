#!/usr/bin/env python3
"""
abschalt_check.py — Waechter fuer die Abschaltregel.

Die Regel (Entscheidung 2026-08-09, im Manifest festgehalten): Liegt eine
Strategie am Stichtag mehr als GRENZE_PUNKTE hinter dem SPY, wird sie
ABGESCHALTET statt weiter optimiert. Vorher festgelegt, damit das Ergebnis
nicht nachtraeglich schoengeredet wird.

Dieses Script laeuft taeglich und ist bis zum Stichtag komplett still. Ab dem
Stichtag meldet es sich per Telegram — und zwar so lange wieder (woechentlich),
bis Mert die Entscheidung quittiert hat. Ein Merksatz, der einmal piept und
dann nie wieder, wird uebersehen; genau das soll hier nicht passieren.

Quittieren:  python3 scripts/abschalt_check.py --erledigt
Probelauf:   python3 scripts/abschalt_check.py --testlauf   (meldet sofort, ohne Merker)

Stichtag und Grenze kommen aus manifest.yaml (einzige Wahrheitsquelle) — wird
die Frist dort geaendert, zieht dieses Script automatisch mit.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.alerts.notifier import send_action_required
from src.common.performance import compute_metrics

ROOT = Path(__file__).resolve().parents[1]
MERKER = ROOT / "data" / "abschalt_check_state.txt"
AUFGABEN_TITEL = "Stichtag Abschaltregel"
GRENZE_PUNKTE = 5.0          # mehr Rueckstand als das -> abschalten
ERINNERUNG_TAGE = 7          # danach woechentlich erneut, bis quittiert

STRATEGIEN = [
    ("paper",    "Momentum",  "2026-04-29"),
    ("ai_swing", "KI-Swing",  "2026-07-02"),
]


def stichtag() -> dt.date | None:
    """Faelligkeit der Abschalt-Aufgabe aus manifest.yaml. None wenn nicht auffindbar."""
    try:
        import yaml
        m = yaml.safe_load((ROOT / "manifest.yaml").read_text())
        for t in m.get("tasks") or []:
            if str(t.get("title", "")).startswith(AUFGABEN_TITEL):
                f = t.get("faellig")
                return dt.date.fromisoformat(str(f)) if f else None
    except Exception as exc:  # noqa: BLE001 — Waechter darf nie hart scheitern
        print(f"  WARN: Stichtag nicht aus manifest.yaml lesbar ({exc})")
    return None


def _merker_lesen() -> tuple[bool, dt.date | None]:
    """(quittiert, zuletzt_gemeldet)"""
    if not MERKER.exists():
        return False, None
    quittiert, zuletzt = False, None
    for zeile in MERKER.read_text().splitlines():
        if zeile.strip() == "quittiert":
            quittiert = True
        elif zeile.startswith("gemeldet="):
            try:
                zuletzt = dt.date.fromisoformat(zeile.split("=", 1)[1].strip())
            except ValueError:
                pass
    return quittiert, zuletzt


def _merker_schreiben(*, quittiert: bool, gemeldet: dt.date | None) -> None:
    MERKER.parent.mkdir(parents=True, exist_ok=True)
    zeilen = []
    if quittiert:
        zeilen.append("quittiert")
    if gemeldet:
        zeilen.append(f"gemeldet={gemeldet.isoformat()}")
    MERKER.write_text("\n".join(zeilen) + "\n")


def bilanz() -> tuple[list[str], list[str]]:
    """(Textzeilen je Strategie, Namen der Strategien die durchgefallen sind)."""
    zeilen, durchgefallen = [], []
    heute = dt.date.today()
    for quelle, name, start in STRATEGIEN:
        tage = (heute - dt.date.fromisoformat(start)).days + 5
        m = compute_metrics(source=quelle, days=tage)
        if m.alpha_pct is None:
            zeilen.append(f"• <b>{name}</b>: keine Vergleichsdaten — bitte nachsehen")
            continue
        punkte = m.alpha_pct * 100
        raus = punkte < -GRENZE_PUNKTE
        if raus:
            durchgefallen.append(name)
        zeilen.append(
            f"• <b>{name}</b>: {m.total_return_pct*100:+.1f}% gegen Markt "
            f"{m.spy_return_pct*100:+.1f}% → <b>{punkte:+.1f} Punkte</b> "
            f"{'❌ abschalten' if raus else '✅ bleibt'}"
        )
    return zeilen, durchgefallen


def melde(ziel: dt.date, *, test: bool = False) -> bool:
    zeilen, durchgefallen = bilanz()
    vorspann = ("🧪 <b>TESTLAUF</b> — so sieht die Meldung am Stichtag aus. "
                "Der echte Termin ist der 31.10.2026, heute ist nichts zu tun.\n\n") if test else ""
    kopf = (vorspann
            + f"⏰ <b>Stichtag Abschaltregel erreicht</b> ({ziel.strftime('%d.%m.%Y')})\n\n"
            f"Am 09.08.2026 wurde festgelegt: Wer heute mehr als {GRENZE_PUNKTE:.0f} Punkte "
            f"hinter dem Markt liegt, wird <b>abgeschaltet statt weiter optimiert</b>.\n\n")
    body = "\n".join(zeilen)
    if durchgefallen:
        fuss = ("\n\n<b>Fällig ist jetzt eine Entscheidung von dir:</b> "
                + ", ".join(durchgefallen) + " abschalten — oder die Regel bewusst brechen "
                "und begründen, warum es diesmal anders ist.\n\n"
                "Diese Meldung kommt wöchentlich wieder, bis du entschieden hast.")
    else:
        fuss = ("\n\nAlle Strategien haben den Test bestanden. Neuen Stichtag setzen, "
                "damit die Regel scharf bleibt.")
    return send_action_required(kopf + body + fuss, label="abschaltregel")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--erledigt", action="store_true", help="Entscheidung quittieren, Meldungen einstellen")
    ap.add_argument("--testlauf", action="store_true", help="sofort melden (ignoriert Stichtag + Merker)")
    args = ap.parse_args()

    if args.erledigt:
        _merker_schreiben(quittiert=True, gemeldet=dt.date.today())
        print("  Abschalt-Entscheidung quittiert — keine weiteren Erinnerungen.")
        return

    ziel = stichtag()
    if args.testlauf:
        print(f"  Testlauf — Meldung wird verschickt: {melde(ziel or dt.date.today(), test=True)}")
        return

    if ziel is None:
        print("  Kein Stichtag im Manifest gefunden — nichts zu tun.")
        return

    heute = dt.date.today()
    if heute < ziel:
        print(f"  Stichtag {ziel} noch nicht erreicht (noch {(ziel - heute).days} Tage) — still.")
        return

    quittiert, zuletzt = _merker_lesen()
    if quittiert:
        print("  Bereits quittiert — still.")
        return
    if zuletzt and (heute - zuletzt).days < ERINNERUNG_TAGE:
        print(f"  Zuletzt am {zuletzt} gemeldet — naechste Erinnerung in "
              f"{ERINNERUNG_TAGE - (heute - zuletzt).days} Tagen.")
        return

    if melde(ziel):
        _merker_schreiben(quittiert=False, gemeldet=heute)
        print(f"  Stichtag erreicht — Meldung an Mert verschickt.")
    else:
        print("  Stichtag erreicht, aber Telegram-Versand fehlgeschlagen — nicht gemerkt, "
              "naechster Lauf versucht es erneut.")


if __name__ == "__main__":
    main()
