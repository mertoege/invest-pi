#!/usr/bin/env python3
"""
pm_rules.py — Nicht verhandelbare Leitplanken des Portfolio-Managers.

WARUM ES DAS GIBT:
Die KI schlaegt vor, der Code entscheidet ueber das Erlaubte. Alles, was schiefgehen
kann, wenn ein Sprachmodell einen schlechten Tag hat, wird hier hart abgefangen —
NICHT im Prompt. Ein Prompt ist eine Bitte, dieser Code ist eine Grenze.

Die Regeln kommen aus Merts Vorgaben (2026-08-01) und aus einer Randbedingung, die
alles andere dominiert:

REVOLUT: EIN GRATIS-HANDEL PRO MONAT.
Bei 50 EUR Monatsrate kostet jeder weitere Handel je nach Tarif rund 1 EUR oder
0,25 Prozent — auf 50 EUR sind das bis zu 2 Prozent Reibung. Aktives Umschichten ist
damit arithmetisch tot: Verkaufen UND Kaufen im selben Monat frisst den Vorteil auf,
bevor er entsteht. Der Portfolio-Manager trifft deshalb genau EINE Entscheidung pro
Monat. Ein Verkauf kostet den Monatshandel — er muss sich also gegen "einen Monat lang
investieren" rechnen, nicht nur gegen "die Position ist gerade unschoen".

KONZENTRATION (Mert, 2026-08-01): max 25 Prozent je EINZELAKTIE, mindestens 4 Positionen.
Breite ETFs sind von der 25-Prozent-Grenze ausgenommen — ein Welt-ETF ist in sich schon
gestreut; ihn zu deckeln wuerde ausgerechnet aus dem sichersten Baustein draengen.
"""

from __future__ import annotations

import datetime as dt
import logging

from .market_scan import REVOLUT_ETFS
from .storage import LEARNING_DB, connect
from .universe import UNIVERSE

log = logging.getLogger("invest_pi.pm_rules")

MAX_SINGLE_STOCK_PCT = 0.25    # je Einzelaktie
MIN_POSITIONS_TARGET = 4       # Ziel-Streuung
MIN_POSITIONS_FLOOR = 3        # darunter wird ein Verkauf hart blockiert
TRADES_PER_MONTH = 1           # Revolut: ein Gratis-Handel

BROAD_ETFS = set(REVOLUT_ETFS)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pm_actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    month       TEXT NOT NULL,          -- YYYY-MM
    created_at  TEXT NOT NULL,
    action      TEXT NOT NULL,          -- buy | sell | wait
    ticker      TEXT,
    eur         REAL,
    reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_pm_actions_month ON pm_actions(month);
