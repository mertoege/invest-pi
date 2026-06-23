#!/usr/bin/env python3
"""
champion_duell.py — Ehrlicher Strategie-Vergleich gegen ETFs (2018–2026).

Ziel (Mert): Schlägt IRGENDEIN Mid-Term-Ansatz die gängigen ETFs robust über
mehrere Jahre INKL. Crash (COVID 2020 + Bär 2022) — oder ist Kaufen-und-Halten
die ehrlichste Antwort? Mid-/Long-Term only (monatliches Rebalancing) —
Daytrading bleibt bewusst dem Schwesterprojekt daypi überlassen.

Methodik (fair, kein Look-ahead):
  - Monatliches Rebalancing am letzten Handelstag.
  - Entscheidung an Tag t nutzt NUR Daten bis t; Performance über [t, t+1M].
  - Transaktionskosten 0,1% auf Umschichtung (Turnover).
  - Survivorship-Bias-Hinweis: Universum = heutige Large-Caps, die schon 2018
    liquide waren. Leichter Bias zugunsten der Aktien-Strategien (ehrlich genannt).
"""
from __future__ import annotations
import sys, warnings
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.filterwarnings("ignore")

import pandas as pd
from src.learning.backtest_engine import _load_history

START, END = "2018-01-01", "2026-06-18"
COST = 0.001            # 0,1% pro Umschichtungs-Turnover
MOM_LOOKBACK = 126      # 6 Monate Momentum
SMA_TREND = 200         # Trend-Filter

UNIVERSE = ["NVDA","AMD","MSFT","AAPL","GOOGL","AMZN","META","AVGO","TSM","JPM",
            "UNH","LLY","PG","KO","JNJ","V","COST","ABBV","NFLX","ADBE","CRM",
            "INTC","QCOM","TXN","CAT","HD","MA","DIS","XOM","WMT"]
BENCH = ["SPY","QQQ","SMH"]


def load_all():
    h = _load_history(UNIVERSE + BENCH, start=START, end=END, period="10y")
    closes = {t: df["close"] for t, df in h.items() if df is not None and len(df) > 300}
    return pd.DataFrame(closes).sort_index().ffill()


def month_end_dates(idx):
    s = pd.Series(idx, index=idx)
    return list(s.groupby([idx.year, idx.month]).last())


def run(strategy, px, rebals):
    """strategy(px, asof) -> dict{ticker: weight}. Gibt equity-Serie zurück."""
    eq, prev = 1.0, {}
    curve = []
    for i in range(len(rebals) - 1):
        t, nxt = rebals[i], rebals[i + 1]
        w = strategy(px, t)
        turnover = sum(abs(w.get(k, 0) - prev.get(k, 0)) for k in set(w) | set(prev))
        eq *= (1 - turnover * COST)
        if w:
            ret = sum(wt * (px[k].loc[nxt] / px[k].loc[t] - 1)
                      for k, wt in w.items() if k in px and not pd.isna(px[k].loc[t]))
            eq *= (1 + ret)
        curve.append((nxt, eq))
        prev = w
    return pd.Series(dict(curve))


def metrics(curve, years):
    total = curve.iloc[-1] / 1.0 - 1
    cagr = (curve.iloc[-1]) ** (1 / years) - 1
    rets = curve.pct_change().dropna()
    sharpe = (rets.mean() / rets.std() * (12 ** 0.5)) if rets.std() > 0 else 0
    run_max = curve.cummax()
    maxdd = ((curve - run_max) / run_max).min()
    return total, cagr, sharpe, maxdd


# ---- Strategien ----
def s_hold(ticker):
    return lambda px, t: {ticker: 1.0}

def _momentum(px, t, n):
    scores = {}
    for k in UNIVERSE:
        if k not in px: continue
        s = px[k].loc[:t]
        if len(s) < MOM_LOOKBACK + 1 or pd.isna(s.iloc[-1]): continue
        scores[k] = s.iloc[-1] / s.iloc[-MOM_LOOKBACK] - 1
    top = sorted(scores, key=scores.get, reverse=True)[:n]
    return {k: 1.0 / len(top) for k in top} if top else {}

def s_mom(n):
    return lambda px, t: _momentum(px, t, n)

def s_mom_trend(n):
    def f(px, t):
        spy = px["SPY"].loc[:t]
        if len(spy) >= SMA_TREND and spy.iloc[-1] < spy.iloc[-SMA_TREND:].mean():
            return {}  # Markt unter Trend -> Cash (Crash-Schutz)
        return _momentum(px, t, n)
    return f

def s_quality_hold(px, t):
    core = ["MSFT","AAPL","GOOGL","AMZN","NVDA","V","UNH","LLY","COST","JPM"]
    have = [k for k in core if k in px]
    return {k: 1.0 / len(have) for k in have}


def main():
    px = load_all()
    rebals = [d for d in month_end_dates(px.index) if d in px.index]
    years = (rebals[-1] - rebals[0]).days / 365.25
    print(f"Champion-Duell {str(rebals[0])[:10]}..{str(rebals[-1])[:10]} ({years:.1f} Jahre, {len(rebals)} Monate)")
    print(f"Universum: {len(UNIVERSE)} Aktien | Kosten {COST*100:.1f}%/Turnover | inkl. COVID-2020 + Bär-2022\n")

    champions = [
        ("SPY halten (Markt)",        s_hold("SPY")),
        ("QQQ halten (Tech)",         s_hold("QQQ")),
        ("SMH halten (Halbleiter)",   s_hold("SMH")),
        ("Quality-Growth halten",     s_quality_hold),
        ("Momentum Top-3 (konz.)",    s_mom(3)),
        ("Momentum Top-5",            s_mom(5)),
        ("Momentum Top-5 + Trend",    s_mom_trend(5)),
    ]
    print(f"{'Strategie':>24} | {'Gesamt':>9} | {'p.a.':>7} | {'Sharpe':>6} | {'MaxDD':>7}")
    print("-" * 70)
    spy_cagr = None
    res = []
    for name, strat in champions:
        try:
            curve = run(strat, px, rebals)
            total, cagr, sharpe, maxdd = metrics(curve, years)
            if name.startswith("SPY"): spy_cagr = cagr
            res.append((name, cagr))
            print(f"{name:>24} | {total*100:>+8.0f}% | {cagr*100:>+6.1f}% | {sharpe:>6.2f} | {maxdd*100:>+6.0f}%")
        except Exception as e:
            print(f"{name:>24} | FEHLER: {str(e)[:30]}")
    print("-" * 70)
    if spy_cagr is not None:
        print(f"\nAlpha p.a. vs SPY (Markt):")
        for name, cagr in res:
            if not name.startswith("SPY"):
                d = (cagr - spy_cagr) * 100
                verdict = "SCHLÄGT Markt ✓" if d > 0.5 else ("~gleich" if d > -0.5 else "unter Markt ✗")
                print(f"  {name:>24}: {d:>+5.1f}% p.a.  {verdict}")


if __name__ == "__main__":
    main()
