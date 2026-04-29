"""
Performance-Metriken aus equity_snapshots.

Berechnet aus zeitlich geordneten Snapshots:
  - daily_returns: prozentuale Veraenderung pro Kalendertag (USD-Basis)
  - sharpe_ratio: annualisiert (252 Handelstage), risk-free 0 zur Vereinfachung
  - sortino_ratio: nur down-side-deviation
  - max_drawdown: groesste Drop vom running peak
  - calmar_ratio: annualized return / max_drawdown
  - cagr: compound annual growth rate

Alle USD-basiert (FX-resistent, siehe T37).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .storage import TRADING_DB, connect


@dataclass
class PerfMetrics:
    period_days:    int
    n_observations: int
    total_return_pct: Optional[float] = None
    cagr:           Optional[float] = None
    annual_vol:     Optional[float] = None
    sharpe:         Optional[float] = None
    sortino:        Optional[float] = None
    max_drawdown:   Optional[float] = None
    calmar:         Optional[float] = None
    last_total_usd: Optional[float] = None
    first_total_usd: Optional[float] = None


def _fetch_daily_equity(source: str, days: int) -> list[tuple[str, float]]:
    """
    Returns [(date_iso, equity_usd), ...] — eine Zeile pro Kalendertag (last snapshot).
    """
    sql = """
        SELECT date(timestamp, 'localtime') AS d,
               total_usd
          FROM equity_snapshots
         WHERE source = ?
           AND total_usd IS NOT NULL
           AND date(timestamp, 'localtime') >= date('now', ?)
         GROUP BY d
         HAVING MAX(timestamp)
         ORDER BY d
    """
    with connect(TRADING_DB) as conn:
        rows = conn.execute(sql, (source, f"-{days} day")).fetchall()
    return [(r["d"], float(r["total_usd"])) for r in rows]


def compute_metrics(source: str = "paper", days: int = 30) -> PerfMetrics:
    """Hauptfunktion. Wenn nicht genug Daten: PerfMetrics mit n_observations<2."""
    series = _fetch_daily_equity(source, days)
    if len(series) < 2:
        return PerfMetrics(period_days=days, n_observations=len(series))

    first_total = series[0][1]
    last_total  = series[-1][1]
    total_ret   = (last_total / first_total) - 1 if first_total > 0 else 0

    # Daily returns
    returns = []
    for i in range(1, len(series)):
        prev = series[i-1][1]
        curr = series[i][1]
        if prev > 0:
            returns.append((curr / prev) - 1)

    n = len(returns)
    if n == 0:
        return PerfMetrics(period_days=days, n_observations=len(series),
                           total_return_pct=total_ret,
                           first_total_usd=first_total, last_total_usd=last_total)

    mean_ret = sum(returns) / n
    var = sum((r - mean_ret) ** 2 for r in returns) / n
    std = math.sqrt(var)

    # Annualisiert: 252 Handelstage, mean_ret und std sind tag-basiert
    annual_ret = mean_ret * 252
    annual_vol = std * math.sqrt(252) if std > 0 else None
    sharpe = (annual_ret / annual_vol) if annual_vol and annual_vol > 0 else None

    # Sortino: nur Down-Deviation
    down_returns = [r for r in returns if r < 0]
    if down_returns:
        down_var = sum((r - 0) ** 2 for r in down_returns) / len(down_returns)
        down_std = math.sqrt(down_var) * math.sqrt(252)
        sortino = (annual_ret / down_std) if down_std > 0 else None
    else:
        sortino = None    # kein Verlust-Tag → unbestimmt

    # Max-Drawdown
    peak = series[0][1]
    max_dd = 0
    for _, val in series:
        if val > peak:
            peak = val
        if peak > 0:
            dd = (val / peak) - 1
            if dd < max_dd:
                max_dd = dd

    # CAGR
    period_years = max(days / 365.0, 1/365)
    if first_total > 0:
        cagr = (last_total / first_total) ** (1 / period_years) - 1
    else:
        cagr = None

    # Calmar = CAGR / |max_drawdown|
    calmar = (cagr / abs(max_dd)) if (cagr is not None and max_dd < 0) else None

    return PerfMetrics(
        period_days=days,
        n_observations=len(series),
        total_return_pct=total_ret,
        cagr=cagr,
        annual_vol=annual_vol,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=max_dd,
        calmar=calmar,
        last_total_usd=last_total,
        first_total_usd=first_total,
    )


def format_metrics(m: PerfMetrics) -> str:
    """Human-readable HTML-formattiert fuer Telegram."""
    if m.n_observations < 2:
        return f"<i>noch nicht genug Daten ({m.n_observations} snapshots)</i>"
    parts = [f"📊 <b>Performance ({m.period_days}d)</b>"]
    if m.total_return_pct is not None:
        parts.append(f"  Total: <b>{m.total_return_pct*100:+.2f}%</b>")
    if m.cagr is not None:
        parts.append(f"  CAGR: {m.cagr*100:+.1f}%/y")
    if m.annual_vol is not None:
        parts.append(f"  Vol:  {m.annual_vol*100:.1f}%/y")
    if m.sharpe is not None:
        parts.append(f"  Sharpe:  <b>{m.sharpe:.2f}</b>")
    if m.sortino is not None:
        parts.append(f"  Sortino: {m.sortino:.2f}")
    if m.max_drawdown is not None:
        parts.append(f"  Max-DD:  {m.max_drawdown*100:.1f}%")
    if m.calmar is not None:
        parts.append(f"  Calmar:  {m.calmar:.2f}")
    return "\n".join(parts)
