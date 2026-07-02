#!/usr/bin/env python3
"""
ai_swing_execute.py - Execution-Adapter fuer den "AI Swing Trader" (Phase 3, Live-Paper).

Was das Skript tut (einfach gesagt): Es nimmt die JUENGSTE KI-Wochenentscheidung aus
data/ai_swing.db (die "shadow"-Empfehlung), baut daraus einen gleichgewichteten Korb
und gleicht ein ZWEITES Alpaca-Paper-Konto (KEY_2/SECRET_2) an dieses Ziel an. Das ist
strikt getrennt vom Momentum-System: alle Ledger-Eintraege tragen source="ai_swing",
strategy_label="ai_swing-v1" - NIE "paper", sonst wuerde das Momentum-Ledger verschmutzt.

Sicherheits-Kaefig (ALLE Zahlen leben im CODE, die KI liefert KEINE):
  1. KILL-SWITCH: existiert .KILL_ai -> keine Order, sauberer Exit.
  2. WHITELIST-GATE: jeder Ziel-Ticker MUSS in candidates_json der Entscheidung stehen
     (haluzinierte/fremde Ticker werden verworfen + geloggt).
  3. KORB: picks der juengsten Entscheidung; bevorzugt high+medium. Ergibt das <3 Namen,
     kommt low dazu. Gekappt auf MAX_POSITIONS. Gleichgewichtet.
  4. SIZING (deterministisch): je Name = equity * INVEST_PCT / len(korb), hart gekappt auf
     equity * MAX_POS_PCT. Kaeufe NUR aus Cash (nie Margin). Passt ein Kauf nicht in den
     Cash, wird er verkleinert. >=5% Cash-Puffer bleibt stehen.
  5. STATE-VERIFIER: broker2.get_positions() ist die Wahrheit VOR dem Ordern. Ticker nicht
     mehr im Korb -> voll verkaufen. Ticker in beiden -> mit 20%-No-Trade-Band angleichen.
     Fehlende Korb-Ticker -> bis zum Ziel kaufen.
  6. Nur MARKET-Orders. Stueckzahl auf 4 Nachkommastellen (get_quote(.last) * FX).
  7. Jede Order (filled/failed/canceled) -> _log_trade mit source="ai_swing".
  8. --dry-run: kompletter Plan wird GEDRUCKT, nichts gesendet. Ohne Flag = ECHT.
     Idempotent: erneuter Lauf konvergiert, kauft nicht blind nach.

Vorlage/Stil abgeschaut von scripts/momentum_rebalance.py - NICHT geforkt (eigene Logik,
eigenes Konto, eigene Ledger-Quelle).
"""
from __future__ import annotations
import argparse, datetime as dt, json, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_ROOT = Path(__file__).resolve().parents[1]
_env = _ROOT / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from src.broker import get_broker
from src.common.fx import eur_per_usd
from src.common.storage import TRADING_DB, connect

# ─────────────────────────────────────────────────────────────
# SICHERHEITS-KAEFIG (Konstanten - die KI liefert keine Zahlen)
# ─────────────────────────────────────────────────────────────
INVEST_PCT     = 0.95     # hoechstens 95% des Equity einsetzen; >=5% Cash-Puffer
MAX_POSITIONS  = 12       # Korb-Groesse deckeln
MAX_POS_PCT    = 0.15     # harte Kappe: kein Name >15% des Equity
NO_TRADE_BAND  = 0.20     # Drift <20% -> nicht anfassen (Churn vermeiden)
KILL_FILE      = _ROOT / ".KILL_ai"
AI_SWING_DB    = _ROOT / "data" / "ai_swing.db"
SOURCE         = "ai_swing"
STRATEGY_LABEL = "ai_swing-v1"
CONVICTION_RANK = {"high": 0, "medium": 1, "low": 2}


