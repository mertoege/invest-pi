"""
Config-Loader — liest config.yaml und stellt typisierte Objekte bereit.

Zentrale Wahrheitsquelle für Portfolio, Universe und Einstellungen.
Alle anderen Module importieren aus hier, nie direkt aus der YAML.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.yaml"


# ────────────────────────────────────────────────────────────
# DATENMODELLE
# ────────────────────────────────────────────────────────────
@dataclass
class Position:
    ticker:        str
    invested_eur:  float
    shares:        Optional[float]
    avg_buy_price: Optional[float]
    date_first:    str
    currency:      str
    ring:          int
    note:          str = ""


@dataclass
class WatchlistEntry:
    ticker:       str
    name:         str
    note:         str
    ring:         int
    category:     str       # z.B. "ring_1_semiconductors"
    yahoo_ticker: str = ""  # falls vom Ticker abweichend (z.B. ASML.AS)

    @property
    def fetch_ticker(self) -> str:
        """Welchen Ticker yfinance nutzen soll."""
        return self.yahoo_ticker if self.yahoo_ticker else self.ticker


@dataclass
class Settings:
    monatliches_budget_eur:   float
    max_position_anteil:      float
    max_ring1_anteil:         float
    max_tactical_anteil:      float
    drawdown_scan_threshold:  float
    alert_stufe2_trigger:     int
    alert_stufe3_trigger:     int
    dca_fallback_etf:         str
    risk_profile:             str


@dataclass
class Config:
    portfolio: dict[str, Position]
    universe:  list[WatchlistEntry]
    settings:  Settings
    api_keys:  dict[str, str]

    # ── Convenience-Properties ──────────────────────────────
    @property
    def all_tickers(self) -> list[str]:
        """Alle eindeutigen Ticker: Portfolio + Watchlist."""
        seen = set()
        result = []
        for t in [e.fetch_ticker for e in self.universe]:
            if t not in seen:
                seen.add(t)
                result.append(t)
        return result

    @property
    def portfolio_tickers(self) -> list[str]:
        return list(self.portfolio.keys())

    @property
    def watchlist_tickers(self) -> list[str]:
        return [e.fetch_ticker for e in self.universe]

    @property
    def ring1_entries(self) -> list[WatchlistEntry]:
        return [e for e in self.universe if e.ring == 1]

    @property
    def etf_entries(self) -> list[WatchlistEntry]:
        return [e for e in self.universe if e.category == "etfs"]

    def entry_by_ticker(self, ticker: str) -> Optional[WatchlistEntry]:
        for e in self.universe:
            if e.ticker == ticker or e.fetch_ticker == ticker:
                return e
        return None

    def ring_allocation(self) -> dict[int, float]:
        """Aktueller Ring-Anteil im Portfolio (nach investiertem Betrag)."""
        total = sum(p.invested_eur for p in self.portfolio.values())
        if total == 0:
            return {}
        by_ring: dict[int, float] = {}
        for ticker, pos in self.portfolio.items():
            entry = self.entry_by_ticker(ticker)
            ring = entry.ring if entry else 0
            by_ring[ring] = by_ring.get(ring, 0) + pos.invested_eur
        return {r: v / total for r, v in by_ring.items()}

    def concentration_check(self, ticker: str, add_eur: float) -> dict:
        """
        Prüfe ob ein Kauf die Konzentrationsregeln verletzt.
        Returns: {"ok": bool, "warnings": [str], "blocks": [str]}
        """
        total_after = (
            sum(p.invested_eur for p in self.portfolio.values()) + add_eur
        )
        ticker_eur_after = (
            self.portfolio.get(ticker, Position(ticker, 0, None, None, "", "USD", 0)).invested_eur
            + add_eur
        )
        warnings = []
        blocks = []

        # Einzeltitel-Grenze
        pct_single = ticker_eur_after / total_after
        if pct_single > self.settings.max_position_anteil:
            blocks.append(
                f"{ticker} wäre {pct_single:.0%} des Portfolios "
                f"(Limit: {self.settings.max_position_anteil:.0%})"
            )
        elif pct_single > self.settings.max_position_anteil * 0.8:
            warnings.append(
                f"{ticker} nähert sich dem Limit: {pct_single:.0%}"
            )

        # Ring-1-Grenze
        entry = self.entry_by_ticker(ticker)
        if entry and entry.ring == 1:
            ring1_eur_after = (
                sum(
                    p.invested_eur
                    for t, p in self.portfolio.items()
                    if (self.entry_by_ticker(t) or WatchlistEntry("", "", "", 0, "")).ring == 1
                ) + add_eur
            )
            pct_ring1 = ring1_eur_after / total_after
            if pct_ring1 > self.settings.max_ring1_anteil:
                blocks.append(
                    f"Ring-1-Anteil wäre {pct_ring1:.0%} "
                    f"(Limit: {self.settings.max_ring1_anteil:.0%})"
                )

        return {
            "ok": len(blocks) == 0,
            "warnings": warnings,
            "blocks": blocks,
            "ticker_pct_after": pct_single,
            "total_after": total_after,
        }


# ────────────────────────────────────────────────────────────
# PARSER
# ────────────────────────────────────────────────────────────
def load() -> Config:
    """Lädt und parst config.yaml. Wird gecacht nach erstem Aufruf."""
    if _cache:
        return _cache[0]

    raw = yaml.safe_load(CONFIG_PATH.read_text())

    # Portfolio
    portfolio = {}
    for ticker, data in (raw.get("portfolio") or {}).items():
        portfolio[ticker] = Position(
            ticker=ticker,
            invested_eur=float(data.get("invested_eur", 0)),
            shares=data.get("shares"),
            avg_buy_price=data.get("avg_buy_price"),
            date_first=data.get("date_first", ""),
            currency=data.get("currency", "USD"),
            ring=int(data.get("ring", 0)),
            note=data.get("note", ""),
        )

    # Universe: alle Kategorien aus universe.*
    universe: list[WatchlistEntry] = []
    seen_tickers: set[str] = set()
    ring_map = {
        # Neue Struktur (2026-05-02): Sektor-ETFs + Blue Chips
        "ring_1_sector_etfs":    1,
        "ring_1_semiconductors": 1,
        "ring_2_defensive":      2,
        "ring_2_hyperscaler":    2,
        "ring_3_speculative":    3,
        # Legacy (falls alte config geladen wird)
        "ring_1_equipment":      1,
        "ring_2_ecosystem":      2,
        "ring_3_hyperscaler":    3,
        "ring_3_software":       3,
        "ring_3_europe":         3,
        "ring_3_infra":          3,
        "etfs":                  0,
    }
    for category, entries in (raw.get("universe") or {}).items():
        ring = ring_map.get(category, 0)
        for item in (entries or []):
            t = item["ticker"]
            if t in seen_tickers:
                continue
            seen_tickers.add(t)
            universe.append(WatchlistEntry(
                ticker=t,
                name=item.get("name", t),
                note=item.get("note", ""),
                ring=ring,
                category=category,
                yahoo_ticker=item.get("yahoo_ticker", ""),
            ))

    # Settings
    s = raw.get("settings", {})
    settings = Settings(
        monatliches_budget_eur=float(s.get("monatliches_budget_eur", 50)),
        max_position_anteil=float(s.get("max_position_anteil", 0.40)),
        max_ring1_anteil=float(s.get("max_ring1_anteil", 0.70)),
        max_tactical_anteil=float(s.get("max_tactical_anteil", 0.20)),
        drawdown_scan_threshold=float(s.get("drawdown_scan_threshold", 0.15)),
        alert_stufe2_trigger=int(s.get("alert_stufe2_trigger", 50)),
        alert_stufe3_trigger=int(s.get("alert_stufe3_trigger", 75)),
        dca_fallback_etf=s.get("dca_fallback_etf", "SMH"),
        risk_profile=s.get("risk_profile", "moderate"),
    )

    # API Keys (Config überschrieben durch Env-Vars)
    import os
    api_keys = {
        "finnhub":  os.environ.get("FINNHUB_API_KEY",
                                    raw.get("api_keys", {}).get("finnhub", "")),
        "newsapi":  os.environ.get("NEWSAPI_KEY",
                                    raw.get("api_keys", {}).get("newsapi", "")),
        "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN",
                                              raw.get("api_keys", {}).get("telegram_bot_token", "")),
        "telegram_chat_id":   os.environ.get("TELEGRAM_CHAT_ID",
                                              raw.get("api_keys", {}).get("telegram_chat_id", "")),
    }

    cfg = Config(
        portfolio=portfolio,
        universe=universe,
        settings=settings,
        api_keys=api_keys,
    )
    _cache.append(cfg)
    return cfg


_cache: list[Config] = []


def reload() -> Config:
    """Erzwingt neu-Laden (nach Änderung an config.yaml)."""
    _cache.clear()
    return load()


# ────────────────────────────────────────────────────────────
# CLI: Übersicht ausgeben
# ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    cfg = load()

    print("\n╔══════════════════════════════════════════════════════╗")
    print("║  INVEST-PI · CONFIG OVERVIEW                        ║")
    print("╠══════════════════════════════════════════════════════╣")
    print(f"║  Portfolio:  {len(cfg.portfolio)} Positionen                           ║")
    print(f"║  Universe:   {len(cfg.universe)} Titel                               ║")
    print(f"║  Budget:     {cfg.settings.monatliches_budget_eur:.0f} €/Monat                           ║")
    print("╚══════════════════════════════════════════════════════╝")

    print("\n── PORTFOLIO ───────────────────────────────────────────")
    for ticker, pos in cfg.portfolio.items():
        print(f"  {ticker:<8} {pos.invested_eur:6.0f} € · {pos.note}")

    alloc = cfg.ring_allocation()
    print(f"\n  Ring-1-Anteil:  {alloc.get(1, 0):.0%}")
    print(f"  Ring-2-Anteil:  {alloc.get(2, 0):.0%}")
    print(f"  Ring-3-Anteil:  {alloc.get(3, 0):.0%}")

    print("\n── UNIVERSE ────────────────────────────────────────────")
    by_cat: dict[str, list[WatchlistEntry]] = {}
    for e in cfg.universe:
        by_cat.setdefault(e.category, []).append(e)

    for cat, entries in by_cat.items():
        label = cat.replace("_", " ").title()
        print(f"\n  {label}:")
        for e in entries:
            print(f"    {e.ticker:<8} {e.name}")

    print(f"\n  Gesamt: {len(cfg.universe)} Titel")
