#!/usr/bin/env python3
"""
thesis_store.py — Anlage-These je Depot-Position.

WARUM ES DAS GIBT:
Der Sparplan kaufte bisher Titel, ohne festzuhalten WARUM — und konnte deshalb spaeter
nie pruefen, ob der Grund noch gilt. Damit war jede Verkaufsentscheidung entweder reine
Kursreaktion ("faellt gerade") oder gar nicht moeglich. Genau das unterscheidet einen
Portfolio-Manager von einem Sparplan: Er weiss, warum er etwas haelt, und woran er
merkt, dass er sich geirrt hat.

Jede Position bekommt beim Kauf:
  thesis       — warum dieser Titel, langfristig gedacht (kein Kursziel)
  invalidation — was konkret eintreten muesste, damit die These widerlegt ist

Beides schreibt die KI beim Kauf, und beides wird ihr jeden Monat wieder vorgelegt.
Die Invalidierung ist der Hebel: sie zwingt zu einer pruefbaren Aussage VOR dem Kauf,
statt hinterher eine Begruendung zu basteln, warum man doch noch haelt.
"""

from __future__ import annotations

import datetime as dt
import logging

from .storage import LEARNING_DB, connect

log = logging.getLogger("invest_pi.thesis_store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS position_thesis (
    ticker        TEXT PRIMARY KEY,
    thesis        TEXT NOT NULL,
    invalidation  TEXT NOT NULL,
    opened_at     TEXT NOT NULL,
    last_review   TEXT,
    review_count  INTEGER NOT NULL DEFAULT 0,
    last_verdict  TEXT,          -- gilt | wackelt | gebrochen
    last_note     TEXT,
    closed_at     TEXT,          -- gesetzt = Position verkauft, Historie bleibt
    close_reason  TEXT
);
"""


def _ensure() -> None:
    with connect(LEARNING_DB) as conn:
        conn.executescript(_SCHEMA)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def open_thesis(ticker: str, thesis: str, invalidation: str) -> None:
    """Legt die These beim Kauf an. Existiert sie schon (Nachkauf), bleibt das
    Eroeffnungsdatum stehen und nur der Text wird aktualisiert."""
    _ensure()
    ticker = ticker.upper()
    try:
        with connect(LEARNING_DB) as conn:
            row = conn.execute(
                "SELECT ticker FROM position_thesis WHERE ticker=? AND closed_at IS NULL",
                (ticker,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE position_thesis SET thesis=?, invalidation=? WHERE ticker=?",
                    (thesis, invalidation, ticker))
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO position_thesis "
                    "(ticker, thesis, invalidation, opened_at, review_count) VALUES (?,?,?,?,0)",
                    (ticker, thesis, invalidation, _now()))
    except Exception as e:
        log.error(f"These fuer {ticker} nicht speicherbar: {e}")


def review(ticker: str, verdict: str, note: str = "") -> None:
    """Haelt das monatliche Urteil fest: gilt | wackelt | gebrochen."""
    _ensure()
    try:
        with connect(LEARNING_DB) as conn:
            conn.execute(
                "UPDATE position_thesis SET last_review=?, review_count=review_count+1, "
                "last_verdict=?, last_note=? WHERE ticker=? AND closed_at IS NULL",
                (_now(), verdict, note, ticker.upper()))
    except Exception as e:
        log.error(f"Review fuer {ticker} nicht speicherbar: {e}")


def close_thesis(ticker: str, reason: str) -> None:
    """Schliesst die These beim Verkauf — Eintrag bleibt als Historie erhalten."""
    _ensure()
    try:
        with connect(LEARNING_DB) as conn:
            conn.execute(
                "UPDATE position_thesis SET closed_at=?, close_reason=? "
                "WHERE ticker=? AND closed_at IS NULL",
                (_now(), reason, ticker.upper()))
    except Exception as e:
        log.error(f"These fuer {ticker} nicht schliessbar: {e}")


def open_theses() -> dict[str, dict]:
    """Alle offenen Thesen als {ticker: {...}}."""
    _ensure()
    try:
        with connect(LEARNING_DB) as conn:
            rows = conn.execute(
                "SELECT * FROM position_thesis WHERE closed_at IS NULL").fetchall()
    except Exception as e:
        log.error(f"Thesen nicht ladbar: {e}")
        return {}
    return {r["ticker"]: {
        "thesis":       r["thesis"],
        "invalidation": r["invalidation"],
        "opened_at":    (r["opened_at"] or "")[:10],
        "last_review":  (r["last_review"] or "")[:10],
        "review_count": r["review_count"],
        "last_verdict": r["last_verdict"],
    } for r in rows}


def history() -> list[dict]:
    """Geschlossene Thesen — was wurde warum gekauft und warum wieder verkauft."""
    _ensure()
    try:
        with connect(LEARNING_DB) as conn:
            rows = conn.execute(
                "SELECT * FROM position_thesis WHERE closed_at IS NOT NULL "
                "ORDER BY closed_at DESC").fetchall()
    except Exception:
        return []
    return [{"ticker": r["ticker"], "thesis": r["thesis"],
             "opened_at": (r["opened_at"] or "")[:10],
             "closed_at": (r["closed_at"] or "")[:10],
             "close_reason": r["close_reason"]} for r in rows]
