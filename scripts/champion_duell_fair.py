#!/usr/bin/env python3
"""
champion_duell_fair.py — Survivorship-Bias-armer Strategie-Test (2018–2026).

Unterschied zu champion_duell.py: Das Universum ist ein BREITER Korb großer
US-Aktien von Anfang 2018 — BEWUSST inklusive der späteren Underperformer
(GE, INTC, IBM, T, F, WBA, KHC, MMM, PFE, BA, …). Damit fällt der Survivorship-
Bias weitgehend weg: Die Strategien dürfen NICHT rückblickend die Gewinner
kennen, sondern müssen sie per REGEL aus damals verfügbaren Daten finden
(Momentum / niedrige Vola). Schlägt ein regelbasierter Ansatz DANN noch die
ETFs, ist der Edge echt.

Ehrlich bleibt: delistete/übernommene Namen (CELG, UTX…) fehlen mangels Daten —
ein kleiner Rest-Bias bleibt, aber der große „nur heutige Top-10"-Bias ist weg.
Mid-Term (monatl. Rebalancing); Daytrading bleibt daypi.
"""
from __future__ import annotations
import sys, warnings
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.filterwarnings("ignore")

import pandas as pd
from src.learning.backtest_engine import _load_history

START, END = "2018-01-01", "2026-06-18"
COST = 0.001
MOM_LOOKBACK = 126
VOL_LOOKBACK = 126

# Breites 2018er-Large-Cap-Universum — INKL. der bekannten Nieten der letzten 8 Jahre
UNIVERSE = [
    # Tech (Gewinner UND Verlierer)
    "AAPL","MSFT","GOOGL","AMZN","META","NVDA","ADBE","CRM","ORCL","CSCO",
    "INTC","IBM","QCOM","TXN","AVGO","MU","AMD","NFLX","ACN","HPQ",
    # Financials
    "JPM","BAC","WFC","C","GS","MS","AXP","USB","PNC","SCHW","COF","BLK","BRK-B",
    # Healthcare (inkl. Underperformer PFE/BMY/GILD/CVS/WBA)
    "JNJ","UNH","PFE","MRK","ABBV","TMO","ABT","LLY","BMY","AMGN","GILD","CVS","MDT","BIIB",
    # Staples (inkl. KHC/MO/PM/WBA Verlierer)
    "PG","KO","PEP","WMT","COST","CL","MO","PM","MDLZ","KHC","GIS","KMB",
    # Consumer Disc (inkl. F/GM/M Verlierer)
    "HD","MCD","NKE","SBUX","LOW","DIS","BKNG","F","GM",
    # Industrials (inkl. GE/BA/MMM Verlierer)
    "BA","HON","UNP","MMM","GE","CAT","LMT","DE","FDX","UPS","EMR",
    # Energy (lange schwach, 2022 stark)
    "XOM","CVX","SLB","COP","OXY","EOG","KMI",
    # Comm/Materials (inkl. T/VZ Verlierer)
    "T","VZ","CMCSA","LIN",
]
BENCH = ["SPY","QQQ"]


def load_all():
    h = _load_history(UNIVERSE + BENCH, start=START, end=END, period="10y")
    closes = {t: df["close"] for t, df in h.items() if df is not None and len(df) > 400}
    return pd.DataFrame(closes).sort_index().ffill()


def month_ends(idx):
    s = pd.Series(idx, index=idx)
    return list(s.groupby([idx.year, idx.month]).last())


def run(strategy, px, rebals, avail):
    eq, prev, curve = 1.0, {}, []
    for i in range(len(rebals) - 1):
        t, nxt = rebals[i], rebals[i + 1]
        w = strategy(px, t, avail)
        turn = sum(abs(w.get(k, 0) - prev.get(k, 0)) for k in set(w) | set(prev))
        eq *= (1 - turn * COST)
        if w:
            ret = sum(wt * (px[k].loc[nxt] / px[k].loc[t] - 1)
                      for k, wt in w.items() if not pd.isna(px[k].loc[t]))
            eq *= (1 + ret)
        curve.append((nxt, eq)); prev = w
    return pd.Series(dict(curve))


def metrics(curve, years):
    total = curve.iloc[-1] - 1
    cagr = curve.iloc[-1] ** (1 / years) - 1
    rets = curve.pct_change().dropna()
    sharpe = (rets.mean() / rets.std() * (12 ** 0.5)) if rets.std() > 0 else 0
    maxdd = ((curve - curve.cummax()) / curve.cummax()).min()
    return total, cagr, sharpe, maxdd