# ─────────────────────────────────────────────────────────────
# ENTSCHEIDUNG LADEN (juengste shadow-Zeile = aktuelles Ziel)
# ─────────────────────────────────────────────────────────────
def load_latest_decision() -> dict | None:
    """Juengste shadow-Entscheidung + ihre picks + Whitelist (candidates_json).
    Return dict{decision_id, picks:[(ticker,conviction)], whitelist:set} oder None."""
    try:
        with connect(AI_SWING_DB) as conn:
            row = conn.execute(
                "SELECT id, created_at, candidates_json FROM decisions "
                "WHERE mode='shadow' ORDER BY created_at DESC, id DESC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            dec_id = row["id"]
            try:
                whitelist = {str(t).strip().upper() for t in json.loads(row["candidates_json"] or "[]")}
            except Exception:
                whitelist = set()
            picks = conn.execute(
                "SELECT ticker, conviction FROM picks WHERE decision_id=? ", (dec_id,)
            ).fetchall()
            return {
                "decision_id": dec_id,
                "created_at": row["created_at"],
                "picks": [(p["ticker"].strip().upper(), (p["conviction"] or "low").strip().lower())
                          for p in picks],
                "whitelist": whitelist,
            }
    except Exception as e:
        print(f"FEHLER: Entscheidung nicht ladbar: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# KORB BAUEN (Whitelist-Gate + Conviction-Filter + Cap)
# ─────────────────────────────────────────────────────────────
def build_basket(decision: dict) -> list:
    """Return sortierte, gleichgewichtete Ticker-Liste (nach Conviction), <= MAX_POSITIONS.
    Verwirft Picks, die NICHT in der Whitelist stehen (Halluzinations-Schutz)."""
    whitelist = decision["whitelist"]
    clean = []
    seen = set()
    for tk, conv in decision["picks"]:
        if whitelist and tk not in whitelist:
            print(f"  WHITELIST: {tk} verworfen (nicht in candidates_json der Entscheidung)")
            continue
        if tk in seen:
            continue
        seen.add(tk)
        clean.append((tk, conv))

    # Bevorzugt high+medium; reicht das nicht fuer >=3 Namen, low dazunehmen.
    strong = [(tk, c) for tk, c in clean if c in ("high", "medium")]
    basket = strong if len(strong) >= 3 else clean
    # nach Conviction stabil sortieren, dann auf MAX_POSITIONS kappen
    basket.sort(key=lambda x: CONVICTION_RANK.get(x[1], 9))
    tickers = [tk for tk, _ in basket][:MAX_POSITIONS]
    return tickers


# ─────────────────────────────────────────────────────────────
# LEDGER (spiegelt momentum_rebalance._log_trade, aber source="ai_swing")
# ─────────────────────────────────────────────────────────────
def _log_trade(ticker, side, qty, eur_value, price, status, order_id):
    """Jede AI-Swing-Order ins gemeinsame trades-Ledger - IMMER source='ai_swing',
    strategy_label='ai_swing-v1'. Niemals 'paper' (das gehoert dem Momentum-System)."""
    try:
        with connect(TRADING_DB) as conn:
            conn.execute(
                "INSERT INTO trades (ticker, side, qty, eur_value, price, order_type, "
                "status, broker_order_id, strategy_label, source) "
                "VALUES (?,?,?,?,?, 'market', ?,?, ?, ?)",
                (ticker, side, qty, eur_value, price, status, order_id, STRATEGY_LABEL, SOURCE))
    except Exception as e:
        print(f"    WARN: trade-log {ticker} fehlgeschlagen: {e}")


# ─────────────────────────────────────────────────────────────
# PLAN BERECHNEN (deterministisches Sizing + State-Verifier)
# ─────────────────────────────────────────────────────────────
def compute_plan(broker, basket: list) -> dict:
    """Baut aus Ziel-Korb + Broker-Wahrheit den Order-Plan. Return dict mit
    equity, target_eur, sells[(tk,qty,eur)], buys[(tk,eur)], trims[(tk,qty,eur)], skips[]."""
    acct = broker.get_account()
    equity = acct.equity_eur
    positions = {p.ticker: p for p in broker.get_positions()}
    target_set = set(basket)

    # Sizing: gleichgewichtet, dann harte Einzel-Kappe.
    per_name = equity * INVEST_PCT / len(basket) if basket else 0.0
    per_name = min(per_name, equity * MAX_POS_PCT)
    target_eur = {tk: per_name for tk in basket}

    sells, buys, trims, skips = [], [], [], []

    # 1. Halten, aber NICHT im neuen Korb -> voll verkaufen.
    for tk, p in positions.items():
        if tk not in target_set and p.qty > 0:
            sells.append((tk, p.qty, p.market_value_eur))

    # 2. Korb-Ticker: gegen Ziel angleichen (20%-No-Trade-Band).
    for tk in basket:
        cur = positions[tk].market_value_eur if tk in positions else 0.0
        tgt = target_eur[tk]
        drift = (cur - tgt) / tgt if tgt > 0 else 0.0
        if abs(drift) < NO_TRADE_BAND:
            skips.append((tk, cur, tgt))          # innerhalb Band -> nichts tun
            continue
        if cur < tgt:
            buys.append((tk, tgt - cur))          # untergewichtet -> kaufen
        else:
            p = positions[tk]
            over = cur - tgt
            qty = round(over * p.qty / p.market_value_eur, 4) if p.market_value_eur > 0 else 0.0
            if qty > 0:
                trims.append((tk, qty, over))     # uebergewichtet -> trimmen

    return {
        "equity": equity, "cash": acct.cash_eur, "per_name": per_name,
        "target_eur": target_eur, "sells": sells, "buys": buys,
        "trims": trims, "skips": skips,
    }


# ─────────────────────────────────────────────────────────────
# PLAN AUSGEBEN
# ─────────────────────────────────────────────────────────────
def print_plan(basket, plan, live):
    tag = "LIVE" if live else "DRY-RUN"
    print(f"\n=== AI-Swing-Execute -> Korb {basket} [{tag}] ===")
    print(f"Equity {plan['equity']:.0f} EUR | Cash {plan['cash']:.0f} EUR | "
          f"je Position {plan['per_name']:.0f} EUR (Kappe {MAX_POS_PCT*100:.0f}% = "
          f"{plan['equity']*MAX_POS_PCT:.0f} EUR)")
    if plan["sells"]:
        for tk, qty, eur in plan["sells"]:
            print(f"  VERKAUF {tk:5} voll {qty} (~{eur:.0f} EUR) - nicht mehr im Korb")
    if plan["trims"]:
        for tk, qty, eur in plan["trims"]:
            print(f"  TRIM    {tk:5} -{qty} (~{eur:.0f} EUR ueber Ziel)")
    if plan["buys"]:
        for tk, eur in plan["buys"]:
            print(f"  KAUF    {tk:5} ~{eur:.0f} EUR")
    if plan["skips"]:
        for tk, cur, tgt in plan["skips"]:
            print(f"  HALTEN  {tk:5} ~{cur:.0f} EUR (Ziel {tgt:.0f}, im 20%-Band)")
    if not (plan["sells"] or plan["trims"] or plan["buys"]):
        print("  Depot = Ziel (converged) - nichts zu tun.")


# ─────────────────────────────────────────────────────────────
# ORDERS AUSFUEHREN
# ─────────────────────────────────────────────────────────────
def execute_plan(broker, plan) -> dict:
    """Sendet Sells -> Trims -> Buys. Jede Order in try/except, weiterlaufen, am Ende
    Fehler zusammenfassen. Kaeufe nur aus laufend nachgezogenem Cash."""
    n_ok, n_fail = 0, 0
    fails = []

    # SELLS (voll)
    for tk, qty, eur in plan["sells"]:
        try:
            r = broker.place_order(ticker=tk, side="sell", qty=qty, order_type="market")
            print(f"    SELL {tk}: {qty} -> {r.status}")
            _log_trade(tk, "sell", qty, eur, None, r.status, r.order_id)
            n_ok += 1
        except Exception as e:
            print(f"    SELL {tk} FEHLER: {e}"); fails.append(f"sell {tk}: {e}"); n_fail += 1

    # TRIMS (Uebergewicht zurueckschneiden)
    for tk, qty, eur in plan["trims"]:
        try:
            r = broker.place_order(ticker=tk, side="sell", qty=qty, order_type="market")
            print(f"    TRIM {tk}: -{qty} ({eur:.0f} EUR) -> {r.status}")
            _log_trade(tk, "sell", qty, eur, None, r.status, r.order_id)
            n_ok += 1
        except Exception as e:
            print(f"    TRIM {tk} FEHLER: {e}"); fails.append(f"trim {tk}: {e}"); n_fail += 1

    # BUYS (nur aus Cash; Cash nach jedem Kauf frisch ziehen)
    fx = eur_per_usd()
    try:
        avail = broker.get_account().cash_eur
    except Exception:
        avail = plan["cash"]
    cash_floor = plan["equity"] * (1 - INVEST_PCT)   # >=5% Puffer stehen lassen
    for tk, eur in plan["buys"]:
        spendable = max(0.0, avail - cash_floor)
        amt = min(eur, spendable)
        if amt <= 0:
            print(f"    BUY {tk}: aufgeschoben (Cash {avail:.0f} EUR unter Puffer - naechster Lauf)")
            continue
        try:
            q = broker.get_quote(tk)
            if not q.last:
                print(f"    BUY {tk}: keine Quote - uebersprungen"); continue
            qty = round(amt / (q.last * fx), 4)
            if qty <= 0:
                print(f"    BUY {tk}: Menge 0 - uebersprungen"); continue
            r = broker.place_order(ticker=tk, side="buy", qty=qty, order_type="market")
            print(f"    BUY {tk}: {qty} ({amt:.0f} EUR) -> {r.status}")
            _log_trade(tk, "buy", qty, amt, q.last, r.status, r.order_id)
            avail -= amt; n_ok += 1
        except Exception as e:
            print(f"    BUY {tk} FEHLER: {e}"); fails.append(f"buy {tk}: {e}"); n_fail += 1

    return {"ok": n_ok, "fail": n_fail, "fails": fails}


# ─────────────────────────────────────────────────────────────
# HAUPTLAUF
# ─────────────────────────────────────────────────────────────
def run(dry_run: bool) -> int:
    # 1. KILL-SWITCH
    if KILL_FILE.exists():
        reason = ""
        try:
            reason = KILL_FILE.read_text().strip()
        except Exception:
            pass
        print(f"KILL-SWITCH ({KILL_FILE.name}) aktiv - keine Order. {reason}")
        return 0

    # 2. Entscheidung laden
    decision = load_latest_decision()
    if not decision:
        print("Keine shadow-Entscheidung in ai_swing.db gefunden - nichts zu tun.")
        return 0
    print(f"Juengste Entscheidung #{decision['decision_id']} ({decision['created_at']}), "
          f"{len(decision['picks'])} picks, Whitelist {len(decision['whitelist'])} Ticker.")

    # 3. Korb bauen (Whitelist-Gate + Conviction)
    basket = build_basket(decision)
    if not basket:
        print("Leerer Korb nach Whitelist-/Conviction-Filter - keine Order.")
        return 0
    print(f"Korb ({len(basket)}): {basket}")

    # 4. Broker2 (2. Paper-Konto) bauen
    key2 = os.environ.get("ALPACA_API_KEY_2")
    sec2 = os.environ.get("ALPACA_API_SECRET_2")
    if not key2 or not sec2:
        print("FEHLER: ALPACA_API_KEY_2 / ALPACA_API_SECRET_2 fehlen in .env - Abbruch.")
        return 1
    broker2 = get_broker("alpaca_paper", api_key=key2, api_secret=sec2)

    # 5. Plan berechnen (State-Verifier holt Positionen als Wahrheit)
    try:
        plan = compute_plan(broker2, basket)
    except Exception as e:
        print(f"FEHLER: Plan-Berechnung fehlgeschlagen (Broker2 erreichbar?): {e}")
        return 1

    print_plan(basket, plan, live=not dry_run)

    # 6. Dry-Run: nichts senden.
    if dry_run:
        print("\n[DRY-RUN] Es wurde NICHTS gesendet.")
        return 0

    # 7. Echt ausfuehren
    res = execute_plan(broker2, plan)
    print(f"\n  {res['ok']} Orders ok, {res['fail']} Fehler.")
    if res["fails"]:
        print("  Fehlerdetails:")
        for f in res["fails"]:
            print(f"    - {f}")

    # 8. Optionaler Telegram-Hinweis (still bleiben wenn nicht konfiguriert)
    try:
        from src.alerts import notifier
        if notifier.is_configured():
            notifier.send_info("AI-Swing-Execute (2. Paper-Konto)\nKorb: "
                               + ", ".join(basket) + f"\n{res['ok']} Orders, {res['fail']} Fehler",
                               label="ai_swing")
    except Exception:
        pass
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="Plan nur DRUCKEN, nichts senden (Default ohne Flag = ECHT).")
    args = ap.parse_args()
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
