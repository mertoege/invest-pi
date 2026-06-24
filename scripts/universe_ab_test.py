#!/usr/bin/env python3
"""
universe_ab_test.py — Ehrlicher A/B: breites vs. enges (Live-)Universum.

Gleiche Engine (Momentum 6M, monatlich), gleiche Periode, gleiche Kosten/Steuer.
Einziger Unterschied: aus WELCHEM Korb die Top-5 gewaehlt werden.

  ENG   = aktuelles Live-Universum aus config.yaml (~41 Werte, KI/Chip-kuratiert)
  BREIT = branchen-breiter 2018er-Large-Cap-Korb (champion_duell_fair, inkl. Nieten)

EHRLICHKEIT: Der enge Korb hat einen Rueckschau-Vorteil — er wurde 2026 aus den
heutigen Gewinnern gebaut. Die hoehere Rendite dort ist daher KEIN echter Edge,
den man live einfangen koennte. Entscheidungskriterium ist deshalb NICHT die hoechste
CAGR, sondern die ROBUSTHEIT: Drawdown, schlechtestes Jahr, Bestaendigkeit vs Markt.
Ein breiter Korb laesst Momentum ueber Branchen rotieren — das ist die Streuung, die
im Crash schuetzt. Beide Koerbe haben Rest-Bias (delistete Namen fehlen).
"""
from __future__ import annotations
import sys, warnings
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.filterwarnings("ignore")

import yaml, pandas as pd
from src.learning.backtest_engine import _load_history
from scripts.champion_duell_fair import (UNIVERSE as BROAD, START, END, month_ends,
                                         run, s_momentum, s_hold, metrics)
from scripts.robustness_check import yearly, max_drawdown_detail
from scripts.tax_check import backtest_positions


def live_universe():
    c = yaml.safe_load(open("config.yaml"))["universe"]
    out = []
    for k, v in c.items():
        if k in ("etfs", "ring_3_sector_etfs"):
            continue
        if isinstance(v, list):
            out += [e["ticker"] for e in v if isinstance(e, dict) and e.get("ticker")]
    return sorted(set(out))


def load_px(tickers):
    h = _load_history(sorted(set(tickers + ["SPY"])), start=START, end=END, period="10y")
    return pd.DataFrame({t: df["close"] for t, df in h.items()
                         if df is not None and len(df) > 400}).sort_index().ffill()


def profile(label, px, years, n=5):
    avail = [k for k in px.columns if k != "SPY"]
    rebals = [d for d in month_ends(px.index) if d in px.index]
    curve = run(s_momentum(n), px, rebals, avail)
    _, cagr, sharpe, maxdd = metrics(curve, years)
    spy_curve = run(s_hold("SPY"), px, rebals, avail)
    # Jahr-fuer-Jahr vs Markt
    my, sy = yearly(curve), yearly(spy_curve)
    yrs = [y for y in sy if y in my]
    wins = sum(my[y] > sy[y] for y in yrs)
    worst_year = min(my[y] for y in yrs)
    # schlechtestes rollierendes 12M vs Markt
    idx = [d for d in curve.index if d in spy_curve.index]
    worst_roll = min((curve.loc[idx[i+12]]/curve.loc[idx[i]] - 1) -
                     (spy_curve.loc[idx[i+12]]/spy_curve.loc[idx[i]] - 1)
                     for i in range(len(idx)-12))
    depth, _, _, _, rec_m = max_drawdown_detail(curve)
    # netto nach Steuer
    net_tot, _ = backtest_positions(px, rebals, avail, s_momentum(n), taxed=True)
    net = (1 + net_tot) ** (1 / years) - 1
    spy_net_tot, _ = backtest_positions(px, rebals, avail, s_hold("SPY"), taxed=True)
    spy_net = (1 + spy_net_tot) ** (1 / years) - 1
    return dict(label=label, n_univ=len(avail), cagr=cagr, sharpe=sharpe, maxdd=maxdd,
                wins=wins, total=len(yrs), worst_year=worst_year, worst_roll=worst_roll,
                rec_m=rec_m, net=net, net_edge=net - spy_net)


def main():
    live = live_universe()
    px_eng = load_px(live)
    px_brd = load_px(BROAD)
    rb = [d for d in month_ends(px_brd.index) if d in px_brd.index]
    years = (rb[-1] - rb[0]).days / 365.25

    print(f"A/B-Universumstest {START}..{END[:10]} ({years:.1f}J), Momentum Top-5 monatlich\n")
    overlap = sorted(set(live) & set(BROAD))
    print(f"ENG (Live):   {len(live)} Werte")
    print(f"BREIT:        {len(BROAD)} Werte")
    print(f"Ueberlappung: {len(overlap)} Namen in beiden | BREIT bringt {len(set(BROAD)-set(live))} zusaetzliche\n")

    rows = [profile("ENG (Live, KI-kuratiert)", px_eng, years),
            profile("BREIT (branchen-breit)", px_brd, years)]

    print(f"{'Korb':>26} | {'CAGR':>6} | {'Sharpe':>6} | {'MaxDD':>6} | {'netto-Edge':>10} | {'Jahre>Markt':>11} | {'schlecht.Jahr':>13} | {'worst 12M vs Markt':>18}")
    print("-" * 122)
    for r in rows:
        print(f"{r['label']:>26} | {r['cagr']*100:>+5.0f}% | {r['sharpe']:>6.2f} | {r['maxdd']*100:>+5.0f}% | "
              f"{r['net_edge']*100:>+9.1f}% | {r['wins']:>4}/{r['total']:<5} | {r['worst_year']*100:>+12.0f}% | {r['worst_roll']*100:>+17.0f}%")
    print("-" * 122)
    print("\nEHRLICH: Hoehere CAGR im ENGEN Korb ist Rueckschau-Bias (Gewinner vorausgewaehlt), NICHT live einfangbar.")
    print("Robustheits-Kriterien (zaehlen wirklich): MaxDD, schlechtestes Jahr, worst 12M, Bestaendigkeit vs Markt.")


if __name__ == "__main__":
    main()
