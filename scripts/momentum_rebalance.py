#!/usr/bin/env python3
"""
momentum_rebalance.py — Die neue invest-pi-Kernstrategie (belegt, einfach, robust).

Regel: Halte gleichgewichtet die TOP_N Aktien mit dem stärksten 6-Monats-Momentum
aus einem breiten Large-Cap-Universum. Monatliches Rebalancing. Punkt.

Hintergrund: Im fairen, survivorship-bias-armen Backtest (2018–2026, inkl. COVID-
und 2022-Crash) schlug diese Regel den Markt in 7 von 9 Jahren um ~+16 %/Jahr —
bei GERINGEREM Drawdown als der Markt. Sie ersetzt die alte komplexe Score-/
Trading-Maschinerie, die nachweislich nichts brachte (Audit/Backtests 2026-06).
Das "Lernen" zieht eine Ebene höher: Strategie-Parameter werden per Backtest
validiert (champion_duell_fair.py / robustness_check.py), nicht Trade-für-Trade.

SICHERHEIT: Default DRY-RUN (zeigt nur den Plan). Echte Orders nur mit --live.
Paper-only. Respektiert den Kill-Switch (data/.KILL).
Mid-Term (monatlich) — Daytrading bleibt bewusst dem Schwesterprojekt daypi.
"""
from __future__ import annotations
import argparse, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_env = Path(__file__).resolve().parents[1] / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from src.broker import get_broker
from src.common import config as cfg_mod
from src.common.data_loader import get_prices
from src.common.fx import eur_per_usd
from scripts.champion_duell_fair import UNIVERSE

# ── Strategie-Parameter (per Backtest validiert — nicht raten) ──
MOM_LOOKBACK = 126      # 6 Monate
TOP_N = 5               # bester Sharpe + Drawdown im Robustheits-Check
INVEST_PCT = 0.95       # 5 % Cash-Puffer
MIN_TRADE_EUR = 50      # kleine Abweichungen nicht handeln (spart Kosten)
REBAL_BAND = 0.25       # nur traden wenn Position >25 % vom Ziel abweicht
KILL_FILE = Path(__file__).resolve().parents[1] / "data" / ".KILL"


def momentum_ranking() -> list[tuple[str, float]]:
    scores = {}
    for tk in UNIVERSE:
        try:
            px = get_prices(tk, period="1y")
            if px is None or len(px) < MOM_LOOKBACK + 1:
                continue
            s = px["close"]
            scores[tk] = float(s.iloc[-1] / s.iloc[-MOM_LOOKBACK] - 1)
        except Exception:
            continue
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true", help="Echte Orders senden (sonst nur Plan)")
    ap.add_argument("--mock", action="store_true", help="Mock-Broker statt Alpaca")
    args = ap.parse_args()

    if KILL_FILE.exists():
        print("KILL-SWITCH aktiv (data/.KILL) — Abbruch."); return 0

    t_cfg = cfg_mod.load().settings  # nur für broker-Namen
    broker = get_broker("mock" if args.mock else "alpaca_paper")
    if not broker.is_paper and not args.mock:
        print("SICHERHEIT: Broker ist nicht paper — Abbruch (Echtgeld nicht freigegeben)."); return 1

    ranking = momentum_ranking()
    if len(ranking) < TOP_N:
        print(f"Zu wenig Momentum-Daten ({len(ranking)}) — Abbruch."); return 1
    top = ranking[:TOP_N]
    top_set = {t for t, _ in top}

    acct = broker.get_account()
    positions = {p.ticker: p for p in broker.get_positions()}
    fx = eur_per_usd()
    target_eur = acct.equity_eur * INVEST_PCT / TOP_N

    mode = "🔴 LIVE" if args.live else "🟢 SCHATTEN (nur Plan, keine Orders)"
    print(f"\n=== Momentum-Rebalance · {mode} ===")
    print(f"Equity: {acct.equity_eur:.0f} EUR | Cash: {acct.cash_eur:.0f} EUR | Ziel je Position: {target_eur:.0f} EUR\n")
    print(f"TOP-{TOP_N} nach 6M-Momentum (= neues Ziel-Portfolio):")
    for i, (tk, m) in enumerate(top, 1):
        have = positions.get(tk)
        hv = f"halte {have.market_value_eur:.0f} EUR" if have else "neu"
        print(f"  {i}. {tk:5} {m*100:>+7.1f}%   ({hv})")
    print(f"\n  (Rang 6-10: {', '.join(t for t,_ in ranking[5:10])})")

    sells, buys = [], []
    # Verkaufen: alles was gehalten wird, aber nicht mehr Top-N
    for tk, p in positions.items():
        if tk not in top_set and p.qty > 0:
            sells.append((tk, p.qty, p.market_value_eur, "nicht mehr Top-5"))
    # Kaufen/Aufstocken: Top-N Richtung Ziel
    for tk, _ in top:
        cur = positions[tk].market_value_eur if tk in positions else 0.0
        diff = target_eur - cur
        if diff > MIN_TRADE_EUR and diff > target_eur * REBAL_BAND:
            buys.append((tk, diff))

    print("\n── Trade-Plan ──")
    if not sells and not buys:
        print("  Portfolio bereits auf Ziel — keine Trades nötig.")
    for tk, qty, val, why in sells:
        print(f"  VERKAUF {tk:5} ~{val:.0f} EUR ({why})")
    for tk, eur in buys:
        print(f"  KAUF    {tk:5} ~{eur:.0f} EUR (Richtung Ziel {target_eur:.0f})")

    if not args.live:
        print("\n  → Schatten-Modus: nichts ausgeführt. Mit --live scharfschalten.")
        return 0

    # ── LIVE-Ausführung ──
    print("\n  → LIVE: sende Orders …")
    for tk, qty, val, why in sells:
        try:
            q = broker.get_quote(tk)
            lp = round(q.last * 0.998, 2) if q.last else None
            r = broker.place_order(ticker=tk, side="sell", qty=qty,
                                   order_type="limit" if lp else "market", limit_price=lp)
            print(f"    SELL {tk}: {r.status}")
        except Exception as e:
            print(f"    SELL {tk} FEHLER: {e}")
    for tk, eur in buys:
        try:
            q = broker.get_quote(tk)
            if not q.last: print(f"    BUY {tk}: keine Quote"); continue
            qty = round(eur / (q.last * fx), 4)
            lp = round(q.last * 1.002, 2)
            r = broker.place_order(ticker=tk, side="buy", qty=qty,
                                   order_type="limit", limit_price=lp)
            print(f"    BUY {tk}: {qty} @ ~{q.last:.2f} → {r.status}")
        except Exception as e:
            print(f"    BUY {tk} FEHLER: {e}")
    print("\n  Rebalance fertig.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
