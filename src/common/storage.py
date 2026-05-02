"""
Zentrale SQLite-Verwaltung fuer das Invest-Pi-System.

Fuenf Datenbanken (bewusst getrennt fuer granulare Backups):
  - market.db    OHLCV, Fundamentals
  - patterns.db  Pre-Drawdown-Muster
  - alerts.db    Risk-Score-Historie
  - learning.db  predictions + outcomes + feedback (Self-Learning-Loop)
  - trading.db   trades + positions + equity (Paper-Trading-State)
"""

from __future__ import annotations

import os as _os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

# Pfade. Override via INVEST_PI_DATA_DIR.
_DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
DATA_DIR = Path(_os.environ.get("INVEST_PI_DATA_DIR", str(_DEFAULT_DATA_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)

MARKET_DB    = DATA_DIR / "market.db"
PATTERNS_DB  = DATA_DIR / "patterns.db"
ALERTS_DB    = DATA_DIR / "alerts.db"
LEARNING_DB  = DATA_DIR / "learning.db"
TRADING_DB   = DATA_DIR / "trading.db"


SCHEMA_MARKET = """
CREATE TABLE IF NOT EXISTS prices (
    ticker      TEXT    NOT NULL,
    date        TEXT    NOT NULL,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL    NOT NULL,
    volume      INTEGER,
    PRIMARY KEY (ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_prices_ticker ON prices(ticker);
CREATE INDEX IF NOT EXISTS idx_prices_date   ON prices(date);

CREATE TABLE IF NOT EXISTS fundamentals (
    ticker       TEXT PRIMARY KEY,
    name         TEXT,
    sector       TEXT,
    market_cap   REAL,
    pe_ratio     REAL,
    pb_ratio     REAL,
    dividend_yld REAL,
    beta         REAL,
    updated_at   TEXT
);
"""

SCHEMA_PATTERNS = """
CREATE TABLE IF NOT EXISTS drawdown_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    peak_date       TEXT NOT NULL,
    peak_price      REAL NOT NULL,
    trough_date     TEXT NOT NULL,
    trough_price    REAL NOT NULL,
    drawdown_pct    REAL NOT NULL,
    days_to_trough  INTEGER NOT NULL,
    recovery_days   INTEGER,
    regime          TEXT,
    UNIQUE (ticker, peak_date)
);
CREATE INDEX IF NOT EXISTS idx_dd_ticker ON drawdown_events(ticker);

CREATE TABLE IF NOT EXISTS pre_drawdown_features (
    event_id          INTEGER NOT NULL,
    lookback_days     INTEGER NOT NULL,
    ret_30d           REAL,
    ret_90d           REAL,
    ret_180d          REAL,
    volatility_30d    REAL,
    rsi_14            REAL,
    price_vs_ma50     REAL,
    price_vs_ma200    REAL,
    volume_trend_30d  REAL,
    drawdown_prior_y  REAL,
    PRIMARY KEY (event_id, lookback_days),
    FOREIGN KEY (event_id) REFERENCES drawdown_events(id)
);
"""

SCHEMA_ALERTS = """
CREATE TABLE IF NOT EXISTS risk_scores (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker         TEXT NOT NULL,
    timestamp      TEXT NOT NULL,
    composite      REAL NOT NULL,
    alert_level    INTEGER NOT NULL,
    triggered_n    INTEGER NOT NULL,
    dimensions_js  TEXT NOT NULL,
    prediction_id  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_rs_ticker_ts ON risk_scores(ticker, timestamp);

CREATE TABLE IF NOT EXISTS notifications (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker         TEXT NOT NULL,
    timestamp      TEXT NOT NULL,
    level          INTEGER NOT NULL,
    channel        TEXT,
    delivered      INTEGER NOT NULL,
    payload        TEXT,
    prediction_id  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_notif_pred ON notifications(prediction_id);
"""

SCHEMA_LEARNING = """
CREATE TABLE IF NOT EXISTS predictions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at            TEXT    NOT NULL DEFAULT (datetime('now')),
    job_source            TEXT    NOT NULL,
    model                 TEXT    NOT NULL,
    prompt_hash           TEXT,
    input_hash            TEXT,
    input_summary         TEXT,
    output_json           TEXT,
    confidence            TEXT,
    subject_type          TEXT,
    subject_id            TEXT,
    input_tokens          INTEGER,
    output_tokens         INTEGER,
    cost_estimate_eur     REAL    DEFAULT 0,
    outcome_json          TEXT,
    outcome_measured_at   TEXT,
    outcome_correct       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pred_job_date ON predictions(job_source, created_at);
CREATE INDEX IF NOT EXISTS idx_pred_subject  ON predictions(subject_type, subject_id);
CREATE INDEX IF NOT EXISTS idx_pred_outcome  ON predictions(outcome_correct);

CREATE TABLE IF NOT EXISTS feedback_reasons (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id  INTEGER NOT NULL,
    feedback_type  TEXT    NOT NULL,
    reason_code    TEXT,
    reason_text    TEXT,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (prediction_id) REFERENCES predictions(id)
);
CREATE INDEX IF NOT EXISTS idx_fb_pred ON feedback_reasons(prediction_id);
CREATE INDEX IF NOT EXISTS idx_fb_type ON feedback_reasons(feedback_type, reason_code);

CREATE TABLE IF NOT EXISTS cost_ledger (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp      TEXT NOT NULL DEFAULT (datetime('now')),
    api            TEXT NOT NULL,
    job_source     TEXT,
    cost_eur       REAL NOT NULL,
    prediction_id  INTEGER,
    notes          TEXT
);
CREATE INDEX IF NOT EXISTS idx_cost_ts  ON cost_ledger(timestamp);
CREATE INDEX IF NOT EXISTS idx_cost_api ON cost_ledger(api, timestamp);

CREATE TABLE IF NOT EXISTS meta_reviews (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    period_start   TEXT NOT NULL,
    period_end     TEXT NOT NULL,
    job_source     TEXT NOT NULL,
    summary_md     TEXT,
    action_plan_js TEXT,
    prediction_id  INTEGER,
    FOREIGN KEY (prediction_id) REFERENCES predictions(id)
);

CREATE TABLE IF NOT EXISTS reflections (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    prediction_id  INTEGER NOT NULL,
    ticker         TEXT NOT NULL,
    alert_level    INTEGER,
    outcome_correct INTEGER,
    reflection_md  TEXT NOT NULL,
    dimension_blame TEXT,
    return_7d      REAL,
    max_dd_7d      REAL,
    FOREIGN KEY (prediction_id) REFERENCES predictions(id)
);
CREATE INDEX IF NOT EXISTS idx_refl_ticker ON reflections(ticker, created_at);
CREATE INDEX IF NOT EXISTS idx_refl_pred   ON reflections(prediction_id);

CREATE TABLE IF NOT EXISTS weight_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    weights_json   TEXT NOT NULL,
    source         TEXT NOT NULL DEFAULT 'auto_optimizer',
    notes          TEXT
);

CREATE TABLE IF NOT EXISTS regime_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    regime_label   TEXT NOT NULL,
    probability    REAL,
    method         TEXT,
    vix_level      REAL,
    prediction_id  INTEGER,
    FOREIGN KEY (prediction_id) REFERENCES predictions(id)
);
CREATE INDEX IF NOT EXISTS idx_regime_date ON regime_snapshots(created_at);
CREATE INDEX IF NOT EXISTS idx_regime_pred ON regime_snapshots(prediction_id);

CREATE TABLE IF NOT EXISTS config_patch_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    meta_review_id INTEGER,
    path           TEXT NOT NULL,
    old_value      TEXT,
    new_value      TEXT,
    accepted       INTEGER NOT NULL DEFAULT 0,
    applied_at     TEXT,
    reason         TEXT,
    source         TEXT NOT NULL DEFAULT 'meta_review'
);
CREATE INDEX IF NOT EXISTS idx_cpatch_pending ON config_patch_log(accepted, applied_at);
"""


SCHEMA_TRADING = """
CREATE TABLE IF NOT EXISTS trades (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    ticker            TEXT NOT NULL,
    side              TEXT NOT NULL,
    qty               REAL NOT NULL,
    eur_value         REAL,
    price             REAL,
    order_type        TEXT,
    status            TEXT NOT NULL,
    broker_order_id   TEXT,
    strategy_label    TEXT,
    prediction_id     INTEGER,
    fill_ts           TEXT,
    fill_price        REAL,
    fees_eur          REAL DEFAULT 0,
    notes             TEXT,
    source            TEXT NOT NULL DEFAULT 'paper'
);
CREATE INDEX IF NOT EXISTS idx_trades_ticker_ts ON trades(ticker, created_at);
CREATE INDEX IF NOT EXISTS idx_trades_status    ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_pred      ON trades(prediction_id);

CREATE TABLE IF NOT EXISTS positions (
    ticker            TEXT NOT NULL,
    qty               REAL NOT NULL,
    avg_price_eur     REAL,
    opened_at         TEXT,
    last_updated      TEXT NOT NULL DEFAULT (datetime('now')),
    stop_loss_price   REAL,
    peak_price        REAL,
    peak_seen_at      TEXT,
    strategy_label    TEXT,
    source            TEXT NOT NULL DEFAULT 'paper',
    PRIMARY KEY (ticker, source)
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp           TEXT NOT NULL DEFAULT (datetime('now')),
    cash_eur            REAL NOT NULL,
    positions_value_eur REAL NOT NULL,
    total_eur           REAL NOT NULL,
    cash_usd            REAL,
    positions_value_usd REAL,
    total_usd           REAL,
    fx_rate             REAL,
    source              TEXT NOT NULL DEFAULT 'paper',
    notes               TEXT
);
CREATE INDEX IF NOT EXISTS idx_eq_ts_src ON equity_snapshots(source, timestamp);
"""


@contextmanager
def connect(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        conn.execute("PRAGMA journal_mode = WAL;").fetchone()
    except sqlite3.OperationalError:
        pass
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()




def _migrate_positions() -> None:
    """Idempotente ALTER TABLE-Migrationen fuer positions-Tabelle."""
    new_cols = [
        ("peak_price",     "REAL"),
        ("peak_seen_at",   "TEXT"),
        ("strategy_label", "TEXT"),
    ]
    if not TRADING_DB.exists():
        return
    with connect(TRADING_DB) as conn:
        existing = {r[1] for r in conn.execute("PRAGMA table_info(positions)").fetchall()}
        for col, typ in new_cols:
            if col not in existing:
                try:
                    conn.execute(f"ALTER TABLE positions ADD COLUMN {col} {typ}")
                except Exception:
                    pass


def _migrate_equity_snapshots() -> None:
    """Idempotente ALTER TABLE-Migrationen (fuer existierende DBs)."""
    new_cols = [
        ("cash_usd",            "REAL"),
        ("positions_value_usd", "REAL"),
        ("total_usd",           "REAL"),
        ("fx_rate",             "REAL"),
    ]
    if not TRADING_DB.exists():
        return
    with connect(TRADING_DB) as conn:
        existing = {r[1] for r in conn.execute("PRAGMA table_info(equity_snapshots)").fetchall()}
        for col, typ in new_cols:
            if col not in existing:
                try:
                    conn.execute(f"ALTER TABLE equity_snapshots ADD COLUMN {col} {typ}")
                except Exception:
                    pass

def init_all() -> None:
    """Erstellt alle fuenf Datenbanken mit ihren Schemas. Idempotent."""
    configs = [
        (MARKET_DB,    SCHEMA_MARKET),
        (PATTERNS_DB,  SCHEMA_PATTERNS),
        (ALERTS_DB,    SCHEMA_ALERTS),
        (LEARNING_DB,  SCHEMA_LEARNING),
        (TRADING_DB,   SCHEMA_TRADING),
    ]
    for path, schema in configs:
        with connect(path) as conn:
            conn.executescript(schema)
    # Migrationen fuer bestehende DBs
    _migrate_positions()
    _migrate_equity_snapshots()
