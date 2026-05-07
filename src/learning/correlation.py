"""
Portfolio-Korrelation — empirische Korrelations-Analyse.

Trackt realisierte Korrelationen zwischen gehaltenen Positionen.
Warnt bei Cluster-Risiko (>3 Positionen mit Korrelation >0.8).
Wird in run_strategy buy_pass als zusaetzlicher Filter genutzt.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger("invest_pi.correlation")


def compute_correlation_matrix(
    tickers: list[str],
    lookback_days: int = 60,
) -> Optional[pd.DataFrame]:
    """Berechnet paarweise Return-Korrelationen."""
    if len(tickers) < 2:
        return None

    try:
        from ..common.data_loader import get_prices
    except ImportError:
        return None

    returns = {}
    for t in tickers:
        try:
            prices = get_prices(t, period="6mo")
            if prices is not None and len(prices) > lookback_days // 2:
                r = prices["close"].pct_change().dropna().tail(lookback_days)
                if len(r) > 10:
                    returns[t] = r
        except Exception:
            continue

    if len(returns) < 2:
        return None

    df = pd.DataFrame(returns)
    return df.corr()


def detect_cluster_risk(
    tickers: list[str],
    threshold: float = 0.80,
    min_cluster_size: int = 3,
    lookback_days: int = 60,
) -> list[dict]:
    """
    Findet Cluster von hoch-korrelierten Positionen.

    Returns: Liste von {tickers: [...], avg_corr: float}
    """
    corr = compute_correlation_matrix(tickers, lookback_days)
    if corr is None:
        return []

    clusters = []
    visited = set()

    for t1 in corr.columns:
        if t1 in visited:
            continue
        cluster = [t1]
        for t2 in corr.columns:
            if t2 == t1 or t2 in visited:
                continue
            if abs(corr.loc[t1, t2]) >= threshold:
                cluster.append(t2)

        if len(cluster) >= min_cluster_size:
            pairs = []
            for i, a in enumerate(cluster):
                for b in cluster[i + 1:]:
                    pairs.append(abs(corr.loc[a, b]))
            avg_corr = float(np.mean(pairs)) if pairs else 0
            clusters.append({
                "tickers": cluster,
                "avg_corr": round(avg_corr, 3),
            })
            visited.update(cluster)

    return clusters


def correlation_check_for_buy(
    candidate_ticker: str,
    held_tickers: list[str],
    threshold: float = 0.85,
    max_correlated: int = 4,
    lookback_days: int = 60,
) -> tuple[bool, str]:
    """
    Prueft ob ein Kauf-Kandidat zu stark mit bestehenden Positionen korreliert.

    Returns: (ok_to_buy, reason)
    """
    if not held_tickers:
        return True, "no positions"

    all_tickers = list(set([candidate_ticker] + held_tickers))
    corr = compute_correlation_matrix(all_tickers, lookback_days)
    if corr is None or candidate_ticker not in corr.columns:
        return True, "no correlation data"

    high_corr = []
    for t in held_tickers:
        if t in corr.columns:
            c = abs(corr.loc[candidate_ticker, t])
            if c >= threshold:
                high_corr.append((t, round(c, 2)))

    if len(high_corr) >= max_correlated:
        tickers_str = ", ".join(f"{t}({c})" for t, c in high_corr[:5])
        return False, f"zu hoch korreliert mit {len(high_corr)} Positionen: {tickers_str}"

    return True, "ok"
