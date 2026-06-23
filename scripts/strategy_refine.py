#!/usr/bin/env python3
"""
strategy_refine.py — Meta-Lernen Schritt 1: lohnt eine Branchen-Bremse?

Die reine Momentum-Top-5-Regel konzentriert in Trend-Phasen brutal in einen Sektor
(aktuell 4/5 Halbleiter = Klumpenrisiko). Frage: senkt eine Obergrenze "max N pro
Sektor" das Risiko, ohne die Rendite zu killen? Antwort kommt aus dem Backtest
(2018-2026, faires Universum), nicht aus dem Bauch.
"""
from __future__ import annotations
import sys, warnings
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.filterwarnings("ignore")

import pandas as pd
from scripts.champion_duell_fair import load_all, month_ends, run, metrics, _investable, MOM_LOOKBACK, s_hold

# Branchen-Zuordnung fürs faire Universum (grob, aber ausreichend gegen Klumpen)
SECTOR = {
    **{t: "semi" for t in ["NVDA","INTC","QCOM","TXN","AVGO","MU","AMD"]},
    **{t: "tech" for t in ["AAPL","MSFT","GOOGL","ADBE","CRM","ORCL","CSCO","IBM","ACN","HPQ"]},
    **{t: "comm" for t in ["META","NFLX","CMCSA","T","VZ","DIS"]},
    **{t: "fin" for t in ["JPM","BAC","WFC","C","GS","MS","AXP","USB","PNC","COF","BLK","BRK-B"]},
    **{t: "health" for t in ["JNJ","UNH","PFE","MRK","ABBV","TMO","ABT","LLY","BMY","AMGN","GILD","CVS","MDT","BIIB"]},
    **{t: "staples" for t in ["PG","KO","PEP","WMT","COST","CL","MO","PM","MDLZ","KHC","GIS","KMB"]},
    **{t: "discr" for t in ["AMZN","HD","MCD","NKE","SBUX","LOW","BKNG","F","GM"]},
    **{t: "indu" for t in ["BA","HON","UNP","MMM","GE","CAT","LMT","DE","FDX","UPS","EMR"]},
    **{t: "energy" for t in ["XOM","CVX","SLB","COP","OXY","EOG","KMI"]},
    **{t: "mat" for t in ["LIN"]},
}


def s_momentum_capped(n, max_per_sector):
    """Top-N nach 6M-Momentum, aber höchstens max_per_sector je Branche."""
    def f(px, t, avail):
        sc = {k: px[k].loc[:t].iloc[-1] / px[k].loc[:t].iloc[-MOM_LOOKBACK] - 1
              for k in _investable(px, t, avail)}
        picked, sec_count = [], {}
        for k in sorted(sc, key=sc.get, reverse=True):
            s = SECTOR.get(k, "other")
            if sec_count.get(s, 0) >= max_per_sector:
                continue
            picked.append(k); sec_count[s] = sec_count.get(s, 0) + 1
            if len(picked) == n:
                break
        return {k: 1.0 / len(picked) for k in picked} if picked else {}
    return f


def yearly(curve):
    df = curve.copy(); df.index = pd.to_datetime(df.index); out = {}
    for yr in sorted({d.year for d in df.index}):
        seg = df[df.index.year == yr]
        if len(seg) < 2: continue
        prev = df[df.index.year == yr - 1]
        out[yr] = seg.iloc[-1] / (prev.iloc[-1] if len(prev) else seg.iloc[0]) - 1
    return out


def main():
    px = load_all(); avail = list(px.columns)
    rebals = [d for d in month_ends(px.index) if d in px.index]
    years = (rebals[-1] - rebals[0]).days / 365.25

    variants = [
        ("Markt (SPY)",          s_hold("SPY")),
        ("Top-5 PUR (kein Cap)", s_momentum_capped(5, 5)),
        ("Top-5 max 2/Branche",  s_momentum_capped(5, 2)),
        ("Top-5 max 1/Branche",  s_momentum_capped(5, 1)),
    ]
    print(f"Branchen-Bremse-Test {str(rebals[0])[:10]}..{str(rebals[-1])[:10]} ({years:.1f}J)\n")
    print(f"{'Variante':>22} | {'p.a.':>7} | {'Sharpe':>6} | {'MaxDD':>7} | {'schlägt Markt'}")
    print("-" * 68)
    spy_y = None; curves = {}
    for name, st in variants:
        c = run(st, px, rebals, avail); curves[name] = c
        total, cagr, sharpe, maxdd = metrics(c, years)
        y = yearly(c)
        if name.startswith("Markt"): spy_y = y; wins = "—"
        else:
            wins = f"{sum(1 for yr in y if yr in spy_y and y[yr] > spy_y[yr])}/{len(spy_y)} Jahre"
        print(f"{name:>22} | {cagr*100:>+6.1f}% | {sharpe:>6.2f} | {maxdd*100:>+6.0f}% | {wins}")
    print("-" * 68)
    # Risiko-adjustiert (Sharpe) ist der Maßstab: hohe Rendite ist wertlos, wenn sie
    # nur durch wildes Risiko erkauft ist.
    print("\nEntscheidungs-Kriterium: bester Sharpe bei akzeptablem Drawdown.")


if __name__ == "__main__":
    main()
