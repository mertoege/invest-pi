#!/usr/bin/env python3
"""
factor2_study.py — Disziplinierter Test: Bringt ein ZWEITER Faktor (Low-Vol)
neben Momentum echten Diversifikations-Mehrwert?

Schutz gegen Selbsttaeuschung:
  - Train (2018-2022) / Test (2023-2026) getrennt ausgewiesen (Out-of-Sample)
  - Korrelation der Monatsrenditen Momentum vs Low-Vol (der eigentliche Beweis)
  - Echte 50/50-Mischung der Portfolios, nicht nur Filter-Kombi
  - Jahr-fuer-Jahr inkl. Crash-Jahre (2018Q4, 2020, 2022)
  - Vorab festgelegtes Erfolgskriterium: bessere Sharpe UND kleinerer MaxDD
    in BEIDEN Haelften. Nicht "hoechste Rendite gewinnt".
"""
from __future__ import annotations
import sys, warnings
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.filterwarnings("ignore")
import pandas as pd
from src.learning.backtest_engine import _load_history
from src.common.universe import UNIVERSE, BENCH

START, END = "2018-01-01", "2026-06-18"
SPLIT = pd.Timestamp("2023-01-01")   # Train < SPLIT <= Test
COST = 0.001
MOM_LB = 126
VOL_LB = 126


def load_all():
    h = _load_history(UNIVERSE + BENCH, start=START, end=END, period="10y")
    closes = {t: df["close"] for t, df in h.items() if df is not None and len(df) > 400}
    return pd.DataFrame(closes).sort_index().ffill()

def month_ends(idx):
    s = pd.Series(idx, index=idx)
    return list(s.groupby([idx.year, idx.month]).last())

def _investable(px, t, avail):
    return [k for k in avail if k in px and not pd.isna(px[k].loc[t])
            and len(px[k].loc[:t].dropna()) > MOM_LB + 1]

# ---- Faktor-Signale (alle nur aus Daten BIS t -> kein Look-Ahead) ----
def s_hold(tk):
    return lambda px, t, avail: {tk: 1.0}

def _mom_scores(px, t, inv):
    return {k: px[k].loc[:t].iloc[-1] / px[k].loc[:t].iloc[-MOM_LB] - 1 for k in inv}

def _vol_scores(px, t, inv):
    return {k: px[k].loc[:t].pct_change().iloc[-VOL_LB:].std() for k in inv}

def s_momentum(n):
    def f(px, t, avail):
        inv = _investable(px, t, avail); sc = _mom_scores(px, t, inv)
        top = sorted(sc, key=sc.get, reverse=True)[:n]
        return {k: 1.0/len(top) for k in top} if top else {}
    return f

def s_lowvol(n):
    def f(px, t, avail):
        inv = _investable(px, t, avail); vol = _vol_scores(px, t, inv)
        low = sorted(vol, key=vol.get)[:n]
        return {k: 1.0/len(low) for k in low} if low else {}
    return f

def s_blend(n_mom, n_vol, w_mom=0.5):
    """ECHTE Mischung: zwei getrennte Koerbe, Gewichte gemischt."""
    mom, vol = s_momentum(n_mom), s_lowvol(n_vol)
    def f(px, t, avail):
        wm, wv = mom(px, t, avail), vol(px, t, avail)
        out = {}
        for k, w in wm.items(): out[k] = out.get(k, 0) + w_mom * w
        for k, w in wv.items(): out[k] = out.get(k, 0) + (1 - w_mom) * w
        s = sum(out.values())
        return {k: w/s for k, w in out.items()} if s > 0 else {}
    return f

def s_mom_then_lowvol(n_mom, n_final):
    """Filter-Kombi: erst Top-Momentum, daraus die ruhigsten."""
    def f(px, t, avail):
        inv = _investable(px, t, avail); sc = _mom_scores(px, t, inv)
        topm = sorted(sc, key=sc.get, reverse=True)[:n_mom]
        vol = _vol_scores(px, t, topm)
        fin = sorted(vol, key=vol.get)[:n_final]
        return {k: 1.0/len(fin) for k in fin} if fin else {}
    return f


def run_rets(strategy, px, rebals, avail):
    """Gibt Monatsrendite-Serie zurueck (nach Kosten), indiziert auf das Periodenende."""
    prev, rows = {}, []
    for i in range(len(rebals) - 1):
        t, nxt = rebals[i], rebals[i+1]
        w = strategy(px, t, avail)
        turn = sum(abs(w.get(k, 0) - prev.get(k, 0)) for k in set(w) | set(prev))
        cost = turn * COST
        gross = 0.0
        if w:
            gross = sum(wt * (px[k].loc[nxt]/px[k].loc[t] - 1)
                        for k, wt in w.items() if not pd.isna(px[k].loc[t]))
        rows.append((nxt, (1 + gross) * (1 - cost) - 1)); prev = w
    return pd.Series(dict(rows))

def metrics(rets):
    if len(rets) < 2: return (0, 0, 0, 0, 0)
    curve = (1 + rets).cumprod()
    yrs = (rets.index[-1] - rets.index[0]).days / 365.25
    total = curve.iloc[-1] - 1
    cagr = curve.iloc[-1] ** (1/yrs) - 1 if yrs > 0 else 0
    sharpe = (rets.mean()/rets.std() * (12**0.5)) if rets.std() > 0 else 0
    maxdd = ((curve - curve.cummax())/curve.cummax()).min()
    return total, cagr, sharpe, maxdd, yrs

