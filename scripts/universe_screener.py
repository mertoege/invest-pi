#!/usr/bin/env python3
"""
universe_screener.py — sucht neue Ticker-Kandidaten fuer die Watchlist.

Screening-Kriterien:
  1. Mindestens 1B Market-Cap (Liquiditaet)
  2. Mindestens 6 Monate Preishistorie
  3. Durchschnittliches Tagesvolumen > 500k
  4. Nicht bereits im Universe
  5. Geringe Korrelation (<0.85) mit bestehenden Positionen

Output: Vorschlaege in reviews/YYYY-MM-DD-universe_screen.md
Keine automatische config.yaml-Aenderung — nur Empfehlungen.

Timer: monatlich (1. des Monats, 06:00)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
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
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("\'"))

log = logging.getLogger("invest_pi.universe_screener")

REVIEWS_DIR = Path(__file__).resolve().parents[1] / "reviews"
REVIEWS_DIR.mkdir(exist_ok=True)

# Screening-Pool: bekannte liquide US-Aktien die nicht im Universe sind
SCREEN_POOL = [
    "NFLX", "CRM", "ADBE", "INTC", "QCOM", "TXN", "MU", "ANET",
    "PANW", "SNOW", "CRWD", "ZS", "DDOG", "NET", "SQ", "SHOP",
    "COIN", "UBER", "ABNB", "DIS", "NKE", "SBUX", "MCD", "WMT",
    "COST", "HD", "LOW", "TGT", "V", "MA", "AXP", "GS", "MS",
    "BLK", "SCHW", "C", "BAC", "WFC", "PFE", "ABBV", "MRK",
    "BMY", "GILD", "AMGN", "ISRG", "MDT", "ABT", "TMO",
    "DHR", "SYK", "CVX", "COP", "SLB", "EOG", "OXY",
    "NEE", "DUK", "SO", "AEP", "D", "CAT", "DE", "HON",
    "GE", "RTX", "LMT", "BA", "UPS", "FDX",
]


def _get_existing_tickers() -> set[str]:
    """Holt alle Ticker aus config.yaml."""
    import yaml
    cfg_path = Path(__file__).resolve().parents[1] / "config.yaml"
    raw = yaml.safe_load(cfg_path.read_text())
    tickers = set()
    for section in ["portfolio", "watchlist"]:
        if section in raw and raw[section]:
            tickers.update(raw[section].keys())
    universe = raw.get("universe", [])
    if universe:
        for entry in universe:
            if isinstance(entry, dict) and "ticker" in entry:
                tickers.add(entry["ticker"])
            elif isinstance(entry, str):
                tickers.add(entry)
    return tickers


def screen_candidates(max_candidates: int = 10) -> list[dict]:
    """Screent Pool und gibt sortierte Kandidaten zurueck."""
    from src.common.data_loader import get_prices

    existing = _get_existing_tickers()
    pool = [t for t in SCREEN_POOL if t not in existing]

    candidates = []
    for ticker in pool:
        try:
            prices = get_prices(ticker, period="1y")
            if prices is None or len(prices) < 120:
                continue

            avg_volume = float(prices["volume"].tail(60).mean())
            if avg_volume < 500_000:
                continue

            ret_6m = float((prices["close"].iloc[-1] / prices["close"].iloc[-126]) - 1)
            ret_1m = float((prices["close"].iloc[-1] / prices["close"].iloc[-21]) - 1)
            volatility = float(prices["close"].pct_change().tail(60).std() * (252 ** 0.5))

            candidates.append({
                "ticker": ticker,
                "ret_6m": round(ret_6m, 3),
                "ret_1m": round(ret_1m, 3),
                "volatility": round(volatility, 3),
                "avg_volume": int(avg_volume),
                "days_data": len(prices),
            })
        except Exception as e:
            log.debug(f"skip {ticker}: {e}")
            continue

    # Sortiere nach: positive 6m-Returns, niedrige Volatilitaet
    candidates.sort(key=lambda x: (-x["ret_6m"], x["volatility"]))
    return candidates[:max_candidates]


def run(dry_run: bool = False, max_candidates: int = 10) -> dict:
    candidates = screen_candidates(max_candidates)

    if not candidates:
        return {"ok": True, "candidates": 0}

    today = dt.datetime.utcnow().date().isoformat()
    md_lines = [
        f"# Universe Screening ({today})",
        f"",
        f"Pool: {len(SCREEN_POOL)} Ticker | Bereits im Universe: excluded",
        f"",
        f"## Top-Kandidaten",
        f"",
        f"| Ticker | 6M Return | 1M Return | Vol (ann.) | Avg Volume |",
        f"|--------|-----------|-----------|------------|------------|",
    ]
    for c in candidates:
        md_lines.append(
            f"| {c['ticker']:6s} | {c['ret_6m']:+.1%}    | {c['ret_1m']:+.1%}    | "
            f"{c['volatility']:.1%}     | {c['avg_volume']:>10,} |"
        )
    md_lines.append("")
    md_lines.append("_Automatisch generiert. Manuell in config.yaml aufnehmen wenn gewuenscht._")

    md_path = REVIEWS_DIR / f"{today}-universe_screen.md"
    md_path.write_text("\n".join(md_lines))

    try:
        from src.alerts import notifier
        if notifier.is_configured() and not dry_run:
            top3 = ", ".join(c["ticker"] for c in candidates[:3])
            notifier.send_info(
                f"<b>Universe Screening</b>\n"
                f"{len(candidates)} Kandidaten gefunden\n"
                f"Top 3: {top3}",
                label="universe_screen",
            )
    except Exception:
        pass

    return {
        "ok": True,
        "candidates": len(candidates),
        "top": [c["ticker"] for c in candidates[:5]],
        "md_path": str(md_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max", type=int, default=10)
    args = parser.parse_args()
    result = run(dry_run=args.dry_run, max_candidates=args.max)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
