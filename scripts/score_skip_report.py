#!/usr/bin/env python3
"""
score_skip_report.py — Zeigt welche Tickers im letzten Score-Lauf geskipt wurden + warum.

Aufruf:
  sudo -u investpi python3 scripts/score_skip_report.py
  sudo -u investpi python3 scripts/score_skip_report.py --days 7
  sudo -u investpi python3 scripts/score_skip_report.py --as-telegram
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.json_utils import safe_parse
from src.common.storage import LEARNING_DB, connect


def report(days: int = 1) -> dict:
    sql = """
        SELECT subject_id, output_json, created_at
          FROM predictions
         WHERE job_source = 'score_skip'
           AND created_at >= datetime('now', ?)
         ORDER BY created_at DESC
    """
    with connect(LEARNING_DB) as conn:
        rows = conn.execute(sql, (f"-{days} day",)).fetchall()

    skipped = []
    for r in rows:
        out = safe_parse(r["output_json"] or "{}", default={})
        skipped.append({
            "ticker":     r["subject_id"],
            "error":      out.get("error", "?"),
            "created_at": r["created_at"],
        })

    # Eindeutige Tickers (latest skip)
    by_ticker = {}
    for s in skipped:
        if s["ticker"] not in by_ticker:
            by_ticker[s["ticker"]] = s

    return {
        "lookback_days":  days,
        "total_skip_rows": len(skipped),
        "unique_tickers": list(by_ticker.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--as-telegram", action="store_true",
                        help="Sende als Telegram-Push (sonst nur stdout)")
    args = parser.parse_args()

    result = report(days=args.days)

    if args.as_telegram:
        # Build HTML message
        from html import escape
        from src.alerts import notifier
        lines = [f"⚠️ <b>Score-Skip-Report (letzte {args.days}d)</b>"]
        if not result["unique_tickers"]:
            lines.append("  Keine Skips — alle Tickers wurden gescored")
        else:
            lines.append(f"  {len(result['unique_tickers'])} eindeutige Tickers geskipt:")
            for t in result["unique_tickers"][:15]:
                lines.append(f"  • <code>{escape(t['ticker'])}</code>: {escape(t['error'][:80])}")
        notifier.send_info("\n".join(lines), label="score_skip_report")

    # Human-readable stdout
    print(f"=== Skip-Report (letzte {args.days}d) ===")
    print(f"Total skip-rows: {result['total_skip_rows']}")
    print(f"Unique Tickers:  {len(result['unique_tickers'])}")
    if not result["unique_tickers"]:
        print("\nKeine Skips — alle Tickers wurden gescored.")
    else:
        print("\nDetails (latest skip pro Ticker):")
        for t in result["unique_tickers"]:
            print(f"  {t['ticker']:<10} {t['created_at']:<25} {t['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
