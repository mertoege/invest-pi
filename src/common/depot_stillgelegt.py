"""
depot_stillgelegt.py — Schalter fuer den stillgelegten Depot-/Sparplan-Teil.

STAND 2026-08-10: Der Depot-Teil von invest-pi ist ABGESCHALTET. DepotPi fuehrt
Merts echtes Revolut-Depot ab jetzt allein.

WARUM:
Mert bespart seit dem 10.08.2026 zwei provisionsfreie Revolut-ETF-Sparplaene
(80 EUR VWCE + 20 EUR AHYD). Damit faellt das alte Gegenargument weg ("nicht
abschalten, sonst faellt ein Monat aus") — der Monatsbeitrag laeuft ohnehin.
Der Portfolio-Manager wuerde am 1.9. eine EINZELAKTIE empfehlen und damit gegen
die neue Anlagepolitik arbeiten.

WARUM DIESER SCHALTER UND NICHT NUR `systemctl disable`:
Das Abschalten der Timer braucht root; dieses Repo laeuft als `investpi` und darf
per sudo nur `systemd_sync.sh` aufrufen. Der Schalter hier wirkt unabhaengig davon
— selbst wenn ein Timer noch scharf ist oder spaeter versehentlich wieder scharf
gemacht wird, passiert nichts ausser einer Log-Zeile. Guertel und Hosentraeger.

Der Code bleibt bewusst liegen (Referenz fuer DepotPi), er laeuft nur nicht mehr.
Zum Reaktivieren: INVEST_PI_DEPOT_AKTIV=1 setzen — bewusst als Umgebungsvariable
und nicht als config-Schalter, damit es eine bewusste Handlung bleibt.
"""

from __future__ import annotations

import os

STILLGELEGT_SEIT = "2026-08-10"

HINWEIS = (
    f"Depot-/Sparplan-Teil stillgelegt seit {STILLGELEGT_SEIT} — DepotPi fuehrt das "
    "echte Revolut-Depot. Merts Monatsbeitrag laeuft ueber zwei provisionsfreie "
    "Revolut-ETF-Sparplaene (80 EUR VWCE + 20 EUR AHYD); Einzelaktien-Empfehlungen "
    "widersprechen der Anlagepolitik. Nichts getan. "
    "(Reaktivieren nur bewusst: INVEST_PI_DEPOT_AKTIV=1)"
)


def ist_stillgelegt() -> bool:
    """True = der Depot-Teil darf NICHT laufen (Normalfall seit 2026-08-10)."""
    return os.environ.get("INVEST_PI_DEPOT_AKTIV", "").strip() not in ("1", "true", "yes")
