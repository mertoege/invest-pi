#!/usr/bin/env python3
"""
momentum_rebalance.py - Die neue invest-pi-Kernstrategie (belegt, einfach, robust).

Regel: Halte gleichgewichtet die TOP_N Aktien mit dem staerksten 6-Monats-Momentum
aus einem breiten Large-Cap-Universum. Monatsziel 1x/Monat fixieren, dann ueber
mehrere stuendliche Laeufe an das Ziel angleichen (Kaeufe nur aus verfuegbarem Cash).

Produktions-Haertung (Audit 2026-06-24):
- DATEN-SANITY: absurde yfinance-Kurse (Split-Glitches, >200% 6M-Momentum) werden
  verworfen; Top-Kandidaten zusaetzlich gegen die Alpaca-Quote gegengeprueft
  (>15% Abweichung -> raus). Verhindert, dass kaputte Kurse falsche Aktien waehlen.
- MINDESTABDECKUNG: kein Rebalance, wenn <70% des Universums Daten liefern (return 1).
- CIRCUIT-BREAKER: bei >30% Drawdown vom 90-Tage-Hoch -> Kill-Switch + Telegram.
- MARKET-Orders (DAY): fuellen auch fraktional/ausserhalb Kernzeit zuverlaessig.
- Stale offene Orders (>4h) werden abgeraeumt, damit kein Rebalance einfriert.
- State atomar geschrieben.

Belegt (faire Backtests 2018-26): schlug den Markt in 7/9 Jahren, netto nach
DE-Steuer ~+16%/Jahr. SICHERHEIT: Default DRY-RUN. Paper-only. Kill-Switch.
Mid-Term (monatlich) - Daytrading bleibt dem Schwesterprojekt daypi.
"""
from __future__ import annotations
import argparse, datetime as dt, json, os, sys, tempfile
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
from src.common.universe import UNIVERSE

MOM_LOOKBACK = 126
TOP_N = 5
INVEST_PCT = 0.95
MIN_TRADE_EUR = 50
REBAL_BAND = 0.05            # Audit: war 0.25 -> liess bis 24% Cash brachliegen
MAX_DAY_JUMP = 0.45         # Sanity: groesserer Tagessprung = Split/Daten-Glitch
MAX_6M_MOM = 2.00           # Sanity: >200% 6M-Momentum bei Large-Cap = unrealistisch
QUOTE_DEV_MAX = 0.15        # Sanity: yf-Close vs Alpaca-Quote max 15% Abweichung
COVERAGE_MIN = 0.70         # min. Anteil des Universums mit Daten
STALE_HOURS = 4             # offene Orders aelter -> abraeumen
CIRCUIT_DD = -0.30          # Drawdown vom 90d-Hoch -> Notbremse
_ROOT = Path(__file__).resolve().parents[1]
KILL_FILE = _ROOT / "data" / ".KILL"
STATE_FILE = _ROOT / "data" / ".momentum_state.json"


def momentum_ranking() -> list:
    """6M-Momentum je Ticker, mit Daten-Sanity. Return [(ticker, momentum, yf_close)]."""
    out = []
    for tk in UNIVERSE:
        try:
            px = get_prices(tk, period="1y")
            if px is None or len(px) < MOM_LOOKBACK + 1:
                continue
            s = px["close"]
            if float(s.pct_change().abs().max()) > MAX_DAY_JUMP:
                continue  # Split/Glitch
            mom = float(s.iloc[-1] / s.iloc[-MOM_LOOKBACK] - 1)
            if mom > MAX_6M_MOM:
                continue  # unrealistischer Datenmuell
            out.append((tk, mom, float(s.iloc[-1])))
        except Exception:
            continue
    out.sort(key=lambda x: x[1], reverse=True)
    return out


def _pick_top(broker, ranking: list) -> list:
    """Top_N mit Alpaca-Cross-Check: yf-Close stark abweichend von Alpaca -> verwerfen."""
    picked = []
    for tk, mom, yfclose in ranking:
        if len(picked) >= TOP_N:
            break
        try:
            q = broker.get_quote(tk)
            if q.last and yfclose and abs(yfclose / q.last - 1) > QUOTE_DEV_MAX:
                print(f"  SANITY: {tk} verworfen (yf {yfclose:.0f} vs Alpaca {q.last:.0f})")
                continue
        except Exception:
            print(f"  SANITY: {tk} keine Alpaca-Quote - uebersprungen")
            continue
        picked.append(tk)
    return picked


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(d: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(STATE_FILE.parent), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(d, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"  WARN: State nicht gespeichert: {e}")


def _drawdown_from_peak(broker) -> float:
    try:
        from src.common.storage import TRADING_DB, connect
        with connect(TRADING_DB) as c:
            peak = c.execute("SELECT MAX(total_eur) FROM equity_snapshots "
                             "WHERE source='paper' AND timestamp >= datetime('now','-90 day')").fetchone()[0]
        eq = broker.get_account().equity_eur
        if peak and peak > 0:
            return eq / peak - 1
    except Exception:
        pass
    return 0.0


def _circuit_breaker(broker) -> bool:
    """True wenn Notbremse ausgeloest (dann KEIN Rebalance)."""
    dd = _drawdown_from_peak(broker)
    if dd >= CIRCUIT_DD:
        return False
    print(f"CIRCUIT-BREAKER: Drawdown {dd*100:.0f}% vom 90d-Hoch -> Kill-Switch + Alarm")
    try:
        KILL_FILE.write_text(f"circuit-breaker dd {dd*100:.0f}% {dt.date.today().isoformat()}")
    except Exception:
        pass
    try:
        from src.alerts import notifier
        if notifier.is_configured():
            notifier.send_info(f"\U0001F6A8 <b>NOTBREMSE</b>: Depot {dd*100:.0f}% unter 90-Tage-Hoch. "
                               f"Kill-Switch AKTIV - keine neuen Trades. Bitte pruefen.",
                               label="circuit_breaker")
    except Exception:
        pass
    return True


def _open_orders_block(broker) -> bool:
    """Raeumt stale Orders (>4h) ab. True = es laufen noch frische Orders (warten)."""
    try:
        oo = broker.list_orders(status="open")
    except Exception:
        return False
    if not oo:
        return False
    st = _load_state()
    last = st.get("last_order_ts")
    stale = False
    if last:
        try:
            age_h = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last)).total_seconds() / 3600
            stale = age_h > STALE_HOURS
        except Exception:
            stale = True
    else:
        stale = True
    if stale:
        n = 0
        for o in oo:
            try:
                if broker.cancel_order(o.order_id): n += 1
            except Exception:
                pass
        print(f"  {n} stale Orders (>{STALE_HOURS}h) abgeraeumt.")
        return False
    print(f"momentum: {len(oo)} frische Orders offen - warte.")
    return True


