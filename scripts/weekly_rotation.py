#!/usr/bin/env python3
"""
weekly_rotation.py — Wöchentliche Portfolio-Rotation.

Logik:
  1. Alle gehaltenen Positionen bewerten (Risk-Score + Performance)
  2. Die N schwächsten verkaufen (hoher Risk-Score + schlechte Performance)
  3. Die N besten nicht-gehaltenen Kandidaten kaufen (niedriger Risk-Score)
  4. Bestehende Positionen die weit unter target_eur liegen aufstocken (Top-Up)

Ziele:
  - Portfolio aktiv halten statt passiv Buy-and-Hold
  - Self-Learning-Loop füttern mit Sell-Daten
  - Kapital effizienter einsetzen

Timer: Samstag (Markt zu, Orders werden Montag pre-market gefüllt)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.broker import get_broker
from src.alerts import notifier
from src.common import config as cfg_mod
from src.common.fx import eur_per_usd
from src.common.storage import TRADING_DB, connect, init_all
from src.trading import TradingConfig, load_trading_config, get_active_profile
from src.trading.decision import latest_risk_score

# Rotation-Parameter
MAX_SELLS_PER_WEEK = 3
MAX_TOPUPS_PER_RUN = 5
TOPUP_THRESHOLD_PCT = 0.40  # Position aufstocken wenn < 40% von target


def _record_trade(*, ticker, side, qty, eur_value, price, status, order_id, strategy_label, source, notes=""):
    import datetime as dt
    with connect(TRADING_DB) as conn:
        conn.execute(
            """INSERT INTO trades
                (ticker, side, qty, eur_value, price, order_type, status,
                 broker_order_id, strategy_label, prediction_id,
                 fill_ts, fill_price, source, notes)
            VALUES (?, ?, ?, ?, ?, 'market', ?, ?, ?, NULL, ?, ?, ?, ?)""",
            (ticker, side, qty, eur_value, price, status, order_id,
             strategy_label,
             dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds") if status == "filled" else None,
             price if status == "filled" else None,
             source, notes),
        )


def score_position(ticker: str, pct_change: float) -> float:
    """
    Composite score für Rotation: höher = schlechter (sell-Kandidat).
    Kombiniert Risk-Score (0-100) + inverse Performance.
    """
    risk = latest_risk_score(ticker)
    risk_composite = risk["composite"] if risk else 50.0
    # Performance-Malus: negative Performance erhöht Score
    perf_penalty = max(0, -pct_change * 2)  # -5% → +10 Punkte
    # Bonus für gute Performance (senkt Score)
    perf_bonus = max(0, pct_change * 0.5)  # +10% → -5 Punkte
    return risk_composite + perf_penalty - perf_bonus


def rotation_pass(broker, cfg, t_cfg, profile, source, dry_run) -> dict:
    """Verkaufe schwächste Positionen, kaufe bessere Kandidaten."""
    positions = broker.get_positions()
    if not positions:
        return {"sells": [], "buys": []}

    fx = eur_per_usd()
    target_eur = profile.get("max_position_eur", t_cfg.max_position_eur)

    # Score all held positions
    scored = []
    for p in positions:
        pct = ((p.market_price - p.avg_price) / p.avg_price * 100) if p.avg_price else 0
        score = score_position(p.ticker, pct)
        scored.append({"pos": p, "score": score, "pct": pct})

    scored.sort(key=lambda x: x["score"], reverse=True)  # Worst first

    # Sell worst N if their score is bad enough
    sells = []
    for item in scored[:MAX_SELLS_PER_WEEK]:
        p = item["pos"]
        # Only sell if score is actually bad (>40) or performance is negative
        if item["score"] < 40 and item["pct"] > -3:
            continue
        print(f"  ROTATION-SELL {p.ticker}: score={item['score']:.1f}, perf={item['pct']:+.1f}%, "
              f"value={p.market_value_eur:.0f}€")
        if dry_run:
            sells.append({"ticker": p.ticker, "score": item["score"], "dry_run": True})
            continue
        result = broker.place_order(ticker=p.ticker, side="sell", qty=p.qty)
        _record_trade(
            ticker=p.ticker, side="sell", qty=p.qty,
            eur_value=p.market_value_eur, price=p.market_price,
            status=result.status, order_id=result.order_id,
            strategy_label="rotation-sell-v1", source=source,
            notes=f"weekly rotation: score={item['score']:.1f} perf={item['pct']:+.1f}%",
        )
        sells.append({"ticker": p.ticker, "score": item["score"], "status": result.status})
        if result.status in ("filled", "pending_new"):
            try:
                notifier.send_trade(
                    ticker=p.ticker, side="sell", qty=p.qty,
                    eur=p.market_value_eur, price_usd=p.market_price,
                    reason=f"Rotation: Score {item['score']:.0f}, Perf {item['pct']:+.1f}%",
                    paper=broker.is_paper,
                )
            except Exception:
                pass

    # Buy best non-held candidates
    held = {p.ticker for p in positions} - {s["ticker"] for s in sells if not s.get("dry_run")}
    candidates = [e for e in cfg.universe if e.ring in t_cfg.tradeable_rings and e.ticker not in held]

    # Score candidates (lower = better buy)
    buy_candidates = []
    for entry in candidates:
        risk = latest_risk_score(entry.ticker)
        if not risk:
            continue
        if risk["alert_level"] >= t_cfg.force_skip_alert_min:
            continue
        buy_candidates.append({"entry": entry, "risk": risk["composite"], "alert": risk["alert_level"]})

    buy_candidates.sort(key=lambda x: x["risk"])
    buys = []

    for cand in buy_candidates[:len(sells)]:  # Buy as many as we sold
        entry = cand["entry"]
        quote = broker.get_quote(entry.ticker)
        eur_amount = min(target_eur * 0.6, broker.get_account().cash_eur * 0.15)
        if eur_amount < t_cfg.min_position_eur:
            continue
        price_eur = quote.last * fx
        qty = round(eur_amount / price_eur, 4)

        print(f"  ROTATION-BUY {entry.ticker}: risk={cand['risk']:.1f}, "
              f"{qty} @ ${quote.last:.2f} = {eur_amount:.0f}€")
        if dry_run:
            buys.append({"ticker": entry.ticker, "risk": cand["risk"], "dry_run": True})
            continue
        result = broker.place_order(ticker=entry.ticker, side="buy", qty=qty)
        _record_trade(
            ticker=entry.ticker, side="buy", qty=qty,
            eur_value=eur_amount, price=quote.last,
            status=result.status, order_id=result.order_id,
            strategy_label="rotation-buy-v1", source=source,
            notes=f"weekly rotation: risk={cand['risk']:.1f}",
        )
        buys.append({"ticker": entry.ticker, "risk": cand["risk"], "status": result.status})
        if result.status in ("filled", "pending_new"):
            try:
                notifier.send_trade(
                    ticker=entry.ticker, side="buy", qty=qty,
                    eur=eur_amount, price_usd=quote.last,
                    reason=f"Rotation-Buy: Risk {cand['risk']:.0f}",
                    paper=broker.is_paper,
                )
            except Exception:
                pass

    return {"sells": sells, "buys": buys}


def topup_pass(broker, t_cfg, profile, source, dry_run) -> list:
    """Stocke untergewichtete Positionen auf target_eur auf."""
    positions = broker.get_positions()
    fx = eur_per_usd()
    target_eur = profile.get("max_position_eur", t_cfg.max_position_eur)
    threshold = target_eur * TOPUP_THRESHOLD_PCT

    # Find underweight positions with good risk scores
    underweight = []
    for p in positions:
        if p.market_value_eur >= threshold:
            continue
        risk = latest_risk_score(p.ticker)
        if not risk or risk["alert_level"] >= t_cfg.force_skip_alert_min:
            continue
        gap_eur = target_eur * 0.6 - p.market_value_eur  # Top up to 60% of target
        if gap_eur < t_cfg.min_position_eur:
            continue
        underweight.append({"pos": p, "gap_eur": gap_eur, "risk": risk["composite"]})

    underweight.sort(key=lambda x: x["risk"])  # Best risk first
    topups = []

    for item in underweight[:MAX_TOPUPS_PER_RUN]:
        p = item["pos"]
        gap = min(item["gap_eur"], broker.get_account().cash_eur * 0.10)
        if gap < t_cfg.min_position_eur:
            continue
        quote = broker.get_quote(p.ticker)
        price_eur = quote.last * fx
        qty = round(gap / price_eur, 4)

        print(f"  TOP-UP {p.ticker}: {p.market_value_eur:.0f}€ → +{gap:.0f}€ "
              f"(target {target_eur:.0f}€, risk={item['risk']:.1f})")
        if dry_run:
            topups.append({"ticker": p.ticker, "gap_eur": gap, "dry_run": True})
            continue
        result = broker.place_order(ticker=p.ticker, side="buy", qty=qty)
        _record_trade(
            ticker=p.ticker, side="buy", qty=qty,
            eur_value=gap, price=quote.last,
            status=result.status, order_id=result.order_id,
            strategy_label="topup-v1", source=source,
            notes=f"position top-up from {p.market_value_eur:.0f}€",
        )
        topups.append({"ticker": p.ticker, "gap_eur": gap, "status": result.status})
        if result.status in ("filled", "pending_new"):
            try:
                notifier.send_trade(
                    ticker=p.ticker, side="buy", qty=qty,
                    eur=gap, price_usd=quote.last,
                    reason=f"Top-Up: {p.market_value_eur:.0f}€ → {p.market_value_eur+gap:.0f}€",
                    paper=broker.is_paper,
                )
            except Exception:
                pass

    return topups


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Nur simulieren, keine echten Orders")
    parser.add_argument("--skip-rotation", action="store_true", help="Nur Top-Up, keine Rotation")
    parser.add_argument("--skip-topup", action="store_true", help="Nur Rotation, kein Top-Up")
    args = parser.parse_args()

    init_all()
    cfg = cfg_mod.load()
    t_cfg = load_trading_config()
    profile = get_active_profile(t_cfg)

    broker = get_broker(t_cfg.broker)
    source = "paper" if broker.is_paper else "live"

    print(f"\n=== weekly_rotation · broker={broker} · mode={t_cfg.mode} · source={source} ===")
    print(f"  profile target: {profile.get('max_position_eur', t_cfg.max_position_eur)}€/pos")

    results = {}

    if not args.skip_topup:
        print("\n── Top-Up Pass ──")
        topups = topup_pass(broker, t_cfg, profile, source, args.dry_run)
        results["topups"] = topups
        print(f"  → {len(topups)} top-ups")

    if not args.skip_rotation:
        print("\n── Rotation Pass ──")
        rotation = rotation_pass(broker, cfg, t_cfg, profile, source, args.dry_run)
        results["rotation"] = rotation
        print(f"  → {len(rotation['sells'])} sells, {len(rotation['buys'])} buys")

    # Summary notification
    if not args.dry_run and (results.get("topups") or results.get("rotation", {}).get("sells")):
        try:
            parts = []
            if results.get("topups"):
                parts.append(f"Top-Ups: {len(results['topups'])}")
            rot = results.get("rotation", {})
            if rot.get("sells"):
                parts.append(f"Rotation: {len(rot['sells'])} sells → {len(rot.get('buys', []))} buys")
            notifier.send_message(f"📊 Weekly Rotation\n{chr(10).join(parts)}")
        except Exception:
            pass

    print("\n  Done.")


if __name__ == "__main__":
    main()
