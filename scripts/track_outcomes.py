#!/usr/bin/env python3
"""
track_outcomes.py — täglicher Cron-Job für Outcome-Messung.

Empfohlener systemd-Timer (auf dem Pi):
  /etc/systemd/system/invest-pi-outcomes.service
    [Service]
    Type=oneshot
    WorkingDirectory=/home/pi/invest-pi
    ExecStart=/usr/bin/python3 scripts/track_outcomes.py

  /etc/systemd/system/invest-pi-outcomes.timer
    [Timer]
    OnCalendar=*-*-* 02:00:00      # täglich 02:00 lokale Zeit
    Persistent=true
    [Install]
    WantedBy=timers.target

Usage manuell:
    python scripts/track_outcomes.py                  # default: daily_score, age >= 1d
    python scripts/track_outcomes.py --source monthly_dca --age 30
    python scripts/track_outcomes.py --no-drift       # Drift-Check skippen
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.outcomes import run_tracker, detect_drift
from src.common.storage import init_all


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="daily_score",
                        help="job_source (default: daily_score)")
    parser.add_argument("--age", type=int, default=1,
                        help="min Alter in Tagen (default: 1)")
    parser.add_argument("--limit", type=int, default=200,
                        help="max predictions pro Lauf")
    parser.add_argument("--no-drift", action="store_true",
                        help="Drift-Check skippen")
    args = parser.parse_args()

    init_all()

    print(f"\n=== Outcome-Tracker · source={args.source} age>={args.age}d ===")
    stats = run_tracker(
        job_source=args.source,
        older_than_days=args.age,
        limit=args.limit,
    )
    print(f"  checked:       {stats['checked']}")
    print(f"  measured:      {stats['measured']}")
    print(f"  still pending: {stats['still_pending']}")
    print(f"  errors:        {stats['errors']}")
    print(f"  by correctness: {stats['by_correctness']}")

    if not args.no_drift:
        drift = detect_drift(args.source)
        if drift:
            print(f"\n⚠ DRIFT WARNING: {drift['message']}")
            # TODO Phase 2: Telegram-Push hier
            sys.exit(2)
        else:
            print("\n  Drift-Check: ok")


if __name__ == "__main__":
    main()