def rebalance_to(broker, target: list, live: bool) -> dict:
    """Gleicht Depot an die Ziel-Liste an. MARKET-Orders, Kaeufe nur aus Cash."""
    acct = broker.get_account()
    positions = {p.ticker: p for p in broker.get_positions()}
    target_eur = acct.equity_eur * INVEST_PCT / len(target)
    target_set = set(target)

    sells = [(tk, p) for tk, p in positions.items() if tk not in target_set and p.qty > 0]
    buys = []
    for tk in target:
        cur = positions[tk].market_value_eur if tk in positions else 0.0
        diff = target_eur - cur
        if diff > MIN_TRADE_EUR:                        # Audit: nur MIN_TRADE_EUR, kein Band-Loch
            buys.append((tk, diff))

    converged = not sells and not buys
    print(f"\n=== Momentum-Rebalance -> Ziel {target} [{'LIVE' if live else 'PLAN'}] ===")
    print(f"Equity {acct.equity_eur:.0f} EUR | Cash {acct.cash_eur:.0f} EUR | je Position {target_eur:.0f} EUR")
    print("Status: " + ("Depot = Ziel (converged)" if converged else f"{len(sells)} Verkaeufe, {len(buys)} Kaeufe offen"))

    if not live or converged:
        for tk, p in sells: print(f"  VERKAUF {tk:5} ~{p.market_value_eur:.0f} EUR")
        for tk, eur in buys: print(f"  KAUF    {tk:5} ~{eur:.0f} EUR")
        return {"converged": converged, "orders": 0}

    n = 0
    for tk, p in sells:
        try:
            r = broker.place_order(ticker=tk, side="sell", qty=p.qty, order_type="market")
            print(f"    SELL {tk}: {r.status}"); n += 1
        except Exception as e:
            print(f"    SELL {tk} FEHLER: {e}")
    fx = eur_per_usd()
    avail = broker.get_account().cash_eur
    for tk, eur in buys:
        amt = min(eur, avail * 0.98)
        if amt < MIN_TRADE_EUR:
            print(f"    BUY {tk}: aufgeschoben (Cash {avail:.0f} EUR - naechster Lauf)")
            continue
        try:
            q = broker.get_quote(tk)
            if not q.last: print(f"    BUY {tk}: keine Quote"); continue
            qty = round(amt / (q.last * fx), 4)
            r = broker.place_order(ticker=tk, side="buy", qty=qty, order_type="market")
            print(f"    BUY {tk}: {qty} ({amt:.0f} EUR) -> {r.status}")
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
    return {"converged": False, "orders": n}


def run_due(broker, dry_run: bool = False, force: bool = False) -> int:
    if KILL_FILE.exists():
        print("KILL-SWITCH aktiv - kein Rebalance."); return 0
    if not dry_run and _circuit_breaker(broker):
        return 0
    if not dry_run and _open_orders_block(broker):
        return 0
    today = dt.date.today(); month = today.isoformat()[:7]
    st = _load_state()
    if force or st.get("month") != month or not st.get("target"):
        ranking = momentum_ranking()
        coverage = len(ranking) / max(1, len(UNIVERSE))
        if coverage < COVERAGE_MIN:
            print(f"FEHLER: Datenabdeckung nur {coverage:.0%} (<{COVERAGE_MIN:.0%}) - kein Rebalance auf lueckenhaften Daten.")
            return 1
        top = _pick_top(broker, ranking)
        if len(top) < TOP_N:
            print(f"FEHLER: nur {len(top)} saubere Kandidaten nach Sanity-Check - Abbruch.")
            return 1
        st = {"month": month, "target": top, "converged": False,
              "last_rebalance": st.get("last_rebalance"), "last_order_ts": st.get("last_order_ts")}
        print(f"Neues Monatsziel ({month}): {top}")
    if st.get("converged") and not force:
        print(f"momentum: Monatsziel {month} bereits erreicht - nichts zu tun."); return 0
    res = rebalance_to(broker, st["target"], live=not dry_run)
    if not dry_run:
        st["converged"] = res["converged"]
        st["last_rebalance"] = today.isoformat()
        if res.get("orders", 0) > 0:
            st["last_order_ts"] = dt.datetime.now(dt.timezone.utc).isoformat()
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
