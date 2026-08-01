#!/usr/bin/env python3
"""
pm_context.py — Das vollstaendige Lagebild fuer den Portfolio-Manager.

WARUM ES DAS GIBT:
Der alte Sparplan sah pro Monat genau eine Sache: eine Liste von vier Risk-Scores.
Ein Portfolio-Manager braucht das Gegenteil — das GANZE Bild: was halte ich, warum
halte ich es, wie steht es da, was gibt es sonst, und wie ist die Grosswetterlage.

Alle Bausteine dafuer lagen im Projekt bereits herum und wurden vom Sparplan nie
benutzt: Bewertungskennzahlen (fundamentals-Tabelle), Nachrichten-Stimmung, naechste
Quartalstermine, Konjunkturdaten, Marktbreite, Regime-Erkennung, Korrelation zwischen
den eigenen Positionen. Dieses Modul traegt sie zu EINEM Bild zusammen.

Alles ist ausfallsicher: faellt eine Quelle aus, fehlt das Feld — das Lagebild bleibt
nutzbar. Ein Portfolio-Manager, der bei einer kaputten Nachrichten-API gar nichts mehr
entscheidet, ist schlechter als einer, der ohne Nachrichten entscheidet.
"""

from __future__ import annotations

import logging

log = logging.getLogger("invest_pi.pm_context")


def _safe(fn, default=None, label: str = ""):
    """Ruft eine Datenquelle auf und schluckt Fehler — Ausfall darf das Lagebild
    nicht kippen, nur ein Feld leer lassen."""
    try:
        return fn()
    except Exception as e:
        log.warning(f"Quelle '{label}' nicht verfuegbar: {e}")
        return default


def _price(ticker: str) -> float | None:
    from .data_loader import get_prices
    px = get_prices(ticker, period="1mo")
    if px is not None and len(px) > 0:
        return float(px["close"].iloc[-1])
    return None


def _yf_map() -> dict[str, str]:
    """Yahoo-Symbol je Depot-Ticker. Steht in config.yaml (z.B. ASML -> ASML.AS,
    die Amsterdamer Notierung), aber NICHT in der Position-Datenklasse — deshalb
    lesen wir die Rohdatei. Ohne das wuerde fuer ASML die US-Notierung abgefragt
    und Gewinn/Verlust waere schlicht falsch."""
    try:
        import yaml
        from .config import CONFIG_PATH
        raw = yaml.safe_load(CONFIG_PATH.read_text()) or {}
        return {k: (v or {}).get("yf_symbol") or k
                for k, v in (raw.get("portfolio") or {}).items()}
    except Exception as e:
        log.warning(f"yf_symbol-Zuordnung nicht lesbar: {e}")
        return {}


def _yf_symbol(ticker: str, cfg) -> str:
    return _yf_map().get(ticker, ticker)


def holdings_picture(cfg) -> list[dict]:
    """Jede Position mit Gewicht, Gewinn/Verlust, Bewertung, naechsten Zahlen,
    Stimmung — und der hinterlegten Anlage-These."""
    from .market_scan import SECTORS, metrics_for
    from .thesis_store import open_theses

    theses = _safe(open_theses, {}, "thesis_store") or {}
    total = sum(p.invested_eur or 0 for p in cfg.portfolio.values()) or 1.0

    out = []
    for t, p in cfg.portfolio.items():
        sym = _yf_symbol(t, cfg)
        cur = _safe(lambda: _price(sym), None, f"kurs {t}")
        entry = p.avg_buy_price
        pnl = (cur / entry - 1) if (cur and entry) else None

        row = {
            "ticker":        t,
            "invested_eur":  round(p.invested_eur or 0, 2),
            "weight_pct":    round((p.invested_eur or 0) / total * 100, 1),
            "sector":        SECTORS.get(t.upper(), "?"),
            "avg_buy_price": entry,
            "current_price": round(cur, 2) if cur else None,
            "pnl_pct":       round(pnl * 100, 1) if pnl is not None else None,
            "held_since":    p.date_first,
        }

        m = _safe(lambda: metrics_for(sym), None, f"kennzahlen {t}")
        if m:
            row.update({k: m[k] for k in
                        ("mom_3m", "mom_6m", "mom_12m", "mom_12_1",
                         "vs_200d", "dd_from_52w_high", "vol_annual") if k in m})

        f = _safe(lambda: __import__("src.common.data_loader", fromlist=["x"])
                  .get_fundamentals(sym), None, f"fundamentaldaten {t}")
        if f:
            row["valuation"] = {k: f.get(k) for k in
                                ("pe_ratio", "pb_ratio", "dividend_yld", "beta")
                                if f.get(k) is not None}

        e = _safe(lambda: __import__("src.alerts.earnings", fromlist=["x"])
                  .compute_earnings_risk(sym), None, f"quartalstermin {t}")
        if e:
            row["earnings"] = {k: e[k] for k in ("next_date", "days_until", "in_window")
                               if k in e}

        s = _safe(lambda: __import__("src.alerts.sentiment", fromlist=["x"])
                  .compute_sentiment_score(sym), None, f"stimmung {t}")
        if s:
            row["news_sentiment"] = {k: s[k] for k in ("score", "label", "n_headlines")
                                     if k in s}

        th = theses.get(t.upper())
        if th:
            row["thesis"] = th["thesis"]
            row["invalidation"] = th["invalidation"]
            row["thesis_reviews"] = th["review_count"]
            row["last_verdict"] = th["last_verdict"]
        else:
            row["thesis"] = None
            row["invalidation"] = None

        out.append(row)

    out.sort(key=lambda r: -r["weight_pct"])
    return out


