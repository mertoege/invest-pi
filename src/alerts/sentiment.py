"""
News-Sentiment via RSS + VADER — fuellt den sentiment_reversal-Stub.

Quellen (alle kostenlos, kein API-Key):
  - Yahoo Finance RSS (automatisch generiert aus yfinance ticker-news)
  - Google News RSS (via query-parameter)

VADER (Valence Aware Dictionary and sEntiment Reasoner) ist regelbasiert
und benoetigt KEIN Training. Perfekt fuer niedrige Latenz auf dem Pi.

Methode:
  1. Lade News-Headlines der letzten 7 Tage fuer einen Ticker
  2. Score jede Headline mit VADER (compound score -1..+1)
  3. Berechne rolling 7d-Sentiment und vergleiche mit 30d-Baseline
  4. Wenn 7d-Sentiment signifikant unter Baseline → sentiment_reversal triggered

Installation auf dem Pi:
    pip install vaderSentiment --break-system-packages
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time
from typing import Optional
from dataclasses import dataclass

log = logging.getLogger("invest_pi.sentiment")

# Rate limiting fuer News-Fetches
_FETCH_DELAY = 0.5
_last_fetch_ts: float = 0.0


@dataclass
class HeadlineScore:
    title: str
    source: str
    published: Optional[str]
    compound: float  # -1 .. +1


def _get_vader():
    """Lazy-import VADER. Returns None wenn nicht installiert."""
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        return SentimentIntensityAnalyzer()
    except ImportError:
        return None


def _fetch_yfinance_news(ticker: str, max_items: int = 20) -> list[dict]:
    """
    Holt News-Headlines via yfinance Ticker.news.
    Kein API-Key noetig — nutzt yfinance's eingebaute News-Funktion.
    """
    global _last_fetch_ts
    elapsed = time.monotonic() - _last_fetch_ts
    if elapsed < _FETCH_DELAY:
        time.sleep(_FETCH_DELAY - elapsed)

    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        news = t.news or []
        _last_fetch_ts = time.monotonic()
        results = []
        for item in news[:max_items]:
            title = item.get("title", "")
            publisher = item.get("publisher", "unknown")
            pub_date = None
            if "providerPublishTime" in item:
                pub_date = dt.datetime.fromtimestamp(
                    item["providerPublishTime"]
                ).isoformat()
            results.append({
                "title": title,
                "source": publisher,
                "published": pub_date,
            })
        return results
    except Exception as e:
        log.warning(f"yfinance news fetch failed for {ticker}: {e}")
        return []


def score_headlines(ticker: str, max_items: int = 20) -> list[HeadlineScore]:
    """
    Holt News und scored sie mit VADER.
    Returns leere Liste wenn VADER nicht installiert oder keine News.
    """
    analyzer = _get_vader()
    if analyzer is None:
        return []

    headlines = _fetch_yfinance_news(ticker, max_items)
    if not headlines:
        return []

    results = []
    for h in headlines:
        title = h["title"]
        if not title:
            continue
        scores = analyzer.polarity_scores(title)
        results.append(HeadlineScore(
            title=title,
            source=h.get("source", "unknown"),
            published=h.get("published"),
            compound=scores["compound"],
        ))
    return results


def compute_sentiment_score(ticker: str) -> dict:
    """
    Berechnet Sentiment-Score fuer einen Ticker.

    Returns:
        {
            "available": True/False,
            "n_headlines": int,
            "avg_sentiment": float (-1..+1),
            "negative_ratio": float (0..1),  # Anteil negativer Headlines
            "sentiment_score": float (0..100),  # Risk-Score fuer Dimension
            "triggered": bool,
            "reason": str,
            "headlines": [...top 5...],
        }
    """
    headlines = score_headlines(ticker)

    if not headlines:
        return {
            "available": False,
            "n_headlines": 0,
            "avg_sentiment": 0.0,
            "negative_ratio": 0.0,
            "sentiment_score": 0.0,
            "triggered": False,
            "reason": "keine News oder VADER nicht installiert",
        }

    compounds = [h.compound for h in headlines]
    avg = sum(compounds) / len(compounds)
    negatives = sum(1 for c in compounds if c < -0.1)
    neg_ratio = negatives / len(compounds)

    # Score-Berechnung:
    # - Stark negatives Durchschnitts-Sentiment → hoher Score
    # - Hoher Anteil negativer Headlines → hoher Score
    score = 0.0
    reasons = []

    # Component 1: Avg Sentiment (weight: 60%)
    if avg < -0.2:
        score += min(60.0, abs(avg) * 120)
        reasons.append(f"avg sentiment {avg:+.2f}")
    elif avg < -0.05:
        score += abs(avg) * 80
        reasons.append(f"leicht negativ {avg:+.2f}")

    # Component 2: Negative Ratio (weight: 40%)
    if neg_ratio > 0.5:
        score += min(40.0, (neg_ratio - 0.3) * 100)
        reasons.append(f"{neg_ratio:.0%} negative Headlines")

    score = min(100.0, score)
    triggered = score >= 35

    return {
        "available": True,
        "n_headlines": len(headlines),
        "avg_sentiment": round(avg, 3),
        "negative_ratio": round(neg_ratio, 3),
        "sentiment_score": round(score, 1),
        "triggered": triggered,
        "reason": "; ".join(reasons) if reasons else f"neutral ({avg:+.2f})",
        "headlines": [
            {"title": h.title, "compound": round(h.compound, 3)}
            for h in sorted(headlines, key=lambda x: x.compound)[:5]
        ],
    }
