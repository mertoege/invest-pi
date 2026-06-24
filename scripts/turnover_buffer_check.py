#!/usr/bin/env python3
"""
turnover_buffer_check.py — Mehr NETTO-Rendite durch weniger Umschichten?

Idee ohne Signal-Aenderung: Momentum Top-5 bleibt, ABER ein gehaltener Titel wird
NICHT schon verkauft, wenn er aus den Top-5 faellt — erst wenn er aus den Top-`keep`
faellt (Ranking-Puffer). Das senkt die Umschlaghaeufigkeit -> weniger realisierte
Gewinne -> weniger Steuer. Klassischer 'buffer'-Trick aus der Momentum-Literatur.

Frage: Steigt der NETTO-Vorsprung (nach dt. Steuer), ohne dass die Rendite leidet?
keep=5 == aktueller Live-Stand (kein Puffer).
"""
from __future__ import annotations
import sys, warnings
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.filterwarnings("ignore")

import pandas as pd
from scripts.champion_duell_fair import load_all, month_ends, _investable, MOM_LOOKBACK

TAX = 0.26375
ALLOWANCE = 1000.0
SLIPPAGE = 0.0005
START_CAP = 50000.0
N = 5


def mom_ranking(px, t, avail):
    sc = {k: px[k].loc[:t].dropna().iloc[-1] / px[k].loc[:t].dropna().iloc[-MOM_LOOKBACK] - 1
          for k in _investable(px, t, avail)}
    return sorted(sc, key=sc.get, reverse=True)


def backtest_buffer(px, rebals, avail, keep, taxed=True):
    cash = START_CAP
    holds = {}
    loss_pool = 0.0; allowance = ALLOWANCE; cur_year = None
    tax_paid = 0.0; sells = 0

    def sell(k, price_t):
        nonlocal cash, loss_pool, allowance, tax_paid, sells
        h = holds.pop(k); sells += 1
        proceeds = h["qty"] * price_t * (1 - SLIPPAGE)
        gain = proceeds - h["cost"]
        if taxed and gain > 0:
            g = gain
            u = min(loss_pool, g); g -= u; loss_pool -= u
            u = min(allowance, g); g -= u; allowance -= u
            tx = g * TAX; proceeds -= tx; tax_paid += tx
        elif gain < 0:
            loss_pool += -gain
        cash += proceeds

    for i in range(len(rebals) - 1):
        t = rebals[i]
        if t.year != cur_year:
            cur_year = t.year; allowance = ALLOWANCE
        rank = mom_ranking(px, t, avail)
        topN = rank[:N]
        keepset = set(rank[:keep])
        chosen = [k for k in holds if k in keepset]
        for k in topN:
            if len(chosen) >= N: break
            if k not in chosen: chosen.append(k)
        for k in rank:
            if len(chosen) >= N: break
            if k not in chosen: chosen.append(k)
        chosen = chosen[:N]
        for k in [k for k in holds if k not in chosen]:
            sell(k, px[k].loc[t])
        equity = cash + sum(h["qty"] * px[k].loc[t] for k, h in holds.items())
        for k in chosen:
            tgt = equity / N
            cur = holds[k]["qty"] * px[k].loc[t] if k in holds else 0.0
            diff = tgt - cur
            if diff > 50:
                price = px[k].loc[t] * (1 + SLIPPAGE)
                cash -= diff
                if k in holds:
                    holds[k]["qty"] += diff / price; holds[k]["cost"] += diff
                else:
                    holds[k] = {"qty": diff / price, "cost": diff}
    tE = rebals[-1]
    for k in list(holds):
        sell(k, px[k].loc[tE])
    return cash / START_CAP - 1, tax_paid, sells


def main():
    px = load_all(); avail = list(px.columns)
    rebals = [d for d in month_ends(px.index) if d in px.index]
    years = (rebals[-1] - rebals[0]).days / 365.25
    def cagr(tot): return (1 + tot) ** (1 / years) - 1

    from scripts.tax_check import backtest_positions
    from scripts.champion_duell_fair import s_hold
    spy_tot, _ = backtest_positions(px, rebals, avail, s_hold("SPY"), taxed=True)
    spy_net = cagr(spy_tot)

    print(f"Turnover-Puffer-Test ({years:.1f}J, netto nach {TAX*100:.1f}% Steuer)")
    print(f"Markt (SPY) netto: {spy_net*100:+.1f}%/J\n")
    print(f"{'Puffer keep':>12} | {'netto p.a.':>10} | {'Steuer':>10} | {'Verkaeufe':>9} | {'Vorspr. vs Markt':>16}")
    print("-" * 74)
    for keep in (5, 8, 10, 15, 20):
        tot, tax, sells = backtest_buffer(px, rebals, avail, keep, taxed=True)
        c = cagr(tot)
        tag = "  <- LIVE (kein Puffer)" if keep == 5 else ""
        print(f"{keep:>12} | {c*100:>+9.1f}% | {tax:>9,.0f}EUR | {sells:>9} | {(c-spy_net)*100:>+15.1f}%{tag}")
    print("-" * 74)
    print("Lies: hoeherer keep = traegere Umschichtung. Steigt netto p.a. mit keep,")
    print("ist Steuer-Sparen den Puffer wert. Faellt sie, kostet Traegheit Rendite.")


if __name__ == "__main__":
    main()
