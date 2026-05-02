#!/usr/bin/env python3
"""
backtest.py — CLI fuer Walk-Forward-Backtesting.

Usage:
  python3 scripts/backtest.py
  python3 scripts/backtest.py --start 2024-01-01 --end 2025-12-31
  python3 scripts/backtest.py --tickers NVDA,ASML,MSFT --capital 50000
  python3 scripts/backtest.py --compare-strategies   # mehrere Configs vergleichen
  python3 scripts/backtest.py --v2                   # V2 mit 9-Dim-Scoring
  python3 scripts/backtest.py --v2 --compare-strategies  # V1 vs V2 Vergleich

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

from src.learning.backtest_engine import run_backtest, run_backtest_v2

REPORTS_DIR = Path(__file__).resolve().parents[1] / "backtests"
REPORTS_DIR.mkdir(exist_ok=True)


def _config_hash(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:8]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end",   default=dt.date.today().isoformat())
    parser.add_argument("--tickers",
                        default="XLK,XLF,XLE,XLV,XLI,XLP,XLY,XLU,XLRE,XLC,XLB,NVDA,AMD,AVGO,TSM,ASML,JNJ,PG,KO,JPM,XOM,UNH,LLY,MSFT,GOOGL,META,AMZN,AAPL,PLTR,CRM,SMCI,MRVL,NOW")
    parser.add_argument("--capital",      type=float, default=50000)
    parser.add_argument("--score-buy-max", type=float, default=45)
    parser.add_argument("--stop-loss",     type=float, default=0.10)
    parser.add_argument("--take-profit",   type=float, default=0.40)
    parser.add_argument("--position-eur",  type=float, default=2500)
    parser.add_argument("--max-positions", type=int,   default=20)
    parser.add_argument("--v2", action="store_true",
                        help="V2 engine mit vollem 9-Dim-Risk-Scoring, Vol-Targeting, "
                             "Cash-Floor, Sector-Cap, Daily-Loss-Brake")
    parser.add_argument("--no-vol-targeting", action="store_true",
                        help="V2: Vol-Targeting deaktivieren")
    parser.add_argument("--compare-strategies", action="store_true",
                        help="Conservative vs Moderate vs Aggressive vs ADAPTIVE Vergleich")
    args = parser.parse_args()

    tickers = [t.strip().upper() for t in args.tickers.split(",")]
    engine = run_backtest_v2 if args.v2 else run_backtest
    version = "V2" if args.v2 else "V1"

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

        # V2-specific kwargs
        v2_extra = {}
        if args.v2:
            v2_extra["vol_targeting"] = not args.no_vol_targeting

        results = []
        for name, cfg in configs:
            print(f"\n{'='*60}")
            print(f"  Strategy: {name} ({version})")
            print(f"{'='*60}")
            merged = {**cfg, **v2_extra}
            r = engine(start=args.start, end=args.end, tickers=tickers,
                       initial_capital=args.capital, **merged)
            print(r.summary())
            results.append((name, r))

        # Comparison table
        print(f"\n{'='*80}")
        print(f"  COMPARISON TABLE ({version})")
        print(f"{'='*80}")
        hdr = f"{'Strategy':<14} {'Return':>8} {'CAGR':>8} {'Sharpe':>7} {'Max-DD':>8} {'Vola':>7} {'Trades':>7} {'WinRate':>8}"
        print(hdr)
        print("-" * len(hdr))
        for name, r in results:
            wr = f"{r.win_rate*100:.1f}%" if r.win_rate is not None else "n/a"
            sh = f"{r.sharpe:.2f}" if r.sharpe else "n/a"
            vol = f"{r.annual_vol*100:.1f}%" if r.annual_vol else "n/a"
            print(f"{name:<14} {r.total_return*100:>+7.2f}% {r.cagr*100:>+7.2f}% "
                  f"{sh:>7} {r.max_drawdown*100:>+7.2f}% {vol:>7} {r.n_trades:>7} {wr:>8}")

        # Save comparison report
        h = _config_hash({"strategies": "compare", "version": version,
                          "start": args.start, "end": args.end})
        report_path = REPORTS_DIR / f"{dt.date.today().isoformat()}-compare-{version.lower()}-{h}.md"
        md_lines = [
            f"# Strategy Comparison Report ({version})",
            f"**Period:** {args.start} → {args.end}",
            f"**Tickers:** {', '.join(tickers)}",
            f"**Capital:** {args.capital:,.0f} EUR\n",
            "| Strategy | Return | CAGR | Sharpe | Max-DD | Vola | Trades | Win-Rate |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for name, r in results:
            wr = f"{r.win_rate*100:.1f}%" if r.win_rate is not None else "n/a"
            sh = f"{r.sharpe:.2f}" if r.sharpe else "n/a"
            vol = f"{r.annual_vol*100:.1f}%" if r.annual_vol else "n/a"
            md_lines.append(
                f"| {name} | {r.total_return*100:+.2f}% | {r.cagr*100:+.2f}% | "
                f"{sh} | {r.max_drawdown*100:.2f}% | {vol} | {r.n_trades} | {wr} |"
            )

        # V2-specific extra metrics
        if args.v2:
            md_lines.extend(["", "### V2 Extra Metrics", "",
                "| Strategy | Sortino | Calmar | Avg Hold | Long-Term | Mid-Term |",
                "|---|---|---|---|---|---|"])
            for name, r in results:
                so = f"{r.sortino:.2f}" if hasattr(r, 'sortino') and r.sortino else "n/a"
                ca = f"{r.calmar:.2f}" if hasattr(r, 'calmar') and r.calmar else "n/a"
                ah = f"{r.avg_holding_days:.0f}d" if hasattr(r, 'avg_holding_days') and r.avg_holding_days else "n/a"
                lt = getattr(r, 'long_term_count', 0)
                mt = getattr(r, 'mid_term_count', 0)
                md_lines.append(f"| {name} | {so} | {ca} | {ah} | {lt} | {mt} |")

        report_path.write_text("\n".join(md_lines))
        print(f"\nReport: {report_path}")
        return 0

    # Single strategy run
    v2_extra = {}
    if args.v2:
        v2_extra["vol_targeting"] = not args.no_vol_targeting

    cfg = dict(
        start=args.start, end=args.end, tickers=tickers,
        initial_capital=args.capital, score_buy_max=args.score_buy_max,
        stop_loss_pct=args.stop_loss, take_profit_pct=args.take_profit,
        position_eur=args.position_eur, max_positions=args.max_positions,
        **v2_extra,
    )

    print(f"Running backtest {version} {args.start} → {args.end}, {len(tickers)} tickers...")
    result = engine(**cfg)
    print(result.summary())

    # Markdown-Report
    h = _config_hash(cfg)
    report_path = REPORTS_DIR / f"{dt.date.today().isoformat()}-{version.lower()}-{h}.md"
    md = (
        f"# Backtest Report {version} ({args.start} → {args.end})\n\n"
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
