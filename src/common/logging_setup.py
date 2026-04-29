"""
Zentrales Logging-Setup mit RotatingFileHandler.

Ein konsistenter Setup-Aufruf am Beginn von jedem Skript:
    from src.common.logging_setup import setup_logging
    log = setup_logging("score_portfolio")

Schreibt nach data/../logs/<name>.log mit max 5 MB pro File, 5 Backups.
Plus stdout-Handler fuer journalctl-Sichtbarkeit.

Format: [2026-04-29T17:35:16Z] LEVEL  module  message
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


_DEFAULT_LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
LOG_DIR = Path(os.environ.get("INVEST_PI_LOG_DIR", str(_DEFAULT_LOG_DIR)))
LOG_DIR.mkdir(parents=True, exist_ok=True)


def setup_logging(name: str = "invest_pi",
                  level: int = logging.INFO,
                  to_file: bool = True,
                  to_stdout: bool = True) -> logging.Logger:
    """
    Konfiguriert root-logger einmal pro Process.
    Idempotent: ein zweiter Aufruf fuegt keine doppelten Handler hinzu.
    """
    log = logging.getLogger("invest_pi")
    if log.handlers:
        # schon konfiguriert
        return logging.getLogger(f"invest_pi.{name}")

    log.setLevel(level)
    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-7s %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    fmt.converter = __import__("time").gmtime  # UTC

    if to_file:
        log_path = LOG_DIR / f"{name}.log"
        fh = RotatingFileHandler(log_path, maxBytes=5_242_880, backupCount=5)
        fh.setFormatter(fmt)
        log.addHandler(fh)

    if to_stdout:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)

    return logging.getLogger(f"invest_pi.{name}")
