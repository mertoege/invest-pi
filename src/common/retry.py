"""
Retry-Defaults für externe APIs.

Hintergrund (LESSONS_FOR_INVEST_PI.md Bug-Story 7):
  yfinance + Finnhub + NewsAPI sind alle netzwerk-instabil. Ein einzelner
  Hicks darf nicht den ganzen Score-Run versauen. Daher Tenacity-Decorator
  mit exponentieller Backoff-Strategie.

Usage:
    from src.common.retry import api_retry

    @api_retry()
    def fetch_finnhub_insiders(ticker, key):
        resp = requests.get(...)
        resp.raise_for_status()
        return resp.json()

    # Strenger (mehr Versuche) für kritische Pfade:
    @api_retry(attempts=5, max_wait=30)
    def critical_fetch(...): ...

    # Permissiver (weniger Versuche, schneller fail) für nice-to-have:
    @api_retry(attempts=2, max_wait=4)
    def optional_fetch(...): ...
"""

from __future__ import annotations

import logging
from typing import Any, Callable

try:
    from tenacity import (
        retry,
        retry_if_exception_type,
        stop_after_attempt,
        wait_exponential,
        before_sleep_log,
    )
    TENACITY_AVAILABLE = True
except ImportError:
    TENACITY_AVAILABLE = False


log = logging.getLogger("invest_pi.retry")

# Retry-Klassen, die typisch für transient-fails sind.
# Wir importieren defensiv damit das Modul auch ohne requests/yfinance lädt.
_RETRYABLE_EXCEPTIONS: tuple = (ConnectionError, TimeoutError, OSError)
try:
    import requests
    _RETRYABLE_EXCEPTIONS = _RETRYABLE_EXCEPTIONS + (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.HTTPError,  # für 5xx — yfinance schmeißt das
    )
except ImportError:
    pass


def api_retry(
    attempts: int = 3,
    min_wait: float = 2.0,
    max_wait: float = 10.0,
    multiplier: float = 1.0,
    exceptions: tuple = _RETRYABLE_EXCEPTIONS,
) -> Callable:
    """
    Decorator: Tenacity-basierte Retries mit exponentiellem Backoff.

    Args:
        attempts:    Maximale Anzahl Versuche (inkl. erstem). Default 3.
        min_wait:    Minimale Wartezeit in Sekunden zwischen Retries.
        max_wait:    Maximale Wartezeit in Sekunden.
        multiplier:  Backoff-Multiplikator (wait = multiplier * 2^n).
        exceptions:  Welche Exceptions retried werden. Default: Connection/Timeout.

    Falls Tenacity nicht installiert ist, wird der Decorator zum no-op
    (Funktion läuft genau einmal). Damit sind Smoke-Tests ohne tenacity möglich.
    """
    if not TENACITY_AVAILABLE:
        def passthrough(fn):
            return fn
        return passthrough

    return retry(
        stop=stop_after_attempt(attempts),
        wait=wait_exponential(multiplier=multiplier, min=min_wait, max=max_wait),
        retry=retry_if_exception_type(exceptions),
        before_sleep=before_sleep_log(log, logging.WARNING),
        reraise=True,
    )


# Vordefinierte Profile — Convenience.
yfinance_retry = api_retry(attempts=3, min_wait=2, max_wait=10)
finnhub_retry  = api_retry(attempts=4, min_wait=1, max_wait=8)   # rate-limit-aware
newsapi_retry  = api_retry(attempts=3, min_wait=2, max_wait=15)
anthropic_retry = api_retry(attempts=3, min_wait=4, max_wait=20)  # längere Backoffs für 529s