def per_year(rets):
    return {y: (1 + g).prod() - 1 for y, g in rets.groupby(rets.index.year)}


def main():
    px = load_all()
    avail = [k for k in UNIVERSE if k in px.columns]
    rebals = [d for d in month_ends(px.index) if d in px.index]
    print(f"Zeitraum {str(rebals[0])[:10]}..{str(rebals[-1])[:10]} | "
          f"Universum {len(avail)}/{len(UNIVERSE)} | Split @ {str(SPLIT)[:10]}")
    print("Erfolgskriterium (vorab): Mischung muss in BEIDEN Haelften bessere Sharpe")
    print("UND kleineren MaxDD als pures Momentum zeigen.\n")

    strategies = [
        ("SPY (Markt)",          s_hold("SPY")),
        ("QQQ (Tech)",           s_hold("QQQ")),
        ("Momentum-5",           s_momentum(5)),
        ("LowVol-5",             s_lowvol(5)),
        ("Blend 50/50 (M5+V5)",  s_blend(5, 5)),
        ("Blend 60/40 (M5+V5)",  s_blend(5, 5, 0.6)),
        ("Mom15->LowVol5",       s_mom_then_lowvol(15, 5)),
    ]

    # Renditen einmal berechnen, dann beliebig slicen
    R = {}
    for name, strat in strategies:
        try:
            R[name] = run_rets(strat, px, rebals, avail)
        except Exception as e:
            print(f"  FEHLER {name}: {str(e)[:50]}")

    def table(title, sl):
        print(f"== {title} ==")
        print(f"{'Strategie':>20} | {'p.a.':>7} | {'Sharpe':>6} | {'MaxDD':>7}")
        print("-"*52)
        spy_cagr = None
        for name, _ in strategies:
            r = R[name]
            if sl: r = r[(r.index >= sl[0]) & (r.index < sl[1])]
            tot, cagr, sh, dd, yrs = metrics(r)
            if name.startswith("SPY"): spy_cagr = cagr
            print(f"{name:>20} | {cagr*100:>+6.1f}% | {sh:>6.2f} | {dd*100:>+6.0f}%")
        print()
        return spy_cagr

    table("GESAMT 2018-2026", None)
    table("TRAIN 2018-2022", (pd.Timestamp(START), SPLIT))
    table("TEST 2023-2026 (out-of-sample)", (SPLIT, pd.Timestamp(END)+pd.Timedelta(days=1)))

    # Korrelation der Monatsrenditen
    print("== Korrelation Monatsrenditen (niedrig = gute Diversifikation) ==")
    key = ["Momentum-5", "LowVol-5", "SPY (Markt)"]
    M = pd.DataFrame({k: R[k] for k in key}).dropna()
    corr = M.corr()
    print(corr.round(2).to_string(), "\n")

    # Jahr-fuer-Jahr (Crash-Federung sichtbar machen)
    print("== Jahr-fuer-Jahr (%) — federt LowVol die Momentum-Schmerzen? ==")
    yc = ["Momentum-5", "LowVol-5", "Blend 50/50 (M5+V5)", "SPY (Markt)"]
    pys = {k: per_year(R[k]) for k in yc}
    years = sorted({y for d in pys.values() for y in d})
    print(f"{'Jahr':>5} | " + " | ".join(f"{k.split()[0]:>8}" for k in yc))
    print("-"*52)
    for y in years:
        print(f"{y:>5} | " + " | ".join(f"{pys[k].get(y,0)*100:>+7.1f}%" for k in yc))
    print()

    # Automatisches Vorab-Urteil
    print("== URTEIL (gegen das vorab festgelegte Kriterium) ==")
    def half(sl):
        out = {}
        for name in ["Momentum-5", "Blend 50/50 (M5+V5)", "Blend 60/40 (M5+V5)"]:
            r = R[name]; r = r[(r.index >= sl[0]) & (r.index < sl[1])]
            _, cagr, sh, dd, _ = metrics(r)
            out[name] = (cagr, sh, dd)
        return out
    tr = half((pd.Timestamp(START), SPLIT))
    te = half((SPLIT, pd.Timestamp(END)+pd.Timedelta(days=1)))
    base = "Momentum-5"
    for blend in ["Blend 50/50 (M5+V5)", "Blend 60/40 (M5+V5)"]:
        better_tr = tr[blend][1] > tr[base][1] and tr[blend][2] > tr[base][2]  # dd: weniger negativ = groesser
        better_te = te[blend][1] > te[base][1] and te[blend][2] > te[base][2]
        verdict = "BESTEHT (robust besser)" if (better_tr and better_te) else \
                  "teilweise" if (better_tr or better_te) else "BESTEHT NICHT"
        print(f"  {blend}: Train Sharpe {tr[blend][1]:.2f} vs {tr[base][1]:.2f}, "
              f"MaxDD {tr[blend][2]*100:+.0f}% vs {tr[base][2]*100:+.0f}% | "
              f"Test Sharpe {te[blend][1]:.2f} vs {te[base][1]:.2f}, "
              f"MaxDD {te[blend][2]*100:+.0f}% vs {te[base][2]*100:+.0f}%  -> {verdict}")


if __name__ == "__main__":
    main()
