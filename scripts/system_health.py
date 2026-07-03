#!/usr/bin/env python3
"""
system_health.py — Prueft Systemgesundheit und sendet Telegram-Alert bei Problemen.
Laeuft taeglich via Backup-Timer (03:30) oder standalone.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.storage import (
    TRADING_DB, LEARNING_DB, MARKET_DB, ALERTS_DB,
    DATA_DIR, connect, init_all,
)


def check_db_integrity() -> list[str]:
    issues = []
    for name, path in [("trading", TRADING_DB), ("learning", LEARNING_DB),
                        ("market", MARKET_DB), ("alerts", ALERTS_DB)]:
        try:
            with connect(path) as conn:
                result = conn.execute("PRAGMA integrity_check").fetchone()[0]
                if result != "ok":
                    issues.append(f"DB {name}: integrity={result}")
        except Exception as e:
            issues.append(f"DB {name}: {e}")
    return issues


def check_disk() -> list[str]:
    issues = []
    st = os.statvfs("/")
    free_gb = (st.f_bavail * st.f_frsize) / (1024**3)
    if free_gb < 10:
        issues.append(f"Disk: nur {free_gb:.1f} GB frei")
    db_size_mb = sum(
        p.stat().st_size for p in DATA_DIR.glob("*.db") if p.exists()
    ) / (1024 * 1024)
    if db_size_mb > 500:
        issues.append(f"DBs: {db_size_mb:.0f} MB (>500 MB)")
    return issues


def check_stale_scores() -> list[str]:
    issues = []
    try:
        with connect(LEARNING_DB) as conn:
            latest = conn.execute(
                "SELECT max(created_at) as t FROM predictions WHERE job_source='daily_score'"
            ).fetchone()["t"]
        if latest:
            age_h = (dt.datetime.now(dt.timezone.utc) -
                     dt.datetime.fromisoformat(latest).replace(tzinfo=dt.timezone.utc)
                     ).total_seconds() / 3600
            if age_h > 26:
                issues.append(f"Scores: letzter Score {age_h:.0f}h alt")
    except Exception as e:
        issues.append(f"Score-Check: {e}")
    return issues


def check_equity_trend() -> list[str]:
    issues = []
    try:
        with connect(TRADING_DB) as conn:
            snaps = conn.execute(
                "SELECT total_usd FROM equity_snapshots "
                "WHERE total_usd IS NOT NULL ORDER BY timestamp DESC LIMIT 10"
            ).fetchall()
        if len(snaps) >= 5:
            latest = snaps[0]["total_usd"]
            avg_5 = sum(s["total_usd"] for s in snaps[:5]) / 5
            drop_pct = (latest / avg_5 - 1) * 100
            if drop_pct < -3:
                issues.append(f"Equity: {drop_pct:+.1f}% vs 5-Snapshot-Schnitt (${latest:,.0f})")
    except Exception:
        pass
    return issues


def check_failed_timers() -> list[str]:
    issues = []
    try:
        result = subprocess.run(
            ["systemctl", "list-units", "--failed", "--no-legend", "--no-pager"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.strip().split("\n"):
            if "invest-pi" in line and ".service" in line:
                parts = line.split()
                unit = next((p for p in parts if "invest-pi" in p), parts[0])
                issues.append(f"Timer failed: {unit}")
    except Exception:
        pass
    return issues


def check_trade_anomalies() -> list[str]:
    issues = []
    try:
        with connect(TRADING_DB) as conn:
            stuck = conn.execute(
                "SELECT count(*) as n FROM trades "
                "WHERE status IN ('accepted','pending_new','new') AND source = 'paper' "
                "AND created_at < datetime('now', '-24 hours')"
            ).fetchone()["n"]
            if stuck > 0:
                issues.append(f"Trades: {stuck} Orders seit >24h unfilled")
    except Exception:
        pass
    return issues


def db_maintenance():
    """Bereinigt alte Daten und optimiert DBs."""
    try:
        with connect(LEARNING_DB) as conn:
            count_before = conn.execute("SELECT count(*) as n FROM reflections").fetchone()["n"]
            if count_before > 5000:
                conn.execute(
                    "DELETE FROM reflections WHERE id NOT IN "
                    "(SELECT id FROM reflections ORDER BY created_at DESC LIMIT 5000)"
                )
                cleaned = count_before - 5000
                print(f"  reflections: {count_before} -> 5000 ({cleaned} geloescht)")
    except Exception as e:
        print(f"  reflections cleanup failed: {e}")

    try:
        with connect(LEARNING_DB) as conn:
            old = conn.execute(
                "DELETE FROM predictions WHERE created_at < datetime('now', '-120 days') "
                "AND outcome_correct IS NULL AND job_source != 'trade_decision'"
            )
            if old.rowcount:
                print(f"  old predictions: {old.rowcount} geloescht")
    except Exception as e:
        print(f"  predictions cleanup failed: {e}")

    for name, db in [("learning", LEARNING_DB), ("trading", TRADING_DB)]:
        try:
            import sqlite3
            conn = sqlite3.connect(db)
            conn.execute("VACUUM")
            conn.close()
            print(f"  {name}.db: VACUUM ok")
        except Exception as e:
            print(f"  {name}.db VACUUM failed: {e}")


def main():
    init_all()
    all_issues = []
    all_issues.extend(check_db_integrity())
    all_issues.extend(check_disk())
    all_issues.extend(check_stale_scores())
    all_issues.extend(check_equity_trend())
    all_issues.extend(check_failed_timers())
    all_issues.extend(check_trade_anomalies())

    if not all_issues:
        print("System Health: OK")
    else:
        msg = "⚠️ *Invest-Pi Health Check*\n\n"
        for issue in all_issues:
            msg += f"• {issue}\n"
        msg += f"\n_{dt.datetime.now().strftime('%Y-%m-%d %H:%M')}_"

        print(f"Health issues found ({len(all_issues)}):")
        for i in all_issues:
            print(f"  - {i}")

        try:
            from src.alerts.notifier import send_info
            sent = send_info(msg, label="health_check")
            print(f"Telegram alert: {'sent' if sent else 'failed'}")
        except Exception as e:
            print(f"Telegram alert failed: {e}")

    db_maintenance()


if __name__ == "__main__":
    main()
