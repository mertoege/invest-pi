#!/usr/bin/env python3
"""
daily_report.py — Taeglicher Performance-Report via Telegram.

Wird nach US-Marktschluss (21:30 CEST / 19:30 UTC) aufgerufen.
Format: Telegram-Push mit:
  - Trades heute (count + Volumen)
  - Equity heute vs gestern (USD + EUR + FX)
  - Offene Positionen mit PnL
  - Cost-Verbrauch heute / Monat
  - Pending outcomes die morgen messbar werden
  - Drift-Status

Sonntags zusaetzlich:
  - Wochen-Hit-Rate stratifiziert
  - Top-3 best/schlecht performende Positionen
  - Strategy-Vergleich (conservative vs moderate, falls beide laufen)
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# .env laden
env_path = Path(__file__).resolve().parents[1] / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from src.alerts import notifier
from src.common.outcomes import detect_drift
from src.common.performance import compute_metrics, format_metrics
from src.learning.attribution import attribution_block
from src.common.predictions import hit_rate, hit_rate_stratified
from src.common.storage import LEARNING_DB, TRADING_DB, connect


def _gather_today() -> dict:
    """Sammelt alle heute-relevanten Daten."""
    out = {}
    # Trades heute
    with connect(TRADING_DB) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n, COALESCE(SUM(eur_value), 0) AS volume
              FROM trades
             WHERE date(created_at, 'localtime') = date('now', 'localtime')
               AND status = 'filled'
            """
        ).fetchone()
        out["trades_today"]   = int(row["n"])
        out["volume_today"]   = float(row["volume"])

        # Equity heute (latest) vs gestern (last before today)
        row_now = conn.execute(
            """
            SELECT total_eur, total_usd, fx_rate, cash_eur, positions_value_eur
              FROM equity_snapshots
             WHERE source = 'paper'
             ORDER BY timestamp DESC LIMIT 1
            """
        ).fetchone()
        row_yesterday = conn.execute(
            """
            SELECT total_eur, total_usd
              FROM equity_snapshots
             WHERE source = 'paper'
               AND date(timestamp, 'localtime') < date('now', 'localtime')
             ORDER BY timestamp DESC LIMIT 1
            """
        ).fetchone()
        out["equity_now"]    = dict(row_now) if row_now else None
        out["equity_yest"]   = dict(row_yesterday) if row_yesterday else None

        # Open positions
        positions = conn.execute(
            "SELECT ticker, qty, avg_price_eur, peak_price FROM positions WHERE source='paper'"
        ).fetchall()
        out["positions"] = [dict(p) for p in positions]

    # Cost heute + Monat
    with connect(LEARNING_DB) as conn:
        row = conn.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN date(timestamp,'localtime') = date('now','localtime')
                            THEN cost_eur ELSE 0 END), 0) AS today,
              COALESCE(SUM(CASE WHEN strftime('%Y-%m', timestamp,'localtime') = strftime('%Y-%m','now','localtime')
                            THEN cost_eur ELSE 0 END), 0) AS this_month
              FROM cost_ledger
            """
        ).fetchone()
        out["cost_today"]      = float(row["today"])
        out["cost_this_month"] = float(row["this_month"])

        # Pending outcomes (daily_score predictions die T+1d/7d-messbar werden bald)
        pending_rows = conn.execute(
            """
            SELECT COUNT(*) AS n
              FROM predictions
             WHERE job_source = 'daily_score'
               AND outcome_correct IS NULL
               AND outcome_json IS NULL
               AND created_at <= datetime('now', '-1 day')
            """
        ).fetchone()
        out["pending_t1d"] = int(pending_rows["n"])

    return out


def _build_daily_msg(data: dict) -> str:
    """HTML-Telegram-Message fuer den Tagesreport."""
    parts = ["🌙 <b>Daily Report</b>"]

    # Trades
    if data["trades_today"] > 0:
        parts.append(f"📊 Trades: <b>{data['trades_today']}</b> ({data['volume_today']:.0f} EUR Volumen)")
    else:
        parts.append("📊 Trades: <b>0</b> heute")

    # Equity
    now = data.get("equity_now")
    yest = data.get("equity_yest")
    if now:
        parts.append(f"💰 Equity: <b>{now['total_eur']:.0f} EUR</b> · {now['total_usd'] or 0:.0f} USD · FX {now['fx_rate'] or 0:.4f}")
        if yest and yest.get("total_usd"):
            delta_usd = now["total_usd"] - yest["total_usd"]
            delta_pct = (delta_usd / yest["total_usd"]) * 100 if yest["total_usd"] else 0
            sign = "+" if delta_usd >= 0 else ""
            parts.append(f"   24h: {sign}{delta_usd:.0f} USD ({sign}{delta_pct:.2f}%) — FX-bereinigt")

    # Positions
    n_pos = len(data.get("positions", []))
    if n_pos > 0:
        parts.append(f"📈 Open: <b>{n_pos}</b> Positionen")
        for p in data["positions"][:5]:
            peak_str = f" peak {p['peak_price']:.2f}" if p.get("peak_price") else ""
            parts.append(f"   • {escape(p['ticker'])}: {p['qty']} @ {p['avg_price_eur'] or 0:.2f} EUR{peak_str}")
    else:
        parts.append("📈 Open: keine Positionen")

    # Cost
    parts.append(
        f"💸 Cost: <b>{data['cost_today']:.3f} EUR</b> heute · "
        f"{data['cost_this_month']:.2f} EUR diesen Monat"
    )

    # Pending outcomes
    if data["pending_t1d"] > 0:
        parts.append(f"⏳ Pending T+1d: {data['pending_t1d']} predictions")

    # Performance-Metriken (nur wenn genug Daten)
    metrics = compute_metrics(source="paper", days=30)
    if metrics.n_observations >= 2:
        parts.append("")
        parts.append(format_metrics(metrics))

    return "\n".join(parts)


def _build_weekly_msg() -> str:
    """Sonntags-Report mit Hit-Rate-Details."""
    parts = ["📅 <b>Weekly Report</b>"]

    # Hit-Rate fuer alle 3 sources
    for source in ("daily_score", "trade_decision", "monthly_dca"):
        rates = hit_rate_stratified(source, days=7)
        o = rates["overall"]
        if o["measured"] == 0:
            continue
        parts.append(f"\n<b>{source}</b>")
        parts.append(
            f"  Total: {o['total']} · measured: {o['measured']} · "
            f"hit-rate {(o['hit_rate'] or 0):.0%}"
        )
        for level in ("high", "medium", "low"):
            s = rates[level]
            if s["measured"] > 0:
                parts.append(
                    f"  {level}: {s['correct']}/{s['measured']} "
                    f"({(s['hit_rate'] or 0):.0%})"
                )

    # Drift
    drift = detect_drift("daily_score", window_days=7)
    if drift:
        parts.append(f"\n⚠ Drift: {escape(drift['message'])}")

    # Attribution
    attrib = attribution_block("daily_score", days=7)
    if attrib:
        parts.append("")
        parts.append(attrib)

    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["daily", "weekly", "auto"], default="auto",
                        help="auto: weekly am Sonntag, sonst daily")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print message statt Telegram-Push")
    args = parser.parse_args()

    if not notifier.is_configured() and not args.dry_run:
        print("Telegram nicht konfiguriert, daily_report skipped")
        return 0

    is_sunday = dt.datetime.now().weekday() == 6
    do_weekly = args.mode == "weekly" or (args.mode == "auto" and is_sunday)

    daily_data = _gather_today()
    daily_msg = _build_daily_msg(daily_data)

    full = daily_msg
    if do_weekly:
        full += "\n\n" + _build_weekly_msg()

    if args.dry_run:
        print(full)
        return 0

    ok = notifier.send_info(full, label="daily_report")
    print(f"daily_report sent: {ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