def market_picture() -> dict:
    """Grosswetterlage: Regime, Marktbreite, Konjunktur."""
    pic: dict = {}

    r = _safe(lambda: __import__("src.learning.regime", fromlist=["x"]).current_regime(),
              None, "regime")
    if r is not None:
        pic["regime"] = {"label": getattr(r, "label", None) or getattr(r, "state", None),
                         "confidence": getattr(r, "confidence", None)}

    b = _safe(lambda: __import__("src.alerts.market_breadth", fromlist=["x"])
              .market_breadth_score(), None, "marktbreite")
    if b:
        pic["breadth"] = {k: b[k] for k in ("score", "triggered", "reasons") if k in b}

    m = _safe(lambda: __import__("src.alerts.fred_signals", fromlist=["x"])
              .macro_risk_score(), None, "konjunktur")
    if m:
        pic["macro"] = {k: m[k] for k in ("score", "label", "reasons", "triggered") if k in m}

    return pic


def cluster_risk(cfg) -> dict | None:
    """Laufen die eigenen Positionen im Gleichschritt? Vier Titel, die dasselbe tun,
    sind keine Streuung — das sieht man nur an der Korrelation, nicht an der Branche."""
    tickers = [_yf_symbol(t, cfg) for t in cfg.portfolio]
    if len(tickers) < 2:
        return None
    return _safe(lambda: __import__("src.learning.correlation", fromlist=["x"])
                 .detect_cluster_risk(tickers), None, "korrelation")


def build(cfg, budget_eur: float, n_candidates: int = 12) -> dict:
    """Das komplette Lagebild fuer den monatlichen Manager-Lauf."""
    from .dca_benchmark import summary_line
    from .market_scan import etf_metrics, top_candidates
    from .pm_rules import (MAX_SINGLE_STOCK_PCT, MIN_POSITIONS_TARGET,
                           TRADES_PER_MONTH, trade_used_this_month)

    holdings = holdings_picture(cfg)
    held = {h["ticker"].upper() for h in holdings}

    cands = _safe(lambda: top_candidates(n=n_candidates), [], "kandidaten-scan") or []
    # Bewertungskennzahlen dazu — Momentum allein sagt nichts darueber, ob ein
    # Titel auf Jahre tragfaehig ist.
    for c in cands:
        f = _safe(lambda: __import__("src.common.data_loader", fromlist=["x"])
                  .get_fundamentals(c["ticker"]), None, f"fundamentaldaten {c['ticker']}")
        if f:
            c["valuation"] = {k: f.get(k) for k in
                              ("pe_ratio", "pb_ratio", "dividend_yld", "beta")
                              if f.get(k) is not None}
        c["already_held"] = c["ticker"].upper() in held

    sectors: dict[str, float] = {}
    for h in holdings:
        sectors[h["sector"]] = round(sectors.get(h["sector"], 0.0) + h["weight_pct"], 1)

    return {
        "budget_eur":        budget_eur,
        "holdings":          holdings,
        "sector_weights_pct": dict(sorted(sectors.items(), key=lambda kv: -kv[1])),
        "cluster_risk":      cluster_risk(cfg),
        "candidates":        cands,
        "etf_options":       _safe(etf_metrics, [], "etf-kennzahlen") or [],
        "market":            market_picture(),
        "benchmark_record":  _safe(summary_line, "", "bilanz") or "",
        "constraints": {
            "trades_per_month":       TRADES_PER_MONTH,
            "trade_already_used":     trade_used_this_month(),
            "max_single_stock_pct":   MAX_SINGLE_STOCK_PCT,
            "min_positions_target":   MIN_POSITIONS_TARGET,
            "broad_etfs_exempt":      True,
        },
    }
