#!/usr/bin/env python3
"""
momentum_rebalance.py — Die neue invest-pi-Kernstrategie (belegt, einfach, robust).

Regel: Halte gleichgewichtet die TOP_N Aktien mit dem stärksten 6-Monats-Momentum
aus einem breiten Large-Cap-Universum. Rebalancing EINMAL pro Monat.

Belegt (faire, survivorship-bias-arme Backtests 2018–2026, inkl. COVID + 2022-Crash):
schlug den Markt in 7/9 Jahren, bei geringerem Drawdown; NETTO nach DE-Steuer noch
~+16 %/Jahr Vorsprung. Branchen-Bremse getestet → schadet, daher reine Regel.
Belege: champion_duell_fair.py, robustness_check.py, strategy_refine.py, tax_check.py.

Aufruf-Wege:
  - Über die Engine: run_strategy.py delegiert hierher, wenn settings.trading.
    strategy_engine == "momentum" (läuft im bestehenden stündlichen Timer; das
    Rebalance selbst feuert nur 1×/Monat dank Fälligkeits-Check).
  - Manuell: python scripts/momentum_rebalance.py            (Schatten/Plan)
             python scripts/momentum_rebalance.py --live     (Spielgeld-Orders)
             python scripts/momentum_rebalance.py --force    (Fälligkeit ignorieren)

SICHERHEIT: Default DRY-RUN. Paper-only. Kill-Switch (data/.KILL) respektiert.
Mid-Term (monatlich) — Daytrading bleibt dem Schwesterprojekt daypi.
"""
from __future__ import annotations
import argparse, datetime as dt, json, os, sys
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
from src.common.data_loader import get_prices
from src.common.fx import eur_per_usd
from scripts.champion_duell_fair import UNIVERSE

# ── Strategie-Parameter (per Backtest validiert — nicht raten) ──
MOM_LOOKBACK = 126      # 6 Monate
TOP_N = 5               # bester Sharpe + Drawdown im Robustheits-Check
INVEST_PCT = 0.95       # 5 % Cash-Puffer
MIN_TRADE_EUR = 50
REBAL_BAND = 0.25       # Top-N nur aufstocken, wenn >25 % unter Ziel
_ROOT = Path(__file__).resolve().parents[1]
KILL_FILE = _ROOT / "data" / ".KILL"
STATE_FILE = _ROOT / "data" / ".momentum_state.json"   # gitignored → überlebt auto_pull


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


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(d: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(exist_ok=True)
        STATE_FILE.write_text(json.dumps(d))
    except Exception as e:
        print(f"  WARN: State nicht gespeichert: {e}")


def rebalance(broker, live: bool) -> dict:
    """Ein Momentum-Rebalance. live=False → nur Plan. Gibt Zusammenfassung zurück."""
    ranking = momentum_ranking()
    if len(ranking) < TOP_N:
        print(f"Zu wenig Momentum-Daten ({len(ranking)}) — Abbruch."); return {"ok": False}
    top = ranking[:TOP_N]
    top_set = {t for t, _ in top}

    acct = broker.get_account()
    positions = {p.ticker: p for p in broker.get_positions()}
    fx = eur_per_usd()
    target_eur = acct.equity_eur * INVEST_PCT / TOP_N

    print(f"\n=== Momentum-Rebalance · {'🔴 LIVE (Spielgeld)' if live else '🟢 PLAN'} ===")
    print(f"Equity {acct.equity_eur:.0f} EUR | Ziel je Position {target_eur:.0f} EUR")
    print("TOP-5 nach 6M-Momentum:")
    for i, (tk, m) in enumerate(top, 1):
        print(f"  {i}. {tk:5} {m*100:>+7.1f}%")

    sells = [(tk, p) for tk, p in positions.items() if tk not in top_set and p.qty > 0]
    buys = []
    for tk, _ in top:
        cur = positions[tk].market_value_eur if tk in positions else 0.0
        diff = target_eur - cur
        if diff > MIN_TRADE_EUR and diff > target_eur * REBAL_BAND:
            buys.append((tk, diff))

    print(f"Plan: {len(sells)} Verkäufe, {len(buys)} Käufe/Aufstockungen")
    if not live:
        for tk, p in sells: print(f"  VERKAUF {tk:5} ~{p.market_value_eur:.0f} EUR")
        for tk, eur in buys: print(f"  KAUF    {tk:5} ~{eur:.0f} EUR")
        return {"ok": True, "live": False, "sells": len(sells), "buys": len(buys),
                "top": [t for t, _ in top]}

    n_ok = 0
    for tk, p in sells:
        try:
            q = broker.get_quote(tk); lp = round(q.last * 0.998, 2) if q.last else None
            r = broker.place_order(ticker=tk, side="sell", qty=p.qty,
                                   order_type="limit" if lp else "market", limit_price=lp)
            print(f"    SELL {tk}: {r.status}"); n_ok += 1
        except Exception as e:
            print(f"    SELL {tk} FEHLER: {e}")
    for tk, eur in buys:
        try:
            q = broker.get_quote(tk)
            if not q.last: print(f"    BUY {tk}: keine Quote"); continue
            qty = round(eur / (q.last * fx), 4); lp = round(q.last * 1.002, 2)
            r = broker.place_order(ticker=tk, side="buy", qty=qty, order_type="limit", limit_price=lp)
            print(f"    BUY {tk}: {qty} @ ~{q.last:.2f} → {r.status}"); n_ok += 1
        except Exception as e:
            print(f"    BUY {tk} FEHLER: {e}")
    # Telegram-Kurzmeldung
    try:
        from src.alerts import notifier
        if notifier.is_configured():
            notifier.send_info(f"📈 <b>Momentum-Rebalance</b> (Spielgeld)\nNeue Top-5: "
                               f"{', '.join(t for t,_ in top)}\n{len(sells)} raus, {len(buys)} rein",
                               label="momentum_rebalance")
    except Exception:
        pass
    print(f"\n  Rebalance fertig ({n_ok} Orders).")
    return {"ok": True, "live": True, "orders": n_ok, "top": [t for t, _ in top]}


def run_due(broker, dry_run: bool = False, force: bool = False) -> int:
    """Vom Timer aufgerufen: rebalanciert nur, wenn diesen Monat noch nicht geschehen."""
    if KILL_FILE.exists():
        print("KILL-SWITCH aktiv — kein Rebalance."); return 0
    today = dt.date.today()
    state = _load_state()
    last = state.get("last_rebalance")  # "YYYY-MM-DD"
    due = force or last is None or last[:7] != today.isoformat()[:7]
    if not due:
        print(f"momentum: dieser Monat bereits rebalanciert ({last}) — skip.")
        return 0
    res = rebalance(broker, live=not dry_run)
    if res.get("ok") and not dry_run:
        _save_state({"last_rebalance": today.isoformat(), "top": res.get("top", [])})
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true", help="Echte (Spielgeld-)Orders senden")
    ap.add_argument("--force", action="store_true", help="Fälligkeits-Check ignorieren")
    ap.add_argument("--mock", action="store_true", help="Mock-Broker statt Alpaca")
    args = ap.parse_args()
    broker = get_broker("mock" if args.mock else "alpaca_paper")
    if not broker.is_paper and not args.mock:
        print("SICHERHEIT: Broker ist nicht paper — Abbruch."); return 1
    return run_due(broker, dry_run=not args.live, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
