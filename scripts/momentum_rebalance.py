#!/usr/bin/env python3
"""
momentum_rebalance.py - Die neue invest-pi-Kernstrategie (belegt, einfach, robust).

Regel: Halte gleichgewichtet die TOP_N Aktien mit dem staerksten 6-Monats-Momentum
aus einem breiten Large-Cap-Universum. Ziel wird EINMAL pro Monat festgelegt; das
Depot wird dann ueber so viele (stuendliche) Laeufe wie noetig an dieses Monatsziel
angeglichen - Kaeufe immer nur aus tatsaechlich verfuegbarem Cash, damit beim grossen
Umbau keine Order an fehlendem Guthaben scheitert (Verkaeufe muessen erst fuellen).
Ist das Depot = Ziel ("converged"), passiert bis zum naechsten Monat nichts mehr.

Belegt (faire Backtests 2018-26, inkl. COVID + 2022-Crash): schlug den Markt in 7/9
Jahren bei geringerem Drawdown; netto nach DE-Steuer ~+16%/Jahr Vorsprung.
Belege: champion_duell_fair.py, robustness_check.py, strategy_refine.py, tax_check.py.

SICHERHEIT: Default DRY-RUN. Paper-only. Kill-Switch (data/.KILL) respektiert.
Mid-Term (monatlich) - Daytrading bleibt dem Schwesterprojekt daypi.
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

MOM_LOOKBACK = 126
TOP_N = 5
INVEST_PCT = 0.95
MIN_TRADE_EUR = 50
REBAL_BAND = 0.25
_ROOT = Path(__file__).resolve().parents[1]
KILL_FILE = _ROOT / "data" / ".KILL"
STATE_FILE = _ROOT / "data" / ".momentum_state.json"


def momentum_ranking() -> list:
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


def rebalance_to(broker, target: list, live: bool) -> dict:
    """Gleicht das Depot an die Ziel-Liste an. Kaeufe nur aus verfuegbarem Cash."""
    acct = broker.get_account()
    positions = {p.ticker: p for p in broker.get_positions()}
    fx = eur_per_usd()
    target_eur = acct.equity_eur * INVEST_PCT / len(target)
    target_set = set(target)

    sells = [(tk, p) for tk, p in positions.items() if tk not in target_set and p.qty > 0]
    buys = []
    for tk in target:
        cur = positions[tk].market_value_eur if tk in positions else 0.0
        diff = target_eur - cur
        if diff > MIN_TRADE_EUR and diff > target_eur * REBAL_BAND:
            buys.append((tk, diff))

    converged = not sells and not buys
    mode = "LIVE" if live else "PLAN"
    print(f"\n=== Momentum-Rebalance -> Ziel {target} [{mode}] ===")
    print(f"Equity {acct.equity_eur:.0f} EUR | Cash {acct.cash_eur:.0f} EUR | je Position {target_eur:.0f} EUR")
    print("Status: " + ("Depot = Ziel (converged)" if converged else f"{len(sells)} Verkaeufe, {len(buys)} Kaeufe offen"))

    if not live or converged:
        for tk, p in sells: print(f"  VERKAUF {tk:5} ~{p.market_value_eur:.0f} EUR")
        for tk, eur in buys: print(f"  KAUF    {tk:5} ~{eur:.0f} EUR")
        return {"converged": converged, "sells": len(sells), "buys": len(buys), "orders": 0}

    n = 0
    for tk, p in sells:
        try:
            q = broker.get_quote(tk); lp = round(q.last * 0.998, 2) if q.last else None
            r = broker.place_order(ticker=tk, side="sell", qty=p.qty,
                                   order_type="limit" if lp else "market", limit_price=lp)
            print(f"    SELL {tk}: {r.status}"); n += 1
        except Exception as e:
            print(f"    SELL {tk} FEHLER: {e}")
    avail = broker.get_account().cash_eur
    for tk, eur in buys:
        amt = min(eur, avail * 0.98)
        if amt < MIN_TRADE_EUR:
            print(f"    BUY {tk}: aufgeschoben (Cash {avail:.0f} EUR - naechster Lauf kauft nach)")
            continue
        try:
            q = broker.get_quote(tk)
            if not q.last: print(f"    BUY {tk}: keine Quote"); continue
            qty = round(amt / (q.last * fx), 4); lp = round(q.last * 1.002, 2)
            r = broker.place_order(ticker=tk, side="buy", qty=qty, order_type="limit", limit_price=lp)
            print(f"    BUY {tk}: {qty} @ ~{q.last:.2f} ({amt:.0f} EUR) -> {r.status}")
            avail -= amt; n += 1
        except Exception as e:
            print(f"    BUY {tk} FEHLER: {e}")

    try:
        from src.alerts import notifier
        if notifier.is_configured():
            notifier.send_info("Momentum-Rebalance (Spielgeld)\nZiel-Top-5: "
                               + ", ".join(target) + f"\n{len(sells)} raus, {n} Orders",
                               label="momentum_rebalance")
    except Exception:
        pass
    print(f"\n  {n} Orders gesendet.")
    return {"converged": False, "sells": len(sells), "buys": len(buys), "orders": n}


def run_due(broker, dry_run: bool = False, force: bool = False) -> int:
    """Monatsziel 1x/Monat fixieren, dann an Ziel angleichen bis converged."""
    if KILL_FILE.exists():
        print("KILL-SWITCH aktiv - kein Rebalance."); return 0
    today = dt.date.today(); month = today.isoformat()[:7]
    st = _load_state()
    if force or st.get("month") != month or not st.get("target"):
        ranking = momentum_ranking()
        if len(ranking) < TOP_N:
            print(f"Zu wenig Momentum-Daten ({len(ranking)}) - Abbruch."); return 0
        st = {"month": month, "target": [t for t, _ in ranking[:TOP_N]],
              "converged": False, "last_rebalance": st.get("last_rebalance")}
        print(f"Neues Monatsziel ({month}): {st['target']}")
    if st.get("converged") and not force:
        print(f"momentum: Monatsziel {month} bereits erreicht - nichts zu tun."); return 0
    res = rebalance_to(broker, st["target"], live=not dry_run)
    if not dry_run:
        st["converged"] = res["converged"]
        st["last_rebalance"] = today.isoformat()
        _save_state(st)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--mock", action="store_true")
    args = ap.parse_args()
    broker = get_broker("mock" if args.mock else "alpaca_paper")
    if not broker.is_paper and not args.mock:
        print("SICHERHEIT: Broker ist nicht paper - Abbruch."); return 1
    return run_due(broker, dry_run=not args.live, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
