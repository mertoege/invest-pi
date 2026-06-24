#!/usr/bin/env python3
"""
regime_overlay_check.py — Schuetzt ein simpler Regime-Schalter vor dem Momentum-Crash?

Das Risiko, das champion_duell_fair / robustness_check NICHT testen: Der Zeitraum
2018-2026 hatte keinen echten Momentum-Crash (scharfe Trendwende nach Markttief,
wie 2009). Momentum's bekanntes Versagensmuster. Ein klassischer Schutz: nur
handeln, solange der Markt ueber seinem 200-Tage-Schnitt steht — sonst Cash.

Testet drei Varianten gegen den Markt:
  - Momentum Top-5 (pur)            — der aktuelle Live-Stand
  - Momentum Top-5 + Regime-Schalter (SPY < 200d-MA am Rebal-Tag -> Cash)
  - SPY halten (Referenz)

Frage: Senkt der Schalter den Drawdown SPUERBAR, ohne die Rendite zu killen?
Wenn ja = billige Versicherung gegen das eine ungetestete Szenario. Wenn nein
(Schalter kostet mehr Rendite als er Drawdown spart) = Finger weg, pur lassen.
"""
from __future__ import annotations
import sys, warnings
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.filterwarnings("ignore")

import pandas as pd
from scripts.champion_duell_fair import load_all, month_ends, run, s_momentum, s_hold, metrics

MA_DAYS = 200


def s_momentum_regime(n, px_full):
    """Momentum Top-n, aber nur wenn SPY am Rebal-Tag >= 200d-MA. Sonst Cash."""
    base = s_momentum(n)
    spy = px_full["SPY"]
    def f(px, t, avail):
        hist = spy.loc[:t].dropna()
        if len(hist) < MA_DAYS:
            return base(px, t, avail)
        ma = hist.iloc[-MA_DAYS:].mean()
        if hist.iloc[-1] < ma:
            return {}            # Markt unter Trend -> Cash, kein Momentum-Ritt
        return base(px, t, avail)
    return f


def main():
    px = load_all()
    avail = [k for k in px.columns]
    rebals = [d for d in month_ends(px.index) if d in px.index]
    years = (rebals[-1] - rebals[0]).days / 365.25
    print(f"Regime-Overlay-Test {str(rebals[0])[:10]}..{str(rebals[-1])[:10]} ({years:.1f}J, MA={MA_DAYS}d)\n")

    variants = [
        ("SPY halten (Markt)",         s_hold("SPY")),
        ("Momentum Top-5 (pur)",       s_momentum(5)),
        ("Momentum Top-5 + Regime",    s_momentum_regime(5, px)),
        ("Momentum Top-3 (pur)",       s_momentum(3)),
        ("Momentum Top-3 + Regime",    s_momentum_regime(3, px)),
    ]
    print(f"{'Variante':>26} | {'Gesamt':>9} | {'p.a.':>7} | {'Sharpe':>6} | {'MaxDD':>7}")
    print("-" * 74)
    curves = {}
    for name, st in variants:
        c = run(st, px, rebals, avail); curves[name] = c
        total, cagr, sharpe, maxdd = metrics(c, years)
        print(f"{name:>26} | {total*100:>+8.0f}% | {cagr*100:>+6.1f}% | {sharpe:>6.2f} | {maxdd*100:>+6.0f}%")
    print("-" * 74)

    # Direktvergleich pur vs Regime fuer Top-5
    pur = metrics(curves["Momentum Top-5 (pur)"], years)
    reg = metrics(curves["Momentum Top-5 + Regime"], years)
    d_cagr = (reg[1] - pur[1]) * 100
    d_dd = (reg[3] - pur[3]) * 100   # positiv = weniger tiefer Drawdown (besser)
    print(f"\nTop-5: Regime-Schalter vs pur:")
    print(f"  Rendite p.a.: {d_cagr:+.1f} Punkte | MaxDD: {d_dd:+.1f} Punkte (positiv = flacher)")
    if d_dd > 3 and d_cagr > -3:
        verdict = "LOHNT — Drawdown deutlich flacher, Rendite kaum schlechter. Billige Versicherung."
    elif d_cagr < -3:
        verdict = "FINGER WEG — Schalter kostet zu viel Rendite. Pur lassen."
    else:
        verdict = "MARGINAL — kaum Unterschied. Im Zweifel pur (einfacher = robuster)."
    print(f"  Urteil: {verdict}")


if __name__ == "__main__":
    main()
