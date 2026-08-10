"""
Invest-Pi Web Dashboard — FastAPI Backend.
Serves API endpoints and static frontend.
"""
from __future__ import annotations

import sys
import os
import re
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
AI_SWING_DB = PROJECT_DIR / "data" / "ai_swing.db"


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
                    "SELECT COUNT(*) as cnt FROM trades WHERE created_at >= ? AND status='filled' AND source='paper'",
                    (today_str,)
                ).fetchone()
                trades_today = row["cnt"] if row else 0

        # P&L calculations
        total_unrealized_eur = sum(p.unrealized_pl_eur for p in positions)
        total_unrealized_usd = sum((p.market_price - p.avg_price) * p.qty for p in positions if p.avg_price)
        total_market_value_eur = sum(p.market_value_eur for p in positions)
        market_value_usd = account.equity_usd - account.cash_usd
        cost_basis_usd = sum(p.avg_price * p.qty for p in positions if p.avg_price)

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
            "invested_usd": round(cost_basis_usd, 2),
            "market_value_usd": round(market_value_usd, 2),
            "total_market_value_eur": round(total_market_value_eur, 2),
            "unrealized_pl_eur": round(total_unrealized_eur, 2),
            "unrealized_pl_usd": round(total_unrealized_usd, 2),
            "unrealized_pl_pct": round((total_unrealized_usd / cost_basis_usd * 100) if cost_basis_usd > 0 else 0, 2),
            "day_pl_usd": round(day_pl_usd, 2),
            "day_pl_eur": round(day_pl_usd * account.fx_rate, 2),
            "day_pl_pct": round((day_pl_usd / (account.equity_usd - day_pl_usd) * 100) if account.equity_usd > day_pl_usd else 0, 2),
            "total_pl_usd": round(total_pl_usd, 2),
            "total_pl_eur": round(total_pl_usd * account.fx_rate, 2),
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
                   FROM trades WHERE source='paper' ORDER BY created_at DESC LIMIT 50"""
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
def equity_history(days: int = 0):
    try:
        if not TRADING_DB.exists():
            return {"snapshots": []}
        where_clause = "source = 'paper' AND total_usd IS NOT NULL"
        params: list = []
        if days > 0:
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            where_clause += " AND timestamp >= ?"
            params.append(cutoff)
        with db_connect(TRADING_DB) as conn:
            rows = conn.execute(
                f"""SELECT timestamp, total_usd, total_eur, cash_usd, positions_value_usd
                   FROM equity_snapshots
                   WHERE {where_clause}
                   ORDER BY timestamp ASC""",
                params,
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
    """
    DCA-Depot = das, was Mert WIRKLICH haelt.

    Quelle ist der portfolio-Block in config.yaml (laut CLAUDE.md die einzige
    Wahrheitsquelle fuer Depot-Daten). Vorher (bis 2026-07-27) baute dieser
    Endpunkt das Depot aus dem Empfehlungs-Protokoll zusammen - allen
    feedback_reasons vom Typ 'dca_bought'. Das driftete zwangslaeufig von der
    Realitaet weg, sobald Mert etwas anderes kaufte als vorgeschlagen oder
    etwas verkaufte:
      - verkaufte Positionen verschwanden NIE (dca_sold wurde nie abgefragt),
        ORCL stand am 27.07. noch mit -32% im Depot, verkauft am 10.07.
      - im Juli stand SPY drin, gekauft wurde VWCE - der Protokoll-Eintrag
        nannte sogar noch ein drittes Papier (XLV)
      - jede Position wurde pauschal mit 50 EUR angesetzt statt mit den
        echten Stueckzahlen
      - ASML wurde ueber die US-Variante bepreist (~1803 USD), gehalten wird
        aber die Amsterdamer in EUR (~1585 EUR) -> ~5% falscher Wert
    Das Empfehlungs-Protokoll liefert jetzt nur noch die Begruendung zur
    Position, nicht mehr die Position selbst.

    STAND 2026-08-10 — DIESE ANSICHT IST NICHT MEHR AKTUELL:
    Der Depot-Teil ist an DepotPi uebergeben, der portfolio-Block in der
    config.yaml wird hier nicht mehr gepflegt und ist nachweislich falsch
    (VWCE mit 0,608939 Stueck / 100 EUR statt echter 0,302188 / 50 EUR, und UNH
    mit 0,1361423 Stueck fehlt ganz — Ursache war der Datenverlust vom 01.08.,
    s. buy.persist_config_change). Die Zahlen bleiben trotzdem stehen statt zu
    verschwinden: eine leere Seite sagt Mert nicht, WARUM sie leer ist. Der
    Endpunkt haengt deshalb ein `stale`-Flag an, das die Oberflaeche als Banner
    zeigt. Wer echte Depotdaten will, fragt DepotPi.
    """
    import yaml
    try:
        stale = {
            "stale": True,
            "stale_note": (
                "Nicht mehr gepflegt. Das echte Depot führt seit 10.08.2026 DepotPi — "
                "die Zahlen hier sind eingefroren und nachweislich falsch (VWCE zu hoch, "
                "UNH fehlt ganz)."
            ),
            "stale_since": "2026-08-10",
        }
        empty_summary = {"total_invested_eur": 0, "total_current_eur": 0, "total_pl_eur": 0,
                         "total_pl_pct": 0, "count": 0}
        try:
            with open(PROJECT_DIR / "config.yaml") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": f"config.yaml nicht lesbar: {e}"})

        holdings = cfg.get("portfolio") or {}
        if not holdings:
            return {"dca": [], "closed": [], "summary": empty_summary, **stale}

        default_budget = float((cfg.get("settings") or {}).get("monatliches_budget_eur", 50.0))

        # Begruendungen + Verkaeufe aus dem Lern-Protokoll (nur noch als Beiwerk)
        reasons, sold = {}, []
        if LEARNING_DB.exists():
            try:
                with db_connect(LEARNING_DB) as conn:
                    for r in conn.execute(
                        """SELECT COALESCE(p.subject_id, json_extract(p.output_json,'$.ticker')) AS tk,
                                  json_extract(p.output_json,'$.reason') AS reason,
                                  json_extract(p.output_json,'$.confidence') AS confidence,
                                  p.created_at AS recommended_at, fr.created_at AS bought_at
                             FROM feedback_reasons fr
                             JOIN predictions p ON p.id = fr.prediction_id
                            WHERE fr.feedback_type = 'dca_bought'
                            ORDER BY fr.created_at ASC"""
                    ).fetchall():
                        if r["tk"]:
                            reasons[str(r["tk"]).upper()] = dict(r)
                    for r in conn.execute(
                        """SELECT COALESCE(p.subject_id, json_extract(p.output_json,'$.ticker')) AS tk,
                                  fr.created_at AS sold_at, fr.reason_text AS note
                             FROM feedback_reasons fr
                             JOIN predictions p ON p.id = fr.prediction_id
                            WHERE fr.feedback_type = 'dca_sold'
                            ORDER BY fr.created_at DESC"""
                    ).fetchall():
                        note = r["note"] or ""
                        tk = r["tk"]
                        if not tk:   # Ticker steht teils nur im Freitext
                            m = re.search(r"\b([A-Z]{2,5})\b", note)
                            tk = m.group(1) if m else None
                        sold.append({"ticker": tk, "sold_at": r["sold_at"], "note": note[:200]})
            except Exception:
                pass

        import yfinance as yf
        fx_rate = _get_broker().get_account().fx_rate    # USD -> EUR

        result, total_invested, total_current = [], 0.0, 0.0
        for key, h in holdings.items():
            h = h or {}
            # Boersenplatz: der config-Schluessel ist nicht immer das yfinance-Symbol
            symbol = h.get("yf_symbol") or key
            currency = (h.get("currency") or "USD").upper()
            invested = float(h.get("invested_eur") or default_budget)
            shares = h.get("shares")
            buy_price = h.get("avg_buy_price")

            entry = {
                "ticker": key,
                "symbol": symbol,
                "currency": currency,
                "shares": round(float(shares), 6) if shares else None,
                "buy_price": round(float(buy_price), 2) if buy_price else None,
                "invested_eur": round(invested, 2),
                "current_price": None,
                "current_value_eur": round(invested, 2),   # Fallback: Einstand
                "performance_pct": None,
                "note": h.get("note") or "",
                "recommended_at": None, "bought_at": None,
                "reason": "", "confidence": "?",
            }
            meta = reasons.get(key.upper()) or reasons.get(symbol.upper())
            if meta:
                entry["recommended_at"] = meta.get("recommended_at")
                entry["bought_at"] = meta.get("bought_at")
                entry["reason"] = (meta.get("reason") or "")[:200]
                entry["confidence"] = meta.get("confidence") or "?"
            if not entry["bought_at"] and h.get("date_first"):
                entry["bought_at"] = str(h["date_first"])

            try:
                hist = yf.Ticker(symbol).history(period="5d")
                hist = hist[hist["Close"].notna()]        # s. Vorfall 2026-07-27
                if not hist.empty:
                    price = float(hist["Close"].iloc[-1])
                    entry["current_price"] = round(price, 2)
                    # Stueckzahl: echt aus config, sonst aus Einstand herleiten
                    n = float(shares) if shares else None
                    if n is None and buy_price and float(buy_price) > 0:
                        base = invested if currency == "EUR" else invested / fx_rate
                        n = base / float(buy_price)
                    if n:
                        value = n * price
                        entry["current_value_eur"] = round(value if currency == "EUR" else value * fx_rate, 2)
                    if buy_price and float(buy_price) > 0:
                        entry["performance_pct"] = round((price / float(buy_price) - 1) * 100, 2)
            except Exception:
                pass

            total_invested += invested
            total_current += entry["current_value_eur"]
            result.append(entry)

        result.sort(key=lambda x: x["current_value_eur"], reverse=True)
        total_pl = total_current - total_invested
        # Barbestand: der Broker zeigt als "Depotkonto" Positionen PLUS freien Saldo.
        # Ohne den fehlten 0,10 EUR und die Summe wich vom Broker ab (2026-07-27).
        # Bewusst NICHT in Einstand/Rendite eingerechnet - uninvestiertes Geld hat
        # keine Rendite und wuerde die Prozentzahl verwaessern.
        cash_eur = float((cfg.get("settings") or {}).get("dca_cash_eur", 0.0) or 0.0)
        summary = {
            "total_invested_eur": round(total_invested, 2),
            "total_current_eur": round(total_current, 2),
            "cash_eur": round(cash_eur, 2),
            "total_account_eur": round(total_current + cash_eur, 2),
            "total_pl_eur": round(total_pl, 2),
            "total_pl_pct": round((total_pl / total_invested * 100) if total_invested > 0 else 0, 2),
            "count": len(result),
            "fx_rate": round(fx_rate, 4),
        }
        # verkauft = nicht mehr im Depot, bleibt aber in der Bilanz sichtbar
        closed = [s for s in sold if str(s.get("ticker") or "").upper() not in
                  {k.upper() for k in holdings} ]
        return {"dca": result, "closed": closed, "summary": summary, **stale}
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

            # Die Liste der Jobs wird aus systemd selbst gelesen statt hartkodiert
            # (Fix 2026-07-27). Vorher stand hier eine Liste, die nicht mehr stimmte:
            # 5 Jobs existierten gar nicht mehr, "invest-pi-strategy" hiess laengst
            # "invest-pi-strategy-hourly", und alle 4 ai-swing-Jobs fehlten. Die
            # Systemseite zeigte also den wichtigsten Job dauerhaft rot, obwohl er
            # lief — Dauer-Rot macht einen echten Ausfall unsichtbar.
            installed = subprocess.run(
                ["systemctl", "list-unit-files", "invest-pi-*.timer",
                 "--no-pager", "--plain", "--no-legend"],
                capture_output=True, text=True, timeout=5
            )
            all_timers = []
            for line in installed.stdout.strip().splitlines():
                parts = line.split()
                if parts and parts[0].endswith(".timer"):
                    all_timers.append(parts[0].replace(".timer", ""))
            for t in sorted(all_timers):
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
    """Detaillierte Performance-Metriken — nutzt live Broker-Daten."""
    try:
        if not TRADING_DB.exists():
            return {"error": "no data"}

        broker = _get_broker()
        account = broker.get_account()
        current_equity = account.equity_usd

        with db_connect(TRADING_DB) as conn:
            first = conn.execute(
                """SELECT timestamp, total_usd FROM equity_snapshots
                   WHERE source='paper' AND total_usd IS NOT NULL
                   ORDER BY timestamp ASC LIMIT 1"""
            ).fetchone()

            if not first or not first["total_usd"]:
                return {"snapshots_count": 0}

            start_equity = first["total_usd"]
            total_return_usd = current_equity - start_equity
            total_return_pct = (total_return_usd / start_equity) * 100

            trade_stats = conn.execute(
                """SELECT
                       COUNT(*) as total_trades,
                       SUM(CASE WHEN side='buy' THEN 1 ELSE 0 END) as buys,
                       SUM(CASE WHEN side='sell' THEN 1 ELSE 0 END) as sells,
                       SUM(eur_value) as total_volume_eur
                   FROM trades WHERE status='filled' AND source='paper'"""
            ).fetchone()

        return {
            "total_return_pct": round(total_return_pct, 3),
            "total_return_usd": round(total_return_usd, 2),
            "start_equity": round(start_equity, 2),
            "current_equity": round(current_equity, 2),
            "total_trades": (trade_stats["total_trades"] or 0) if trade_stats else 0,
            "total_buys": (trade_stats["buys"] or 0) if trade_stats else 0,
            "total_sells": (trade_stats["sells"] or 0) if trade_stats else 0,
            "total_volume_eur": round(float(trade_stats["total_volume_eur"] or 0), 2) if trade_stats else 0,
            "data_since": first["timestamp"][:10] if first else None,
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


# ─── Benchmark ───────────────────────────────────────────────
_bench_cache: dict = {"data": None, "expires": 0}


@app.get("/api/benchmark")
def benchmark():
    """SPY Benchmark-Vergleich seit Portfolio-Start."""
    global _bench_cache
    now = time.time()
    if _bench_cache["expires"] > now and _bench_cache["data"]:
        return _bench_cache["data"]

    try:
        import yfinance as yf

        # Portfolio start date from first equity snapshot
        start_date = "2026-04-29"
        if TRADING_DB.exists():
            with db_connect(TRADING_DB) as conn:
                first = conn.execute(
                    """SELECT timestamp FROM equity_snapshots
                       WHERE source='paper' AND total_usd IS NOT NULL
                       ORDER BY timestamp ASC LIMIT 1"""
                ).fetchone()
                if first:
                    start_date = first["timestamp"][:10]

        # SPY benchmark
        spy = yf.Ticker("SPY")
        spy_hist = spy.history(start=start_date)
        # Zeilen ohne Schlusskurs raus (Vorfall 2026-07-27): Yahoo liefert manchmal einen
        # Tag mit Volumen aber OHLC=NaN. Ungefiltert wurde spy_now=NaN -> die Antwort war
        # nicht JSON-kodierbar -> dieser Endpunkt lieferte 500 statt Benchmark-Vergleich.
        spy_hist = spy_hist[spy_hist["Close"].notna()]
        if spy_hist.empty:
            return {"error": "no SPY data"}

        spy_start = float(spy_hist["Close"].iloc[0])
        spy_now = float(spy_hist["Close"].iloc[-1])
        spy_return_pct = (spy_now / spy_start - 1) * 100

        # SPY daily returns for chart overlay
        spy_daily = []
        for idx, row in spy_hist.iterrows():
            spy_daily.append({
                "date": idx.strftime("%Y-%m-%d"),
                "price": round(float(row["Close"]), 2),
                "return_pct": round((float(row["Close"]) / spy_start - 1) * 100, 3),
            })

        # Portfolio returns
        broker = _get_broker()
        account = broker.get_account()
        positions = broker.get_positions()

        # Total account return (includes cash drag)
        total_return_pct = 0.0
        if TRADING_DB.exists():
            with db_connect(TRADING_DB) as conn:
                first_eq = conn.execute(
                    """SELECT total_usd FROM equity_snapshots
                       WHERE source='paper' AND total_usd IS NOT NULL
                       ORDER BY timestamp ASC LIMIT 1"""
                ).fetchone()
                if first_eq and first_eq["total_usd"]:
                    total_return_pct = (account.equity_usd / first_eq["total_usd"] - 1) * 100

        # Invested capital return (excludes cash drag)
        cost_basis = sum(p.avg_price * p.qty for p in positions if p.avg_price)
        market_value = sum(p.market_price * p.qty for p in positions)
        positions_return_pct = ((market_value / cost_basis - 1) * 100) if cost_basis > 0 else 0

        # Alpha = portfolio positions return - benchmark
        alpha_pct = positions_return_pct - spy_return_pct
        invested_pct = (cost_basis / account.equity_usd * 100) if account.equity_usd else 0

        result = {
            "benchmark": "SPY",
            "start_date": start_date,
            "spy_return_pct": round(spy_return_pct, 3),
            "spy_start": round(spy_start, 2),
            "spy_now": round(spy_now, 2),
            "portfolio_total_return_pct": round(total_return_pct, 3),
            "portfolio_positions_return_pct": round(positions_return_pct, 3),
            "alpha_pct": round(alpha_pct, 3),
            "invested_pct": round(invested_pct, 1),
            "spy_daily": spy_daily,
        }

        _bench_cache = {"data": result, "expires": now + 900}  # 15 min cache
        return result
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ─── DayPi (Schwester-System · Intraday-Day-Trader, eigenes Paper-Konto) ──
DAYPI_SNAPSHOT = Path("/home/pi/daytrader/data/daypi_public.json")


@app.get("/api/daypi")
def daypi():
    """Spiegelt DayPis welt-lesbares Snapshot (entkoppelte Brücke, kein Cross-User-Key-Zugriff)."""
    import json as _json
    try:
        if not DAYPI_SNAPSHOT.exists():
            return {"has_data": False, "error": "DayPi-Snapshot noch nicht vorhanden"}
        with open(DAYPI_SNAPSHOT) as f:
            return _json.load(f)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ─── KI-Swing (Schwester-Strategie · 2. Paper-Konto, source='ai_swing') ──
# WICHTIG: Alle Queries filtern strikt auf source='ai_swing'. Die Momentum-Zahlen
# (source='paper') werden NIE angefasst — getrennte Ledger, getrennte Konten.
_ai_swing_perf_cache: dict = {"data": None, "expires": 0}


def _spearman(xs: list, ys: list):
    """Tie-aware Spearman rank correlation. scipy wenn da, sonst manueller Fallback."""
    n = len(xs)
    if n < 3:
        return None
    try:
        from scipy.stats import spearmanr  # type: ignore
        rho, _ = spearmanr(xs, ys)
        if rho != rho:  # NaN
            return None
        return float(rho)
    except Exception:
        pass

    def _ranks(vals):
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        ranks = [0.0] * len(vals)
        i = 0
        while i < len(vals):
            j = i
            while j + 1 < len(vals) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0  # 1-based average rank for ties
            for k in range(i, j + 1):
                ranks[order[k]] = avg
            i = j + 1
        return ranks

    rx, ry = _ranks(xs), _ranks(ys)
    mx = sum(rx) / n
    my = sum(ry) / n
    cov = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    vx = sum((rx[i] - mx) ** 2 for i in range(n))
    vy = sum((ry[i] - my) ** 2 for i in range(n))
    if vx <= 0 or vy <= 0:
        return None
    return cov / (vx ** 0.5 * vy ** 0.5)


@app.get("/api/ai-swing/equity-history")
def ai_swing_equity_history(days: int = 0):
    try:
        if not TRADING_DB.exists():
            return {"snapshots": []}
        where_clause = "source = 'ai_swing' AND total_usd IS NOT NULL"
        params: list = []
        if days > 0:
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            where_clause += " AND timestamp >= ?"
            params.append(cutoff)
        with db_connect(TRADING_DB) as conn:
            rows = conn.execute(
                f"""SELECT timestamp, total_usd, total_eur, cash_usd, positions_value_usd
                   FROM equity_snapshots
                   WHERE {where_clause}
                   ORDER BY timestamp ASC""",
                params,
            ).fetchall()
            result = [
                {
                    "timestamp": r["timestamp"],
                    "total_usd": r["total_usd"],
                    "total_eur": r["total_eur"],
                    "cash_usd": r["cash_usd"],
                    "positions_value_usd": r["positions_value_usd"],
                }
                for r in rows
            ]
        return {"snapshots": result}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/ai-swing/positions")
def ai_swing_positions():
    # Live vom 2. Paper-Konto (wie /api/positions fuer Momentum) -> USD-native Felder,
    # damit die Anzeige keine EUR-Werte mit $-Zeichen mehr zeigt.
    try:
        import os
        from src.broker import get_broker
        key2 = os.environ.get("ALPACA_API_KEY_2")
        sec2 = os.environ.get("ALPACA_API_SECRET_2")
        if not (key2 and sec2):
            return {"positions": []}
        broker = get_broker("alpaca_paper", api_key=key2, api_secret=sec2)
        result = []
        for p in broker.get_positions():
            pct_change = 0.0
            if p.avg_price and p.avg_price > 0:
                pct_change = ((p.market_price - p.avg_price) / p.avg_price) * 100
            result.append({
                "ticker": p.ticker,
                "qty": p.qty,
                "avg_price": round(p.avg_price, 2),
                "market_price": round(p.market_price, 2),
                "market_value_usd": round(p.qty * p.market_price, 2),
                "market_value_eur": round(p.market_value_eur, 2),
                "pct_change": round(pct_change, 2),
            })
        result.sort(key=lambda x: x["market_value_usd"], reverse=True)
        return {"positions": result}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/ai-swing/picks")
def ai_swing_picks(weeks: int = 4):
    try:
        if not AI_SWING_DB.exists():
            return {"picks": []}
        with db_connect(AI_SWING_DB) as conn:
            decisions = conn.execute(
                """SELECT id, run_date FROM decisions
                   WHERE mode='shadow'
                   ORDER BY run_date DESC, id DESC LIMIT ?""",
                (max(1, weeks),),
            ).fetchall()
            result = []
            for d in decisions:
                picks = conn.execute(
                    """SELECT ticker, conviction, entry_price, thesis
                       FROM picks WHERE decision_id = ? ORDER BY id ASC""",
                    (d["id"],),
                ).fetchall()
                for pk in picks:
                    fwd = conn.execute(
                        """SELECT fwd_return FROM outcomes
                           WHERE decision_id = ? AND ticker = ? AND horizon_days = 20
                           ORDER BY measured_at DESC LIMIT 1""",
                        (d["id"], pk["ticker"]),
                    ).fetchone()
                    result.append({
                        "date": (d["run_date"] or "")[:10],
                        "ticker": pk["ticker"],
                        "conviction": pk["conviction"],
                        "entry_price": pk["entry_price"],
                        "thesis": pk["thesis"],
                        "fwd_return": fwd["fwd_return"] if fwd else None,
                    })
        return {"picks": result}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/ai-swing/performance")
def ai_swing_performance():
    """Nie 500 — bei Fehler/leerer DB gueltiger Null-Default mit phase=warmup."""
    global _ai_swing_perf_cache
    now = time.time()
    if _ai_swing_perf_cache["expires"] > now and _ai_swing_perf_cache["data"]:
        return _ai_swing_perf_cache["data"]

    default = {
        "current_equity_usd": None,
        "total_eur": None,
        "return_pct_since_start": None,
        "n_positions": 0,
        "n_picks_total": 0,
        "phase": "live_paper_warmup",
        "alpha_vs_momentum": None,
        "alpha_vs_spy": None,
        "ic": None,
        "data_since": None,
    }
    try:
        result = dict(default)

        # ai_swing equity window (trading.db)
        first = last = None
        if TRADING_DB.exists():
            with db_connect(TRADING_DB) as conn:
                first = conn.execute(
                    """SELECT timestamp, total_usd FROM equity_snapshots
                       WHERE source='ai_swing' AND total_usd IS NOT NULL
                       ORDER BY timestamp ASC LIMIT 1"""
                ).fetchone()
                last = conn.execute(
                    """SELECT timestamp, total_usd, total_eur FROM equity_snapshots
                       WHERE source='ai_swing' AND total_usd IS NOT NULL
                       ORDER BY timestamp DESC LIMIT 1"""
                ).fetchone()
                result["n_positions"] = conn.execute(
                    "SELECT COUNT(*) c FROM positions WHERE source='ai_swing' AND qty > 0"
                ).fetchone()["c"]

        ai_ret = None
        if first and last and first["total_usd"]:
            result["phase"] = "live_paper"
            result["current_equity_usd"] = round(last["total_usd"], 2)
            result["total_eur"] = round(last["total_eur"], 2) if last["total_eur"] is not None else None
            ai_ret = (last["total_usd"] / first["total_usd"] - 1) * 100
            result["return_pct_since_start"] = round(ai_ret, 3)
            result["data_since"] = first["timestamp"][:10]

        # picks + IC (ai_swing.db)
        conv_map = {"high": 3, "medium": 2, "low": 1}
        if AI_SWING_DB.exists():
            with db_connect(AI_SWING_DB) as conn:
                result["n_picks_total"] = conn.execute(
                    "SELECT COUNT(*) c FROM picks"
                ).fetchone()["c"]
                ic_rows = conn.execute(
                    """SELECT p.conviction AS conviction, o.fwd_return AS fwd
                       FROM picks p
                       JOIN outcomes o
                         ON o.decision_id = p.decision_id AND o.ticker = p.ticker
                       WHERE o.horizon_days = 20 AND o.fwd_return IS NOT NULL
                         AND p.conviction IS NOT NULL"""
                ).fetchall()
            if len(ic_rows) >= 3:
                xs = [conv_map.get(r["conviction"], 0) for r in ic_rows]
                ys = [r["fwd"] for r in ic_rows]
                v = _spearman(xs, ys)
                result["ic"] = round(v, 4) if v is not None else None

        # alpha vs momentum (same window, source='paper')
        if ai_ret is not None and result["n_positions"] > 0 and first and TRADING_DB.exists():
            with db_connect(TRADING_DB) as conn:
                m_first = conn.execute(
                    """SELECT total_usd FROM equity_snapshots
                       WHERE source='paper' AND total_usd IS NOT NULL AND timestamp >= ?
                       ORDER BY timestamp ASC LIMIT 1""",
                    (first["timestamp"],),
                ).fetchone()
                m_last = conn.execute(
                    """SELECT total_usd FROM equity_snapshots
                       WHERE source='paper' AND total_usd IS NOT NULL AND timestamp <= ?
                       ORDER BY timestamp DESC LIMIT 1""",
                    (last["timestamp"],),
                ).fetchone()
            if m_first and m_last and m_first["total_usd"]:
                m_ret = (m_last["total_usd"] / m_first["total_usd"] - 1) * 100
                result["alpha_vs_momentum"] = round(ai_ret - m_ret, 3)

        # alpha vs SPY (same window, yfinance)
        if ai_ret is not None and result["n_positions"] > 0 and result["data_since"]:
            try:
                import yfinance as yf
                spy_hist = yf.Ticker("SPY").history(start=result["data_since"])
                spy_hist = spy_hist[spy_hist["Close"].notna()]   # s. /api/benchmark (2026-07-27)
                if not spy_hist.empty:
                    spy_start = float(spy_hist["Close"].iloc[0])
                    spy_now = float(spy_hist["Close"].iloc[-1])
                    spy_ret = (spy_now / spy_start - 1) * 100
                    result["alpha_vs_spy"] = round(ai_ret - spy_ret, 3)
            except Exception:
                pass

        _ai_swing_perf_cache = {"data": result, "expires": now + 300}
        return result
    except Exception:
        return default


# ─── Static Files & Frontend ─────────────────────────────────
STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
def serve_index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ─── Entry Point ─────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8200, log_level="info")
