#!/usr/bin/env python3
"""
dca_benchmark.py — Beweisfuehrung fuer den Monats-Sparplan.

WARUM ES DAS GIBT (Audit 2026-08-01):
Der Sparplan traf seit April monatlich Entscheidungen, aber NIEMAND hat je geprueft,
ob diese Entscheidungen besser waren als die stumpfe Alternative "kauf einfach den
Welt-ETF". Damit war die Leitfrage des Projekts — schlaegt die KI den Markt? — schlicht
unbeantwortbar. Ohne Messung ist jede Empfehlung nur eine Behauptung.

Dieses Modul schreibt bei JEDER Empfehlung den Einstiegskurs des gewaehlten Titels UND
den gleichzeitigen Kurs der Benchmark (VWCE.DE) mit. Danach laesst sich jederzeit exakt
ausrechnen, wie viel besser oder schlechter der Sparplan gegenueber "immer nur VWCE"
gelaufen waere — pro Pick und in Summe.

Wichtig: Bei verdict=buy_etf mit ticker=VWCE.DE ist der Pick MIT der Benchmark identisch;
solche Monate zaehlen als 0,0 Prozentpunkte Differenz. Das ist gewollt und ehrlich —
ein Monat ohne echte Entscheidung darf sich nicht als Erfolg tarnen.
"""

from __future__ import annotations

import datetime as dt
import logging

from .storage import LEARNING_DB, connect

log = logging.getLogger("invest_pi.dca_benchmark")

