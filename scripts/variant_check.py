#!/usr/bin/env python3
"""
variant_check.py — Gibt es eine BESSERE Momentum-Variante? (ehrlich, overfitting-bewusst)

Testet nur theorie-gestuetzte Varianten gegen den Live-Stand (Mom 6M, Top-5, monatlich):
  A) 12-1-Momentum   — akademischer Standard (12 Mon. Lookback, letzten Monat weglassen)
  B) Vierteljaehrlich — gleiche Strategie, seltener umschichten (weniger Steuer/Kosten)
  C) Lookback-Sweep  — 3/6/9/12 Mon.: ist der Edge fragil (haengt an EINEM Wert) oder breit?

Bewertet wird BRUTTO (Rendite/Sharpe/MaxDD) UND NETTO nach dt. Steuer — denn die
Umschicht-Frage entscheidet sich erst nach Steuer. Mehr Rendite, die der Fiskus frisst,
ist keine Verbesserung.

WICHTIG (Overfitting): Die beste Zahl im Zeitraum 2018-26 ist NICHT automatisch die beste
Strategie. Entscheidend ist, ob eine Variante BREIT (ueber Lookbacks/Perioden) besser ist,
nicht nur an einem Punkt. Echte Bestaetigung gibt nur Out-of-Sample / Live.
"""
from __future__ import annotations
import sys, warnings
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.filterwarnings("ignore")

import pandas as pd
from scripts.champion_duell_fair import load_all, month_ends, run, s_momentum, s_hold, metrics
from scripts.tax_check import backtest_positions

N = 5


def _inv(px, t, avail, need):
    return [k for k in avail if k in px and not pd.isna(px[k].loc[t])
            and len(px[k].loc[:t].dropna()) > need + 1]

def s_mom_lookback(n, days):
    def f(px, t, avail):
        sc = {k: px[k].loc[:t].dropna().iloc[-1] / px[k].loc[:t].dropna().iloc[-days] - 1
              for k in _inv(px, t, avail, days)}
        top = sorted(sc, key=sc.get, reverse=True)[:n]
        return {k: 1.0 / len(top) for k in top} if top else {}
    return f

def s_mom_12_1(n):
    """12-Monats-Momentum, letzten Monat (21 Handelstage) weglassen."""
    def f(px, t, avail):
        sc = {}
        for k in _inv(px, t, avail, 252):
            ser = px[k].loc[:t].dropna()
            sc[k] = ser.iloc[-21] / ser.iloc[-252] - 1
        top = sorted(sc, key=sc.get, reverse=True)[:n]
        return {k: 1.0 / len(top) for k in top} if top else {}
    return f


def cagr_of(curve, years):
    return metrics(curve, years)[1]

def net_cagr(px, rebals, avail, strat, years):
    tot, _tax = backtest_positions(px, rebals, avail, strat, taxed=True)
    return (1 + tot) ** (1 / years) - 1


def main():
    px = load_all(); avail = [k for k in px.columns]
    rebals_m = [d for d in month_ends(px.index) if d in px.index]
    rebals_q = rebals_m[::3]                       # vierteljaehrlich
    years = (rebals_m[-1] - rebals_m[0]).days / 365.25
    print(f"Varianten-Test {str(rebals_m[0])[:10]}..{str(rebals_m[-1])[:10]} ({years:.1f}J), Top-{N}\n")

    base = s_momentum(N)
    spy = s_hold("SPY")

    rows = []  # name, brutto cagr, sharpe, maxdd, netto cagr, rebals
    def add(name, strat, rebals):
        c = run(strat, px, rebals, avail)
        _, g, sh, dd = metrics(c, years)
        n = net_cagr(px, rebals, avail, strat, years)
        rows.append((name, g, sh, dd, n))

    add("SPY halten (Markt)",          spy,            rebals_m)
    add("Mom 6M Top-5 monatl. (LIVE)", base,           rebals_m)
    add("Mom 12-1 Top-5 monatl.",      s_mom_12_1(N),  rebals_m)
    add("Mom 6M Top-5 QUARTAL",        base,           rebals_q)
    add("Mom 12-1 Top-5 QUARTAL",      s_mom_12_1(N),  rebals_q)

    print(f"{'Variante':>28} | {'brutto p.a.':>11} | {'Sharpe':>6} | {'MaxDD':>6} | {'NETTO p.a.':>10}")
    print("-" * 80)
    spy_net = None
    for name, g, sh, dd, n in rows:
        if name.startswith("SPY"): spy_net = n
        print(f"{name:>28} | {g*100:>+10.1f}% | {sh:>6.2f} | {dd*100:>+5.0f}% | {n*100:>+9.1f}%")
    print("-" * 80)
    print(f"\nNETTO-Vorsprung vs Markt (nach Steuer):")
    for name, g, sh, dd, n in rows:
        if name.startswith("SPY"): continue
        print(f"  {name:>28}: {(n-spy_net)*100:>+5.1f}%/J")

    print(f"\n=== Lookback-Sweep (Top-5 monatl., brutto p.a.) — ist der Edge fragil? ===")
    for d in (63, 126, 189, 252):
        g = cagr_of(run(s_mom_lookback(N, d), px, rebals_m, avail), years)
        print(f"  {d//21:>2}-Monats-Momentum: {g*100:>+6.1f}% p.a.")


if __name__ == "__main__":
    main()
