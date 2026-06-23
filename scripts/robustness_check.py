#!/usr/bin/env python3
"""
robustness_check.py — Ist der Momentum-Edge VERLÄSSLICH oder nur ein Schnitt?

Nimmt die Sieger aus champion_duell_fair (Momentum Top-5/Top-3, faires 2018er-
Universum) und prüft, was der Jahres-Schnitt verschweigt:
  1. Jahr für Jahr: schlägt Momentum den Markt in JEDEM Jahr — oder hängt alles
     an 2-3 Mega-Jahren?
  2. Schlimmster Drawdown im Detail (wie tief, wann, wie lange bis Erholung) —
     der echte Schmerz-Test für Echtgeld.
  3. Rollierende 12-Monats-Fenster: in wie viel % der Zeit schlägt es den Markt?
"""
from __future__ import annotations
import sys, warnings
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.filterwarnings("ignore")

import pandas as pd
from scripts.champion_duell_fair import load_all, month_ends, run, s_momentum, s_hold


def yearly(curve):
    """Jahresrenditen aus monatlicher equity-Kurve."""
    df = curve.copy(); df.index = pd.to_datetime(df.index)
    out = {}
    for yr in sorted({d.year for d in df.index}):
        seg = df[df.index.year == yr]
        if len(seg) < 2: continue
        # Anfangswert = letzter Wert des Vorjahres (oder erster im Jahr)
        prev = df[df.index.year == yr - 1]
        start = prev.iloc[-1] if len(prev) else seg.iloc[0]
        out[yr] = seg.iloc[-1] / start - 1
    return out


def max_drawdown_detail(curve):
    c = curve.copy(); c.index = pd.to_datetime(c.index)
    run_max = c.cummax()
    dd = (c - run_max) / run_max
    trough = dd.idxmin(); depth = dd.min()
    peak = c[:trough].idxmax()
    after = c[trough:]
    rec = after[after >= c.loc[peak]]
    recovered = rec.index[0] if len(rec) else None
    months = (recovered.to_period("M") - trough.to_period("M")).n if recovered else None
    return depth, peak, trough, recovered, months


def main():
    px = load_all()
    avail = [k for k in px.columns]
    rebals = [d for d in month_ends(px.index) if d in px.index]

    strategies = {"Momentum Top-5": s_momentum(5), "Momentum Top-3": s_momentum(3), "SPY (Markt)": s_hold("SPY")}
    curves = {name: run(st, px, rebals, avail) for name, st in strategies.items()}

    spy_y = yearly(curves["SPY (Markt)"])
    print("=== 1. JAHR FÜR JAHR: Momentum Top-5 vs Markt ===\n")
    print(f"{'Jahr':>6} | {'Mom Top-5':>10} | {'Markt SPY':>10} | {'Diff':>8} | Sieger")
    print("-" * 56)
    m5_y = yearly(curves["Momentum Top-5"])
    wins = 0; total = 0
    for yr in sorted(spy_y):
        if yr not in m5_y: continue
        m, s = m5_y[yr], spy_y[yr]; diff = (m - s) * 100; total += 1
        win = m > s; wins += win
        print(f"{yr:>6} | {m*100:>+9.1f}% | {s*100:>+9.1f}% | {diff:>+7.1f}% | {'Momentum ✓' if win else 'Markt ✗'}")
    print("-" * 56)
    print(f"Momentum schlug den Markt in {wins} von {total} Jahren ({100*wins/total:.0f}%)\n")

    print("=== 2. SCHLIMMSTER DRAWDOWN (Schmerz-Test) ===\n")
    for name in ("Momentum Top-5", "Momentum Top-3", "SPY (Markt)"):
        depth, peak, trough, rec, months = max_drawdown_detail(curves[name])
        recs = f"erholt nach {months} Mon. ({str(rec)[:7]})" if rec else "NOCH NICHT erholt"
        print(f"  {name:>16}: {depth*100:>+5.0f}%  ({str(peak)[:7]} -> {str(trough)[:7]}, {recs})")

    print("\n=== 3. ROLLIERENDE 12-MONATS-FENSTER ===\n")
    m5 = curves["Momentum Top-5"]; spy = curves["SPY (Markt)"]
    idx = [d for d in m5.index if d in spy.index]
    beat = 0; tot = 0
    worst = (None, 99)
    for i in range(len(idx) - 12):
        a, b = idx[i], idx[i + 12]
        mr = m5.loc[b] / m5.loc[a] - 1; sr = spy.loc[b] / spy.loc[a] - 1
        tot += 1; beat += mr > sr
        if (mr - sr) < worst[1]: worst = (str(a)[:7], mr - sr)
    print(f"  Momentum schlug den Markt in {beat}/{tot} der 12-Monats-Fenster ({100*beat/tot:.0f}%)")
    print(f"  Schlimmstes 12M-Fenster für Momentum: {worst[1]*100:+.0f}% vs Markt (ab {worst[0]})")


if __name__ == "__main__":
    main()