BENCHMARK_TICKER = "VWCE.DE"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dca_picks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT    NOT NULL,
    prediction_id     INTEGER,
    verdict           TEXT    NOT NULL,   -- buy_single | buy_etf | skip
    ticker            TEXT    NOT NULL,
    entry_price       REAL,               -- Kurs des Picks zum Empfehlungszeitpunkt
    budget_eur        REAL,
    benchmark_ticker  TEXT    NOT NULL,
    benchmark_price   REAL,               -- Kurs der Benchmark zum SELBEN Zeitpunkt
    was_real_choice   INTEGER NOT NULL,   -- 0 = Pick == Benchmark (keine echte Entscheidung)
    note              TEXT
);
CREATE INDEX IF NOT EXISTS idx_dca_picks_created ON dca_picks(created_at);
"""


def _ensure_schema() -> None:
    with connect(LEARNING_DB) as conn:
        conn.executescript(_SCHEMA)


def _last_close(ticker: str) -> float | None:
    try:
        from .data_loader import get_prices
        px = get_prices(ticker, period="1mo")
        if px is not None and len(px) > 0:
            return float(px["close"].iloc[-1])
    except Exception as e:
        log.warning(f"Kurs fuer {ticker} nicht ermittelbar: {e}")
    return None


def record_pick(verdict: str, ticker: str, budget_eur: float,
                entry_price: float | None = None,
                prediction_id: int | None = None,
                note: str = "") -> int | None:
    """Schreibt einen Sparplan-Pick samt gleichzeitigem Benchmark-Kurs mit.
    Returns die Zeilen-ID oder None bei Fehler (nie werfend — der Sparplan
    darf an der Buchfuehrung nicht scheitern)."""
    try:
        _ensure_schema()
        ticker = ticker.upper()
        if entry_price is None:
            entry_price = _last_close(ticker)
        bench_price = (entry_price if ticker == BENCHMARK_TICKER
                       else _last_close(BENCHMARK_TICKER))
        with connect(LEARNING_DB) as conn:
            cur = conn.execute(
                "INSERT INTO dca_picks (created_at, prediction_id, verdict, ticker, "
                "entry_price, budget_eur, benchmark_ticker, benchmark_price, "
                "was_real_choice, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                 prediction_id, verdict, ticker, entry_price, budget_eur,
                 BENCHMARK_TICKER, bench_price,
                 0 if ticker == BENCHMARK_TICKER else 1, note),
            )
            return cur.lastrowid
    except Exception as e:
        log.error(f"dca_picks-Eintrag fehlgeschlagen: {e}")
        return None


def evaluate() -> dict:
    """Rechnet jeden vergangenen Pick gegen die Benchmark aus.

    Returns dict mit `picks` (je Pick: Rendite Pick, Rendite Benchmark, Differenz in
    Prozentpunkten) und `summary` (Anzahl echter Entscheidungen, Trefferquote,
    durchschnittliche und aufsummierte Differenz, Euro-Effekt).
    """
    _ensure_schema()
    with connect(LEARNING_DB) as conn:
        rows = conn.execute(
            "SELECT * FROM dca_picks WHERE verdict != 'skip' ORDER BY created_at"
        ).fetchall()

    picks, price_cache = [], {}

    def now_price(tk: str) -> float | None:
        if tk not in price_cache:
            price_cache[tk] = _last_close(tk)
        return price_cache[tk]

    for r in rows:
        cur_p, cur_b = now_price(r["ticker"]), now_price(r["benchmark_ticker"])
        if not (r["entry_price"] and r["benchmark_price"] and cur_p and cur_b):
            continue
        ret_pick = cur_p / r["entry_price"] - 1
        ret_bench = cur_b / r["benchmark_price"] - 1
        picks.append({
            "date":            (r["created_at"] or "")[:10],
            "ticker":          r["ticker"],
            "verdict":         r["verdict"],
            "was_real_choice": bool(r["was_real_choice"]),
            "budget_eur":      r["budget_eur"],
            "ret_pick":        round(ret_pick, 4),
            "ret_benchmark":   round(ret_bench, 4),
            "diff_pp":         round((ret_pick - ret_bench) * 100, 2),
            "diff_eur":        round((ret_pick - ret_bench) * (r["budget_eur"] or 0), 2),
        })

    real = [p for p in picks if p["was_real_choice"]]
    wins = [p for p in real if p["diff_pp"] > 0]
    summary = {
        "picks_total":      len(picks),
        "picks_real":       len(real),
        "picks_benchmark":  len(picks) - len(real),
        "wins":             len(wins),
        "hit_rate":         round(len(wins) / len(real), 3) if real else None,
        "avg_diff_pp":      round(sum(p["diff_pp"] for p in real) / len(real), 2) if real else None,
        "total_diff_eur":   round(sum(p["diff_eur"] for p in picks), 2),
        "invested_eur":     round(sum(p["budget_eur"] or 0 for p in picks), 2),
    }
    return {"picks": picks, "summary": summary}


def summary_line() -> str:
    """Einzeiler fuer Telegram/Report. Leer wenn es noch nichts zu messen gibt."""
    try:
        ev = evaluate()
    except Exception as e:
        log.warning(f"Benchmark-Auswertung fehlgeschlagen: {e}")
        return ""
    s = ev["summary"]
    if not s["picks_total"]:
        return ""
    if not s["picks_real"]:
        return (f"Bisher {s['picks_total']} Sparplan-Kaeufe — alle im Welt-ETF, "
                f"also noch keine echte KI-Entscheidung zu messen.")
    return (f"Bilanz gegen 'immer nur {BENCHMARK_TICKER}': {s['picks_real']} echte "
            f"Entscheidungen, davon {s['wins']} besser "
            f"({s['hit_rate']:.0%}), im Schnitt {s['avg_diff_pp']:+.1f} Prozentpunkte, "
            f"unterm Strich {s['total_diff_eur']:+.2f} EUR.")


if __name__ == "__main__":
    ev = evaluate()
    print(f"{'Datum':<12}{'Ticker':<10}{'echt?':<7}{'Pick':>9}{'Bench':>9}{'Diff':>9}{'EUR':>9}")
    print("-" * 65)
    for p in ev["picks"]:
        print(f"{p['date']:<12}{p['ticker']:<10}{'ja' if p['was_real_choice'] else 'nein':<7}"
              f"{p['ret_pick']:>+8.1%}{p['ret_benchmark']:>+9.1%}"
              f"{p['diff_pp']:>+8.1f}pp{p['diff_eur']:>+8.2f}")
    print()
    s = ev["summary"]
    print(f"Kaeufe gesamt: {s['picks_total']}  |  davon echte Entscheidungen: {s['picks_real']}"
          f"  |  reine Benchmark-Kaeufe: {s['picks_benchmark']}")
    if s["picks_real"]:
        print(f"Trefferquote: {s['wins']}/{s['picks_real']} ({s['hit_rate']:.0%})  |  "
              f"Schnitt: {s['avg_diff_pp']:+.1f}pp")
    print(f"Euro-Effekt gegenueber 'immer nur {BENCHMARK_TICKER}': {s['total_diff_eur']:+.2f} EUR "
          f"auf {s['invested_eur']:.0f} EUR investiert")
