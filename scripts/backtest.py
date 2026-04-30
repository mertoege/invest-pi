#!/usr/bin/env python3
"""
backtest.py — CLI fuer Walk-Forward-Backtesting.

Usage:
  python3 scripts/backtest.py
  python3 scripts/backtest.py --start 2024-01-01 --end 2025-12-31
  python3 scripts/backtest.py --tickers NVDA,ASML,MSFT --capital 50000
  python3 scripts/backtest.py --compare-strategies   # mehrere Configs vergleichen

Output:
  - stdout-Summary
  - Markdown-Report in backtests/<date>-<config-hash>.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.learning.backtest_engine import run_backtest

REPORTS_DIR = Path(__file__).resolve().parents[1] / "backtests"
REPORTS_DIR.mkdir(exist_ok=True)


def _config_hash(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:8]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end",   default=dt.date.today().isoformat())
    parser.add_argument("--tickers",
                        default="NVDA,ASML,TSM,AMD,AVGO,MSFT,GOOGL,META,LRCX,KLAC")
    parser.add_argument("--capital",      type=float, default=50000)
    parser.add_argument("--score-buy-max", type=float, default=45)
    parser.add_argument("--stop-loss",     type=float, default=0.10)
    parser.add_argument("--take-profit",   type=float, default=0.40)
    parser.add_argument("--position-eur",  type=float, default=2500)
    parser.add_argument("--max-positions", type=int,   default=20)
    parser.add_argument("--compare-strategies", action="store_true",
                        help="conservative vs moderate vs aggressive 3-fach-Vergleich")
    args = parser.parse_args()

    tickers = [t.strip().upper() for t in args.tickers.split(",")]

    if args.compare_strategies:
        configs = [
            ("conservative", {"score_buy_max": 25, "stop_loss_pct": 0.15,
                              "take_profit_pct": 0.20, "position_eur": 200, "max_positions": 5}),
            ("moderate",     {"score_buy_max": 45, "stop_loss_pct": 0.10,
                              "take_profit_pct": 0.40, "position_eur": 2500, "max_positions": 20}),
            ("aggressive",   {"score_buy_max": 60, "stop_loss_pct": 0.12,
                              "take_profit_pct": 0.50, "position_eur": 4000, "max_positions": 25}),
            ("ADAPTIVE",     {"score_buy_max": 45, "stop_loss_pct": 0.10,
                              "take_profit_pct": 0.40, "position_eur": 2500, "max_positions": 20,
                              "mode": "adaptive"}),
        ]
        results = []
        for name, cfg in configs:
            print(f"\n=== Strategy: {name} ===")
            r = run_backtest(start=args.start, end=args.end, tickers=tickers,
                             initial_capital=args.capital, **cfg)
            print(r.summary())
            results.append((name, r))
        return 0

    cfg = dict(
        start=args.start, end=args.end, tickers=tickers,
        initial_capital=args.capital, score_buy_max=args.score_buy_max,
        stop_loss_pct=args.stop_loss, take_profit_pct=args.take_profit,
        position_eur=args.position_eur, max_positions=args.max_positions,
    )

    print(f"Running backtest {args.start} → {args.end}, {len(tickers)} tickers...")
    result = run_backtest(**cfg)
    print(result.summary())

    # Markdown-Report
    h = _config_hash(cfg)
    report_path = REPORTS_DIR / f"{dt.date.today().isoformat()}-{h}.md"
    md = (
        f"# Backtest Report ({args.start} → {args.end})\n\n"
        f"## Config\n```json\n{json.dumps(cfg, indent=2, default=str)}\n```\n\n"
        f"## Result\n```\n{result.summary()}\n```\n\n"
        f"## Equity-Curve (taegliche Snapshots)\n"
        f"```\n{chr(10).join(f'{d}  {e:.2f}' for d, e in result.daily_equity[::5])}\n```\n"
    )
    report_path.write_text(md)
    print(f"\nReport: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
