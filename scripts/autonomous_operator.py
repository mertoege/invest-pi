#!/usr/bin/env python3
"""
autonomous_operator.py — Taeglicher System-Check + Auto-Fix.

Laeuft jeden Morgen vor Marktoeffnung (13:00 CET).
Prueft das System, fixt bekannte Probleme automatisch,
und meldet das Ergebnis per Telegram.

Kein LLM noetig — deterministische Checks und Fixes.
Strategische Entscheidungen kommen aus dem Weekly-Mini-Review (Opus).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

env_path = Path(__file__).resolve().parents[1] / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from src.common.storage import connect, TRADING_DB, LEARNING_DB, init_all
from src.alerts.notifier import send_info


def check_equity() -> dict:
    with connect(TRADING_DB) as conn:
        eq = conn.execute(
            "SELECT * FROM equity_snapshots WHERE source='paper' ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        if not eq:
            return {"status": "no_data"}
        prev = conn.execute(
            "SELECT total_usd FROM equity_snapshots WHERE source='paper' "
            "AND timestamp < ? ORDER BY timestamp DESC LIMIT 1",
            (eq["timestamp"],),
        ).fetchone()
        daily_change = 0
        if prev and prev["total_usd"] and eq["total_usd"]:
            daily_change = (eq["total_usd"] / prev["total_usd"] - 1) * 100
        return {
            "total_usd": eq["total_usd"],
            "cash_usd": eq["cash_usd"],
            "daily_change_pct": round(daily_change, 2),
            "timestamp": eq["timestamp"],
        }


def check_trades_yesterday() -> dict:
    with connect(TRADING_DB) as conn:
        trades = conn.execute(
            "SELECT side, count(*) as n, sum(eur_value) as vol FROM trades "
            "WHERE date(created_at) = date('now', '-1 day') GROUP BY side"
        ).fetchall()
        summary = {t["side"]: {"n": t["n"], "vol": round(t["vol"] or 0)} for t in trades}

        dupes = conn.execute(
            "SELECT ticker, side, substr(created_at,1,16) as ts, count(*) as c "
            "FROM trades WHERE created_at > datetime('now', '-1 day') "
            "GROUP BY ticker, side, ts HAVING c > 1"
        ).fetchall()
        return {"trades": summary, "duplicates": len(dupes)}


def check_pending_orders() -> dict:
    with connect(TRADING_DB) as conn:
        pending = conn.execute(
            "SELECT count(*) as n FROM trades "
            "WHERE status IN ('pending_new','accepted') "
            "AND created_at < datetime('now', '-4 hours')"
        ).fetchone()
        return {"stale_pending": pending["n"]}


def check_accuracy() -> dict:
    with connect(LEARNING_DB) as conn:
        acc_7d = conn.execute(
            "SELECT avg(outcome_correct) as acc, count(*) as n FROM predictions "
            "WHERE outcome_correct IS NOT NULL "
            "AND outcome_measured_at > datetime('now', '-7 days')"
        ).fetchone()
        acc_30d = conn.execute(
            "SELECT avg(outcome_correct) as acc, count(*) as n FROM predictions "
            "WHERE outcome_correct IS NOT NULL "
            "AND outcome_measured_at > datetime('now', '-30 days')"
        ).fetchone()
        unmeasured = conn.execute(
            "SELECT count(*) FROM predictions "
            "WHERE outcome_correct IS NULL AND outcome_json IS NULL"
        ).fetchone()[0]
        return {
            "accuracy_7d": round(acc_7d["acc"] * 100, 1) if acc_7d["acc"] else None,
            "accuracy_30d": round(acc_30d["acc"] * 100, 1) if acc_30d["acc"] else None,
            "measured_7d": acc_7d["n"],
            "unmeasured": unmeasured,
        }


def check_positions() -> dict:
    etf_tickers = ("XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY")
    with connect(TRADING_DB) as conn:
        total = conn.execute(
            "SELECT count(*) as n FROM positions WHERE source='paper' AND qty > 0"
        ).fetchone()["n"]
        placeholders = ",".join("?" for _ in etf_tickers)
        etfs = conn.execute(
            f"SELECT count(*) as n FROM positions WHERE source='paper' AND qty > 0 "
            f"AND ticker IN ({placeholders})",
            etf_tickers,
        ).fetchone()["n"]
        return {"total": total, "etfs": etfs, "stocks": total - etfs}


def check_failed_timers() -> list:
    try:
        r = subprocess.run(
            ["systemctl", "list-units", "--failed", "--no-pager", "--plain"],
            capture_output=True, text=True, timeout=10
        )
        return [l.strip() for l in r.stdout.splitlines() if "invest-pi" in l]
    except Exception:
        return []


def check_recent_crashes() -> list:
    """Scanne journalctl der letzten 24h nach Python-Tracebacks in invest-pi
    Services. Faengt Crashes die der Momentan-Check (--failed) verpasst, weil
    timer-getriggerte oneshot-Services zwischen Laeufen nicht 'failed' bleiben
    und der Operator (13:00, Markt zu) oft genau im sauberen Fenster prueft."""
    try:
        r = subprocess.run(
            ["journalctl", "--since", "24 hours ago", "--no-pager", "-o", "cat",
             "-u", "invest-pi-strategy-hourly.service",
             "-u", "invest-pi-rebalance.service",
             "-u", "invest-pi-score.service",
             "-u", "invest-pi-sync.service"],
            capture_output=True, text=True, timeout=20
        )
        markers = ("Traceback (most recent call last)", "UnboundLocalError",
                   "result 'exit-code'", "status=1/FAILURE")
        hits = {}
        for line in r.stdout.splitlines():
            for m in markers:
                if m in line:
                    hits[m] = hits.get(m, 0) + 1
        return [f"{m} x{n}" for m, n in hits.items()]
    except Exception:
        return []


def fix_dedup() -> int:
    with connect(LEARNING_DB) as conn:
        result = conn.execute("""
            UPDATE predictions
            SET outcome_json = '{"superseded": true}',
                outcome_measured_at = datetime('now')
            WHERE id NOT IN (
                SELECT MAX(id) FROM predictions
                WHERE outcome_correct IS NULL AND outcome_json IS NULL
                GROUP BY subject_id, date(created_at)
            )
            AND outcome_correct IS NULL AND outcome_json IS NULL
        """)
        return result.rowcount


def fix_stale_predictions() -> int:
    with connect(LEARNING_DB) as conn:
        result = conn.execute("""
            UPDATE predictions
            SET outcome_json = '{"expired": true}',
                outcome_measured_at = datetime('now')
            WHERE outcome_correct IS NULL AND outcome_json IS NULL
            AND created_at < datetime('now', '-90 days')
        """)
        return result.rowcount


def build_telegram_message(checks: dict, fixes: dict) -> str:
    eq = checks["equity"]
    trades = checks["trades"]
    acc = checks["accuracy"]
    pos = checks["positions"]
    failed = checks["failed_timers"]
    crashes = checks.get("recent_crashes", [])

    if failed or crashes or trades["duplicates"] > 0:
        emoji = "⚠️"
    elif fixes.get("deduped", 0) > 0 or fixes.get("expired", 0) > 0:
        emoji = "\U0001f527"
    else:
        emoji = "✅"

    lines = [f"{emoji} Daily Operator Report"]

    if eq.get("total_usd"):
        lines.append(f"Portfolio: ${eq['total_usd']:,.0f} ({eq['daily_change_pct']:+.1f}%)")
    lines.append(f"Positionen: {pos['stocks']} Stocks, {pos['etfs']} ETFs")

    buy_info = trades["trades"].get("buy", {})
    sell_info = trades["trades"].get("sell", {})
    buy_n = buy_info.get("n", 0)
    sell_n = sell_info.get("n", 0)
    if buy_n or sell_n:
        lines.append(f"Trades gestern: {buy_n} Buys, {sell_n} Sells")

    if acc["accuracy_7d"]:
        lines.append(f"Accuracy: {acc['accuracy_7d']}% (7d), {acc['unmeasured']} pending")

    if fixes.get("deduped", 0) > 0:
        lines.append(f"Dedup: {fixes['deduped']} redundante Predictions bereinigt")
    if fixes.get("expired", 0) > 0:
        lines.append(f"Expired: {fixes['expired']} alte Predictions geschlossen")

    if failed:
        lines.append(f"FEHLER: {len(failed)} Timer fehlgeschlagen!")
    if crashes:
        lines.append(f"CRASH (24h): {', '.join(crashes)}")
    if trades["duplicates"] > 0:
        lines.append(f"WARNUNG: {trades['duplicates']} Duplicate-Trades!")

    return "\n".join(lines)


def main():
    init_all()
    print(f"=== Autonomous Operator === {dt.datetime.now().isoformat(timespec='seconds')}")

    checks = {
        "equity": check_equity(),
        "trades": check_trades_yesterday(),
        "pending": check_pending_orders(),
        "accuracy": check_accuracy(),
        "positions": check_positions(),
        "failed_timers": check_failed_timers(),
        "recent_crashes": check_recent_crashes(),
    }

    for name, result in checks.items():
        print(f"  {name}: {result}")

    fixes = {}
    fixes["deduped"] = fix_dedup()
    if fixes["deduped"]:
        print(f"  FIX: {fixes['deduped']} redundante Predictions deduped")

    fixes["expired"] = fix_stale_predictions()
    if fixes["expired"]:
        print(f"  FIX: {fixes['expired']} alte Predictions expired")

    msg = build_telegram_message(checks, fixes)
    print(f"\nTelegram:\n{msg}")
    try:
        send_info(msg, label="operator")
        print("  Telegram: sent")
    except Exception as e:
        print(f"  Telegram failed: {e}")

    print("\n=== Done ===")


if __name__ == "__main__":
    main()