def _investable(px, t, avail):
    return [k for k in avail if k in px and not pd.isna(px[k].loc[t])
            and len(px[k].loc[:t].dropna()) > MOM_LOOKBACK + 1]

def s_hold(tk):
    return lambda px, t, avail: {tk: 1.0}

def s_equal_all(px, t, avail):
    inv = _investable(px, t, avail)
    return {k: 1.0 / len(inv) for k in inv} if inv else {}

def s_momentum(n):
    def f(px, t, avail):
        sc = {k: px[k].loc[:t].iloc[-1] / px[k].loc[:t].iloc[-MOM_LOOKBACK] - 1
              for k in _investable(px, t, avail)}
        top = sorted(sc, key=sc.get, reverse=True)[:n]
        return {k: 1.0 / len(top) for k in top} if top else {}
    return f

def s_lowvol(n):
    """Quality-Proxy: die n Aktien mit der niedrigsten 6M-Schwankung (in Echtzeit wissbar)."""
    def f(px, t, avail):
        vol = {}
        for k in _investable(px, t, avail):
            r = px[k].loc[:t].pct_change().iloc[-VOL_LOOKBACK:]
            vol[k] = r.std()
        low = sorted(vol, key=vol.get)[:n]
        return {k: 1.0 / len(low) for k in low} if low else {}
    return f

def s_mom_then_lowvol(n_mom, n_final):
    """Erst Top-Momentum, daraus die schwankungsärmsten — Rendite + Stabilität."""
    def f(px, t, avail):
        inv = _investable(px, t, avail)
        sc = {k: px[k].loc[:t].iloc[-1] / px[k].loc[:t].iloc[-MOM_LOOKBACK] - 1 for k in inv}
        topm = sorted(sc, key=sc.get, reverse=True)[:n_mom]
        vol = {k: px[k].loc[:t].pct_change().iloc[-VOL_LOOKBACK:].std() for k in topm}
        fin = sorted(vol, key=vol.get)[:n_final]
        return {k: 1.0 / len(fin) for k in fin} if fin else {}
    return f


def main():
    px = load_all()
    avail = [k for k in UNIVERSE if k in px.columns]
    rebals = [d for d in month_ends(px.index) if d in px.index]
    years = (rebals[-1] - rebals[0]).days / 365.25
    print(f"FAIRER Test {str(rebals[0])[:10]}..{str(rebals[-1])[:10]} ({years:.1f} Jahre)")
    print(f"Universum: {len(avail)}/{len(UNIVERSE)} Aktien geladen (inkl. Nieten) | inkl. COVID + Bär-2022\n")

    champs = [
        ("SPY halten (Markt)",        s_hold("SPY")),
        ("QQQ halten (Tech)",         s_hold("QQQ")),
        ("Alle gleichgewichtet",      s_equal_all),
        ("Low-Vol Top-10 (Quality)",  s_lowvol(10)),
        ("Momentum Top-3",            s_momentum(3)),
        ("Momentum Top-5",            s_momentum(5)),
        ("Momentum Top-10",           s_momentum(10)),
        ("Mom-15 -> LowVol-5",        s_mom_then_lowvol(15, 5)),
    ]
    print(f"{'Strategie':>24} | {'Gesamt':>9} | {'p.a.':>7} | {'Sharpe':>6} | {'MaxDD':>7}")
    print("-" * 70)
    spy = None; res = []
    for name, strat in champs:
        try:
            c = run(strat, px, rebals, avail)
            total, cagr, sharpe, maxdd = metrics(c, years)
            if name.startswith("SPY"): spy = cagr
            res.append((name, cagr));
            print(f"{name:>24} | {total*100:>+8.0f}% | {cagr*100:>+6.1f}% | {sharpe:>6.2f} | {maxdd*100:>+6.0f}%")
        except Exception as e:
            print(f"{name:>24} | FEHLER: {str(e)[:34]}")
    print("-" * 70)
    if spy is not None:
        print(f"\nAlpha p.a. vs SPY:")
        for name, cagr in res:
            if name.startswith("SPY"): continue
            d = (cagr - spy) * 100
            v = "SCHLÄGT Markt ✓" if d > 0.5 else ("~gleich" if d > -0.5 else "unter Markt ✗")
            print(f"  {name:>24}: {d:>+5.1f}%  {v}")


if __name__ == "__main__":
    main()
