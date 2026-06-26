#!/usr/bin/env python3
"""
KI-Swing-Trader — Outcome-Tracker (Phase 1, Schatten).

Misst fuer jede geloggte Schatten-Entscheidung die REALISIERTE Forward-Rendite der
Picks (gleichgewichtet) gegen SPY ueber 5/10/20 Handelstage und rechnet daraus den
Information Coefficient (IC) zwischen conviction und Forward-Rendite. KEIN LLM, kostenlos.

Das ist die eigentliche Antwort-Metrik der Phase 1: hat die KI ueberhaupt ein Signal
(IC > 0), bevor wir den Sicherheitskaefig + die echte Anbindung bauen?

Idempotent: misst nur, was reif (genug Handelstage vergangen) und noch nicht gemessen ist.

Aufruf:
    python3 scripts/ai_swing_outcomes.py            # messen + Scoreboard
    python3 scripts/ai_swing_outcomes.py --board    # nur Scoreboard zeigen
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common.data_loader import get_prices       # noqa: E402
from src.common.storage import DATA_DIR, connect    # noqa: E402

AI_DB = DATA_DIR / "ai_swing.db"
HORIZONS = [5, 10, 20]            # Handelstage
CONV_SCORE = {"high": 3, "medium": 2, "low": 1}
REVIEW_AFTER_DECISIONS = 6        # ab so vielen voll (20T) ausgewerteten Wochen-Picks -> Telegram-Wecker


def _ensure_table() -> None:
    with connect(AI_DB) as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS outcomes (
                id           INTEGER PRIMARY KEY,
                decision_id  INTEGER,
                ticker       TEXT,
                horizon_days INTEGER,
                fwd_return   REAL,
                spy_return   REAL,
                excess       REAL,
                measured_at  TEXT DEFAULT (datetime('now')),
                UNIQUE(decision_id, ticker, horizon_days)
            );
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )


def _series_since(ticker: str, run_date: str):
    """Schlusskurse ab run_date (inkl.), aufsteigend. None bei Fehler."""
    try:
        px = get_prices(ticker, period="6mo")
        if px is None or px.empty:
            return None
        s = px["close"].astype(float)
        s = s[s.index >= run_date]
        return s if len(s) else None
    except Exception:
        return None


def measure() -> dict:
    _ensure_table()
    stats = {"checked": 0, "measured": 0, "pending": 0}
    with connect(AI_DB) as c:
        decisions = c.execute("SELECT id, run_date FROM decisions WHERE mode='shadow'").fetchall()
        done = {(r["decision_id"], r["ticker"], r["horizon_days"])
                for r in c.execute("SELECT decision_id,ticker,horizon_days FROM outcomes").fetchall()}

        # SPY-Serien je run_date cachen
        spy_cache: dict[str, object] = {}

        for d in decisions:
            did, run_date = d["id"], d["run_date"]
            picks = c.execute("SELECT ticker, conviction FROM picks WHERE decision_id=?", (did,)).fetchall()
            if run_date not in spy_cache:
                spy_cache[run_date] = _series_since("SPY", run_date)
            spy = spy_cache[run_date]
            for p in picks:
                tkr = p["ticker"]
                ser = _series_since(tkr, run_date)
                for h in HORIZONS:
                    stats["checked"] += 1
                    if (did, tkr, h) in done:
                        continue
                    if ser is None or spy is None or len(ser) <= h or len(spy) <= h:
                        stats["pending"] += 1
                        continue
                    fwd = float(ser.iloc[h]) / float(ser.iloc[0]) - 1.0
                    spyr = float(spy.iloc[h]) / float(spy.iloc[0]) - 1.0
                    c.execute(
                        "INSERT OR IGNORE INTO outcomes "
                        "(decision_id,ticker,horizon_days,fwd_return,spy_return,excess) "
                        "VALUES (?,?,?,?,?,?)",
                        (did, tkr, h, fwd, spyr, fwd - spyr),
                    )
                    stats["measured"] += 1
    return stats


def _spearman(xs, ys) -> float | None:
    if len(xs) < 3:
        return None
    try:
        from scipy.stats import spearmanr
        rho, _ = spearmanr(xs, ys)
        return float(rho) if rho == rho else None  # NaN-Check
    except Exception:
        # manueller Rang-Korr-Fallback
        def ranks(v):
            order = sorted(range(len(v)), key=lambda i: v[i])
            r = [0] * len(v)
            for rank, i in enumerate(order):
                r[i] = rank
            return r
        rx, ry = ranks(xs), ranks(ys)
        n = len(xs)
        d2 = sum((a - b) ** 2 for a, b in zip(rx, ry))
        return 1 - 6 * d2 / (n * (n * n - 1))


def scoreboard() -> None:
    _ensure_table()
    with connect(AI_DB) as c:
        n_dec = c.execute("SELECT COUNT(*) n FROM decisions").fetchone()["n"]
        rows = c.execute(
            "SELECT o.horizon_days h, o.fwd_return f, o.spy_return s, o.excess e, p.conviction conv "
            "FROM outcomes o JOIN picks p ON p.decision_id=o.decision_id AND p.ticker=o.ticker"
        ).fetchall()

    print(f"\n=== KI-Swing Schatten-Scoreboard ===")
    print(f"Entscheidungen geloggt: {n_dec} | gemessene Outcome-Punkte: {len(rows)}")
    if not rows:
        print("Noch nichts reif zum Messen (Picks brauchen 5/10/20 Handelstage). "
              "Das ist normal in den ersten Wochen.")
        return

    for h in HORIZONS:
        sub = [r for r in rows if r["h"] == h]
        if not sub:
            continue
        avg_fwd = sum(r["f"] for r in sub) / len(sub)
        avg_spy = sum(r["s"] for r in sub) / len(sub)
        avg_exc = sum(r["e"] for r in sub) / len(sub)
        hit = sum(1 for r in sub if r["e"] > 0) / len(sub)
        ic = _spearman([CONV_SCORE.get(r["conv"], 2) for r in sub], [r["f"] for r in sub])
        ic_str = f"{ic:+.2f}" if ic is not None else "n/a"
        print(f"  {h:>2}T (n={len(sub):>3}): Pick Ø {avg_fwd*100:+.1f}% | SPY Ø {avg_spy*100:+.1f}% | "
              f"Excess Ø {avg_exc*100:+.1f}% | schlaegt SPY {hit*100:.0f}% | IC(conv→Rendite) {ic_str}")
    print("Lesart: IC>0 = hohe conviction sagt hoehere Rendite voraus (Signal). "
          "Aussagekraeftig erst ab vielen Outcome-Punkten ueber Monate, NICHT jetzt.")


def maybe_notify_review() -> None:
    """Einmaliger Telegram-Wecker, sobald genug Wochen-Picks voll (20T) ausgewertet sind.

    Nicht jede Woche spammen — eine Nachricht, wenn ein erster ehrlicher Zwischen-Check
    moeglich ist (kein Endurteil). Flag in meta verhindert Wiederholung.
    """
    with connect(AI_DB) as c:
        already = c.execute("SELECT value FROM meta WHERE key='review_notified'").fetchone()
        if already:
            return
        n = c.execute(
            "SELECT COUNT(DISTINCT decision_id) n FROM outcomes WHERE horizon_days=20"
        ).fetchone()["n"]
        if n < REVIEW_AFTER_DECISIONS:
            return
        sent = False
        try:
            from src.alerts.notifier import send_info, is_configured
            if is_configured():
                send_info(
                    "🔬 <b>KI-Swing-Trader: Zwischen-Check moeglich</b>\n"
                    f"{n} Wochen-Vorschlaege sind jetzt voll ausgewertet (20 Handelstage) — genug fuer eine "
                    "erste ehrliche Lese, ob ein Signal da ist. <b>Noch kein Endurteil</b> (das braucht Monate).\n"
                    "Sag Claude: „wie steht der KI-Swing-Trader?“ — dann gehen wir die Roadmap weiter oder beerdigen ihn.",
                    label="ai_swing_review",
                )
                sent = True
        except Exception as e:
            print(f"[ai_swing_outcomes] Review-Telegram fehlgeschlagen: {e}")
        if sent:
            c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('review_notified',?)",
                      (dt.date.today().isoformat(),))
            print(f"[ai_swing_outcomes] Review-Ready-Telegram gesendet (n={n}).")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", action="store_true", help="nur Scoreboard, nicht neu messen")
    args = ap.parse_args()
    if not args.board:
        st = measure()
        print(f"[ai_swing_outcomes] geprueft {st['checked']}, gemessen {st['measured']}, ausstehend {st['pending']}")
        maybe_notify_review()
    scoreboard()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
