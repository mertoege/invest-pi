#!/usr/bin/env python3
"""
tax_check.py — Meta-Lernen Schritt 2: überlebt der Momentum-Edge NACH Steuern?

Die +30 %/Jahr aus dem Backtest sind BRUTTO. Momentum rotiert monatlich und
realisiert dauernd Gewinne → der deutsche Fiskus kassiert bei jedem Verkauf
26,375 % (Abgeltungssteuer + Soli). Buy-and-Hold (SPY) zahlt dagegen erst ganz
am Ende EINMAL — und profitiert bis dahin vom Steuerstundungs-Effekt (das nicht
gezahlte Steuergeld arbeitet weiter mit).

Positionsgenauer Backtest mit echter Kostenbasis, Verlustverrechnungstopf und
Sparerpauschbetrag (1000 €/Jahr). Ehrliche Vereinfachungen: keine Kirchensteuer,
0,05 % Slippage/Trade (Alpaca-US kommissionsfrei), Übergewicht wird nicht
getrimmt (spart unnötige Steuer-Realisierung).
"""
from __future__ import annotations
import sys, warnings
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.filterwarnings("ignore")

import pandas as pd
from scripts.champion_duell_fair import load_all, month_ends, s_momentum, s_hold

TAX = 0.26375
ALLOWANCE = 1000.0     # Sparerpauschbetrag €/Jahr
SLIPPAGE = 0.0005
START_CAP = 50000.0


def backtest_positions(px, rebals, avail, strategy, taxed: bool):
    cash = START_CAP
    holds: dict[str, dict] = {}   # ticker -> {qty, cost_eur}
    loss_pool = 0.0
    allowance = ALLOWANCE
    cur_year = None
    tax_paid = 0.0

    def sell(k, price_t):
        nonlocal cash, loss_pool, allowance, tax_paid
        h = holds.pop(k)
        proceeds = h["qty"] * price_t * (1 - SLIPPAGE)
        gain = proceeds - h["cost"]
        if taxed and gain > 0:
            g = gain
            use_loss = min(loss_pool, g); g -= use_loss; loss_pool -= use_loss
            use_allow = min(allowance, g); g -= use_allow; allowance -= use_allow
            t = g * TAX; proceeds -= t; tax_paid += t
        elif gain < 0:
            loss_pool += -gain
        cash += proceeds

    for i in range(len(rebals) - 1):
        t = rebals[i]
        if t.year != cur_year:
            cur_year = t.year; allowance = ALLOWANCE
        target = strategy(px, t, avail)
        # Verkaufen: alles, was nicht mehr im Ziel ist
        for k in [k for k in holds if k not in target]:
            sell(k, px[k].loc[t])
        # Kaufen/aufstocken Richtung Zielgewicht
        equity = cash + sum(h["qty"] * px[k].loc[t] for k, h in holds.items())
        for k, w in target.items():
            tgt = equity * w
            cur = holds[k]["qty"] * px[k].loc[t] if k in holds else 0.0
            diff = tgt - cur
            if diff > 50:  # nur sinnvolle Käufe
                price = px[k].loc[t] * (1 + SLIPPAGE)
                qty = diff / price
                cash -= diff
                if k in holds:
                    holds[k]["qty"] += qty; holds[k]["cost"] += diff
                else:
                    holds[k] = {"qty": qty, "cost": diff}
    # Finale Liquidation (alles verkaufen → letzte Steuer)
    tE = rebals[-1]
    for k in list(holds):
        sell(k, px[k].loc[tE])
    return cash / START_CAP - 1, tax_paid


def main():
    px = load_all(); avail = list(px.columns)
    rebals = [d for d in month_ends(px.index) if d in px.index]
    years = (rebals[-1] - rebals[0]).days / 365.25

    def cagr(tot): return (1 + tot) ** (1 / years) - 1

    print(f"Steuer-Check {str(rebals[0])[:10]}..{str(rebals[-1])[:10]} ({years:.1f}J, DE-Abgeltungssteuer {TAX*100:.1f}%)\n")

    spy_g, _ = backtest_positions(px, rebals, avail, s_hold("SPY"), taxed=False)
    spy_n, spy_tax = backtest_positions(px, rebals, avail, s_hold("SPY"), taxed=True)
    mom_g, _ = backtest_positions(px, rebals, avail, s_momentum(5), taxed=False)
    mom_n, mom_tax = backtest_positions(px, rebals, avail, s_momentum(5), taxed=True)

    print(f"{'Strategie':>22} | {'p.a. BRUTTO':>11} | {'p.a. NETTO':>10} | {'Steuer gezahlt':>14}")
    print("-" * 68)
    print(f"{'Markt (SPY) halten':>22} | {cagr(spy_g)*100:>+10.1f}% | {cagr(spy_n)*100:>+9.1f}% | {spy_tax:>13,.0f}€")
    print(f"{'Momentum Top-5':>22} | {cagr(mom_g)*100:>+10.1f}% | {cagr(mom_n)*100:>+9.1f}% | {mom_tax:>13,.0f}€")
    print("-" * 68)
    edge_b = (cagr(mom_g) - cagr(spy_g)) * 100
    edge_n = (cagr(mom_n) - cagr(spy_n)) * 100
    print(f"\nVorsprung Momentum vs Markt:  BRUTTO {edge_b:+.1f}%/J   →   NETTO {edge_n:+.1f}%/J")
    print(f"Steuer frisst vom Vorsprung:  {edge_b - edge_n:.1f} Prozentpunkte/Jahr")
    print(f"\nFazit: {'NETTO schlägt Momentum den Markt noch klar — Edge überlebt.' if edge_n > 3 else ('NETTO knapp — Edge schrumpft stark.' if edge_n > 0 else 'NETTO TOT — Steuer frisst den Edge auf.')}")


if __name__ == "__main__":
    main()