"""


def _ensure() -> None:
    with connect(LEARNING_DB) as conn:
        conn.executescript(_SCHEMA)


def this_month() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")


def is_broad_etf(ticker: str) -> bool:
    return ticker.upper() in BROAD_ETFS


def tradeable(ticker: str, cfg) -> bool:
    """Auf Revolut in EUR kaufbar: die drei UCITS-ETFs, die Large Caps des
    Momentum-Universums, plus was Mert ohnehin schon haelt."""
    t = ticker.upper()
    return (t in BROAD_ETFS
            or t in {u.upper() for u in UNIVERSE}
            or t in {k.upper() for k in cfg.portfolio})


def weights(cfg) -> dict[str, float]:
    """Aktuelle Depot-Gewichte als Anteil (0..1), nach eingesetztem Kapital."""
    total = sum(p.invested_eur or 0 for p in cfg.portfolio.values())
    if total <= 0:
        return {}
    return {t: (p.invested_eur or 0) / total for t, p in cfg.portfolio.items()}


def trade_used_this_month() -> bool:
    """Wurde der Gratis-Handel diesen Monat schon verbraucht?"""
    _ensure()
    try:
        with connect(LEARNING_DB) as conn:
            row = conn.execute(
                "SELECT COUNT(*) c FROM pm_actions WHERE month=? AND action IN ('buy','sell')",
                (this_month(),)).fetchone()
        return (row["c"] or 0) >= TRADES_PER_MONTH
    except Exception as e:
        log.error(f"pm_actions nicht lesbar: {e}")
        return False


def record_action(action: str, ticker: str | None, eur: float | None, reason: str) -> None:
    _ensure()
    try:
        with connect(LEARNING_DB) as conn:
            conn.execute(
                "INSERT INTO pm_actions (month, created_at, action, ticker, eur, reason) "
                "VALUES (?,?,?,?,?,?)",
                (this_month(),
                 dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                 action, (ticker or "").upper() or None, eur, (reason or "")[:500]))
    except Exception as e:
        log.error(f"pm_action nicht speicherbar: {e}")


def validate(decision: dict, cfg, budget_eur: float) -> tuple[list[str], list[str]]:
    """Prueft eine Entscheidung gegen die Leitplanken.

    Returns (blocks, warnings). Ist `blocks` nicht leer, wird NICHT ausgefuehrt —
    stattdessen faellt der Manager auf 'wait' zurueck (Geld bleibt liegen, der
    Gratis-Handel bleibt unverbraucht). Lieber ein Monat ohne Handel als ein Handel,
    der eine Grenze reisst.
    """
    blocks: list[str] = []
    warns: list[str] = []

    action = (decision.get("action") or "").lower()
    ticker = (decision.get("ticker") or "").upper()

    if action not in ("buy", "sell", "wait"):
        blocks.append(f"Unbekannte Aktion '{action}'")
        return blocks, warns

    if action == "wait":
        return blocks, warns

    if not ticker:
        blocks.append("Kein Ticker angegeben")
        return blocks, warns

    if trade_used_this_month():
        blocks.append("Der Gratis-Handel dieses Monats ist bereits verbraucht")
        return blocks, warns

    positions = {t.upper(): (p.invested_eur or 0) for t, p in cfg.portfolio.items()}
    total = sum(positions.values())

    if action == "buy":
        if not tradeable(ticker, cfg):
            blocks.append(f"{ticker} ist nicht als auf Revolut handelbar gefuehrt")
        eur = float(decision.get("eur") or budget_eur)
        if eur <= 0:
            blocks.append(f"Kaufbetrag {eur} ist nicht plausibel")
        if eur > budget_eur + 0.01:
            blocks.append(f"Kaufbetrag {eur:.2f} EUR ueber dem Budget von {budget_eur:.2f} EUR")
        # Einzelaktien-Grenze — breite ETFs ausgenommen
        if not is_broad_etf(ticker):
            after = positions.get(ticker, 0) + eur
            total_after = total + eur
            pct = after / total_after if total_after else 1.0
            if pct > MAX_SINGLE_STOCK_PCT + 1e-9:
                blocks.append(
                    f"{ticker} laege danach bei {pct:.0%} des Depots — "
                    f"Grenze fuer Einzelaktien ist {MAX_SINGLE_STOCK_PCT:.0%}")
            elif pct > MAX_SINGLE_STOCK_PCT * 0.85:
                warns.append(f"{ticker} naehert sich der Grenze: {pct:.0%}")
        if len(positions) < MIN_POSITIONS_TARGET and ticker in positions:
            warns.append(
                f"Depot hat erst {len(positions)} Positionen (Ziel {MIN_POSITIONS_TARGET}) — "
                f"ein neuer Titel waere der Streuung dienlicher als ein Nachkauf")

    elif action == "sell":
        if ticker not in positions:
            blocks.append(f"{ticker} ist nicht im Depot")
        elif len(positions) - 1 < MIN_POSITIONS_FLOOR:
            blocks.append(
                f"Verkauf wuerde das Depot auf {len(positions) - 1} Positionen druecken — "
                f"Untergrenze ist {MIN_POSITIONS_FLOOR}")
        elif len(positions) - 1 < MIN_POSITIONS_TARGET:
            warns.append(
                f"Nach dem Verkauf nur noch {len(positions) - 1} Positionen "
                f"(Ziel {MIN_POSITIONS_TARGET}) — naechster Kauf sollte ein neuer Titel sein")
        if not blocks:
            warns.append(
                "Der Verkauf verbraucht den Gratis-Handel — diesen Monat wird nicht gekauft")

    return blocks, warns
