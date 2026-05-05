"""
Invest-Pi Web Dashboard — FastAPI Backend.
Serves API endpoints and static frontend.
"""
from __future__ import annotations

import sys
import os
import time
import sqlite3
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
from contextlib import contextmanager
from typing import Generator

# Project path
PROJECT_DIR = Path("/home/investpi/invest-pi")
sys.path.insert(0, str(PROJECT_DIR))

from dotenv import load_dotenv
load_dotenv(PROJECT_DIR / ".env")

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ─── App Setup ───────────────────────────────────────────────
app = FastAPI(title="Invest-Pi Dashboard", root_path="/investpi")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── DB Helpers ──────────────────────────────────────────────
TRADING_DB = PROJECT_DIR / "data" / "trading.db"
LEARNING_DB = PROJECT_DIR / "data" / "learning.db"


@contextmanager
def db_connect(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ─── Broker Helper ───────────────────────────────────────────
def _get_broker():
    from src.broker import get_broker
    return get_broker(kind="alpaca_paper")


# ─── Risk Score Cache ────────────────────────────────────────
_risk_cache: dict = {"data": [], "expires": 0}


# ─── API Endpoints ───────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@app.get("/api/dashboard")
def dashboard():
    try:
        broker = _get_broker()
        account = broker.get_account()
        positions = broker.get_positions()

        # Today's trades count
        today_str = datetime.now().strftime("%Y-%m-%d")
        trades_today = 0
        if TRADING_DB.exists():
            with db_connect(TRADING_DB) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM trades WHERE created_at >= ? AND status='filled'",
                    (today_str,)
                ).fetchone()
                trades_today = row["cnt"] if row else 0

        # P&L calculations
        total_unrealized_eur = sum(p.unrealized_pl_eur for p in positions)
        total_unrealized_usd = sum((p.market_price - p.avg_price) * p.qty for p in positions if p.avg_price)
        total_market_value_eur = sum(p.market_value_eur for p in positions)
        invested_usd = account.equity_usd - account.cash_usd

        # Day P&L from equity snapshots
        day_pl_usd = 0.0
        if TRADING_DB.exists():
            with db_connect(TRADING_DB) as conn:
                prev = conn.execute(
                    """SELECT total_usd FROM equity_snapshots
                       WHERE source='paper' AND date(timestamp) < date('now')
                         AND total_usd IS NOT NULL
                       ORDER BY timestamp DESC LIMIT 1"""
                ).fetchone()
                if prev and prev["total_usd"]:
                    day_pl_usd = account.equity_usd - prev["total_usd"]

        # Starting equity (first snapshot ever) for total P&L
        total_pl_usd = 0.0
        if TRADING_DB.exists():
            with db_connect(TRADING_DB) as conn:
                first = conn.execute(
                    """SELECT total_usd FROM equity_snapshots
                       WHERE source='paper' AND total_usd IS NOT NULL
                       ORDER BY timestamp ASC LIMIT 1"""
                ).fetchone()
                if first and first["total_usd"]:
                    total_pl_usd = account.equity_usd - first["total_usd"]

        return {
            "equity_usd": account.equity_usd,
            "cash_usd": account.cash_usd,
            "equity_eur": account.equity_eur,
            "cash_eur": account.cash_eur,
            "buying_power_usd": account.buying_power_usd,
            "fx_rate": account.fx_rate,
            "positions_count": len(positions),
            "trades_today": trades_today,
            "invested_usd": invested_usd,
            "total_market_value_eur": round(total_market_value_eur, 2),
            "unrealized_pl_eur": round(total_unrealized_eur, 2),
            "unrealized_pl_usd": round(total_unrealized_usd, 2),
            "unrealized_pl_pct": round((total_unrealized_usd / invested_usd * 100) if invested_usd > 0 else 0, 2),
            "day_pl_usd": round(day_pl_usd, 2),
            "day_pl_pct": round((day_pl_usd / (account.equity_usd - day_pl_usd) * 100) if account.equity_usd > day_pl_usd else 0, 2),
            "total_pl_usd": round(total_pl_usd, 2),
            "total_pl_pct": round((total_pl_usd / (account.equity_usd - total_pl_usd) * 100) if account.equity_usd > total_pl_usd else 0, 3),
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/positions")
def positions():
    try:
        broker = _get_broker()
        positions = broker.get_positions()
        result = []
        for p in positions:
            pct_change = 0.0
            if p.avg_price and p.avg_price > 0:
                pct_change = ((p.market_price - p.avg_price) / p.avg_price) * 100
            result.append({
                "ticker": p.ticker,
                "qty": p.qty,
                "avg_price": round(p.avg_price, 2),
                "market_price": round(p.market_price, 2),
                "market_value_eur": round(p.market_value_eur, 2),
                "unrealized_pl_eur": round(p.unrealized_pl_eur, 2),
                "pct_change": round(pct_change, 2),
                "currency": p.currency,
            })
        # Sort by market value descending
        result.sort(key=lambda x: x["market_value_eur"], reverse=True)
        return {"positions": result}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/trades")
def trades():
    try:
        if not TRADING_DB.exists():
            return {"trades": []}
        with db_connect(TRADING_DB) as conn:
            rows = conn.execute(
                """SELECT ticker, side, qty, price, fill_price, status,
                          created_at, strategy_label, source
                   FROM trades ORDER BY created_at DESC LIMIT 50"""
            ).fetchall()
            result = [
                {
                    "ticker": r["ticker"],
                    "side": r["side"],
                    "qty": r["qty"],
                    "price": r["fill_price"] or r["price"],
                    "status": r["status"],
                    "timestamp": r["created_at"],
                    "strategy": r["strategy_label"],
                    "source": r["source"],
                }
                for r in rows
            ]
        return {"trades": result}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/equity-history")
def equity_history():
    try:
        if not TRADING_DB.exists():
            return {"snapshots": []}
        cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        with db_connect(TRADING_DB) as conn:
            rows = conn.execute(
                """SELECT timestamp, total_usd, total_eur, cash_usd, positions_value_usd
                   FROM equity_snapshots
                   WHERE timestamp >= ? AND source = 'paper' AND total_usd IS NOT NULL
                   ORDER BY timestamp ASC""",
                (cutoff,)
            ).fetchall()
            result = [
                {
                    "timestamp": r["timestamp"],
                    "total_usd": r["total_usd"],
                    "total_eur": r["total_eur"],
                    "cash_usd": r["cash_usd"],
                    "positions_usd": r["positions_value_usd"],
                }
                for r in rows
            ]
        return {"snapshots": result}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/risk-scores")
def risk_scores():
    global _risk_cache
    now = time.time()
    if _risk_cache["expires"] > now:
        return {"scores": _risk_cache["data"], "cached": True}

    try:
        broker = _get_broker()
        positions = broker.get_positions()
        if not positions:
            return {"scores": [], "cached": False}

        from src.alerts.risk_scorer import score_ticker
        results = []
        for p in positions[:10]:  # Limit to 10 to avoid timeout
            try:
                report = score_ticker(p.ticker)
                results.append({
                    "ticker": report.ticker,
                    "composite_score": round(report.composite_score, 1),
                    "alert_level": report.alert_label,
                    "timestamp": report.timestamp,
                })
            except Exception:
                results.append({
                    "ticker": p.ticker,
                    "composite_score": None,
                    "alert_level": "Unknown",
                    "timestamp": None,
                })

        _risk_cache = {"data": results, "expires": now + 600}
        return {"scores": results, "cached": False}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/dca")
def dca_holdings():
    import json as json_mod
    try:
        if not LEARNING_DB.exists():
            return {"dca": []}
        with db_connect(LEARNING_DB) as conn:
            rows = conn.execute(
                """SELECT p.id, p.subject_id as ticker,
                          json_extract(p.output_json, '$.ticker') AS ticker_from_output,
                          json_extract(p.output_json, '$.reason') AS buy_reason,
                          json_extract(p.output_json, '$.confidence') AS confidence,
                          p.created_at AS recommended_at,
                          fr.created_at AS bought_at,
                          fr.reason_text AS extra
                   FROM feedback_reasons fr
                   JOIN predictions p ON p.id = fr.prediction_id
                   WHERE fr.feedback_type = 'dca_bought'
                   ORDER BY fr.created_at DESC"""
            ).fetchall()

        result = []
        for r in rows:
            ticker = r["ticker"] or r["ticker_from_output"]
            if not ticker:
                continue
            # Parse actual buy price from reason_text if stored
            actual_buy_price = None
            extra = r["extra"] or ""
            if "buy_price=" in extra:
                try:
                    actual_buy_price = float(extra.split("buy_price=")[1].split()[0])
                except (ValueError, IndexError):
                    pass

            entry = {
                "ticker": ticker,
                "recommended_at": r["recommended_at"],
                "bought_at": r["bought_at"],
                "reason": (r["buy_reason"] or "")[:200],
                "confidence": r["confidence"] or "?",
                "current_price": None,
                "buy_price": actual_buy_price,
                "performance_pct": None,
            }
            # Live price via yfinance
            try:
                import yfinance as yf
                stock = yf.Ticker(ticker)
                hist = stock.history(period="1d")
                if not hist.empty:
                    cur_price = round(float(hist["Close"].iloc[-1]), 2)
                    entry["current_price"] = cur_price
                    if actual_buy_price:
                        entry["performance_pct"] = round((cur_price / actual_buy_price - 1) * 100, 2)
                    else:
                        rec_date = r["recommended_at"][:10] if r["recommended_at"] else None
                        if rec_date:
                            hist_full = stock.history(start=rec_date)
                            if len(hist_full) >= 2:
                                entry["buy_price"] = round(float(hist_full["Close"].iloc[0]), 2)
                                entry["performance_pct"] = round((cur_price / float(hist_full["Close"].iloc[0]) - 1) * 100, 2)
            except Exception:
                pass
            result.append(entry)
        return {"dca": result}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/system")
def system_status():
    try:
        # Systemd timer statuses (system-level timers)
        timer_statuses = []
        try:
            result = subprocess.run(
                ["systemctl", "list-timers", "invest-pi*", "--no-pager", "--plain", "--no-legend"],
                capture_output=True, text=True, timeout=5
            )
            active_timers = set()
            for line in result.stdout.strip().splitlines():
                parts = line.split()
                for p in parts:
                    if p.startswith("invest-pi-") and p.endswith(".timer"):
                        active_timers.add(p.replace(".timer", ""))

            all_timers = [
                "invest-pi-auto-pull", "invest-pi-status-push",
                "invest-pi-telegram-callbacks", "invest-pi-score",
                "invest-pi-hardware", "invest-pi-sync",
                "invest-pi-strategy", "invest-pi-rebalance",
                "invest-pi-daily-report", "invest-pi-outcomes",
                "invest-pi-backup", "invest-pi-train-regime",
                "invest-pi-weekly-recap", "invest-pi-patterns",
                "invest-pi-monthly-dca", "invest-pi-meta-review",
                "invest-pi-dca-watchdog",
            ]
            for t in all_timers:
                timer_statuses.append({"name": t, "active": t in active_timers})
        except Exception:
            pass

        # Last commit
        try:
            result = subprocess.run(
                ["git", "-C", str(PROJECT_DIR), "log", "-1", "--format=%H|%s|%ci"],
                capture_output=True, text=True, timeout=5
            )
            parts = result.stdout.strip().split("|", 2)
            last_commit = {
                "hash": parts[0][:8] if parts else "",
                "message": parts[1] if len(parts) > 1 else "",
                "date": parts[2] if len(parts) > 2 else "",
            }
        except Exception:
            last_commit = {"hash": "", "message": "", "date": ""}

        # Uptime
        try:
            result = subprocess.run(
                ["uptime", "-p"], capture_output=True, text=True, timeout=3
            )
            uptime = result.stdout.strip()
        except Exception:
            uptime = "unknown"

        return {
            "timers": timer_statuses,
            "last_commit": last_commit,
            "uptime": uptime,
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/performance")
def performance():
    """Detaillierte Performance-Metriken."""
    try:
        if not TRADING_DB.exists():
            return {"error": "no data"}
        with db_connect(TRADING_DB) as conn:
            # All equity snapshots for return calculation
            rows = conn.execute(
                """SELECT timestamp, total_usd, total_eur, cash_usd, positions_value_usd
                   FROM equity_snapshots WHERE source='paper'
                   ORDER BY timestamp ASC"""
            ).fetchall()

            if not rows:
                return {"snapshots_count": 0}

            # Skip rows with NULL total_usd
            valid_rows = [r for r in rows if r["total_usd"] is not None]
            if not valid_rows:
                return {"snapshots_count": 0}

            first_equity = valid_rows[0]["total_usd"]
            latest_equity = valid_rows[-1]["total_usd"]
            total_return_pct = ((latest_equity / first_equity) - 1) * 100 if first_equity else 0

            # Trade stats
            trade_stats = conn.execute(
                """SELECT
                       COUNT(*) as total_trades,
                       SUM(CASE WHEN side='buy' THEN 1 ELSE 0 END) as buys,
                       SUM(CASE WHEN side='sell' THEN 1 ELSE 0 END) as sells,
                       SUM(eur_value) as total_volume_eur
                   FROM trades WHERE status='filled'"""
            ).fetchone()

            # Win/loss from closed positions (sells)
            sell_trades = conn.execute(
                """SELECT ticker, qty, price, fill_price, eur_value
                   FROM trades WHERE side='sell' AND status='filled'"""
            ).fetchall()

            # Realized P&L from sells vs buy avg
            realized_pl = 0.0
            # (simplified: would need buy-price lookup, skip for now)

        return {
            "total_return_pct": round(total_return_pct, 3),
            "total_return_usd": round(latest_equity - first_equity, 2),
            "start_equity": round(first_equity, 2),
            "current_equity": round(latest_equity, 2),
            "total_trades": (trade_stats["total_trades"] or 0) if trade_stats else 0,
            "total_buys": (trade_stats["buys"] or 0) if trade_stats else 0,
            "total_sells": (trade_stats["sells"] or 0) if trade_stats else 0,
            "total_volume_eur": round(float(trade_stats["total_volume_eur"] or 0), 2) if trade_stats else 0,
            "data_since": valid_rows[0]["timestamp"][:10] if valid_rows else None,
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/allocation")
def allocation():
    """Portfolio allocation by sector/type."""
    try:
        broker = _get_broker()
        positions = broker.get_positions()
        if not positions:
            return {"allocation": []}

        # Categorize tickers
        etfs = {"XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLRE", "XLC", "XLB"}
        sector_map = {
            "XLK": "Tech", "XLF": "Finance", "XLE": "Energy", "XLV": "Health",
            "XLI": "Industrial", "XLP": "Consumer", "XLRE": "Real Estate",
            "XLC": "Communication", "XLB": "Materials",
            "AAPL": "Tech", "MSFT": "Tech", "GOOGL": "Tech", "META": "Tech",
            "AMZN": "Consumer", "AMD": "Tech", "ASML": "Tech",
            "JPM": "Finance", "UNH": "Health", "JNJ": "Health",
            "LLY": "Health", "KO": "Consumer", "XOM": "Energy",
            "ORCL": "Tech", "NVDA": "Tech",
        }

        sectors = {}
        for p in positions:
            sector = sector_map.get(p.ticker, "Other")
            is_etf = p.ticker in etfs
            cat = f"{sector} (ETF)" if is_etf else sector
            sectors[cat] = sectors.get(cat, 0) + p.market_value_eur

        total = sum(sectors.values())
        result = [
            {"name": k, "value_eur": round(v, 2), "pct": round(v / total * 100, 1) if total else 0}
            for k, v in sorted(sectors.items(), key=lambda x: -x[1])
        ]
        return {"allocation": result, "total_eur": round(total, 2)}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ─── Static Files & Frontend ─────────────────────────────────
STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
def serve_index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ─── Entry Point ─────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8200, log_level="info")
