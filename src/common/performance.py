"""
Performance-Metriken aus equity_snapshots.

Berechnet aus zeitlich geordneten Snapshots:
  - daily_returns: prozentuale Veraenderung pro Kalendertag (USD-Basis)
  - sharpe_ratio: annualisiert (252 Handelstage), risk-free 0 zur Vereinfachung
  - sortino_ratio: nur down-side-deviation
  - max_drawdown: groesste Drop vom running peak
  - calmar_ratio: annualized return / max_drawdown
  - cagr: compound annual growth rate
  - alpha vs SPY: Portfolio-Return minus SPY-Return ueber das Fenster in dem
    SPY-Daten vorliegen (ehrlich: nur ueber den tatsaechlichen Overlap)

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
    # Benchmark (SPY) — nur ueber das Fenster in dem SPY-Daten vorliegen
    spy_return_pct: Optional[float] = None
    alpha_pct:      Optional[float] = None
    benchmark_days: int = 0


def _fetch_daily_equity(source: str, days: int) -> list[tuple[str, float, Optional[float]]]:
    """
    Returns [(date_iso, equity_usd, spy_close_or_None), ...] — eine Zeile pro
    Kalendertag (jeweils letzter Snapshot des Tages).
    """
    sql = """
        SELECT date(timestamp, 'localtime') AS d,
               total_usd,
               spy_close
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
    return [(r["d"], float(r["total_usd"]),
             float(r["spy_close"]) if r["spy_close"] is not None else None)
            for r in rows]


def _compute_alpha(source: str, days: int) -> tuple[Optional[float], Optional[float], int]:
    """Alpha vs SPY ueber das Fenster in dem SPY-Daten vorliegen. Nutzt pro Tag
    den letzten Snapshot der SPY-Daten HAT (spy_close ist oft NULL wenn der Abruf
    fehlschlaegt), damit Equity und SPY zeitgleich verglichen werden.
    Returns (spy_return_pct, alpha_pct, benchmark_days)."""
    sql = """
        SELECT date(timestamp, 'localtime') AS d, total_usd, spy_close
          FROM equity_snapshots
         WHERE source = ?
           AND total_usd IS NOT NULL AND spy_close IS NOT NULL
           AND date(timestamp, 'localtime') >= date('now', ?)
         GROUP BY d
         HAVING MAX(timestamp)
         ORDER BY d
    """
    with connect(TRADING_DB) as conn:
        rows = conn.execute(sql, (source, f"-{days} day")).fetchall()
    both = [(float(r["total_usd"]), float(r["spy_close"])) for r in rows if r["spy_close"] > 0]
    if len(both) < 2 or both[0][0] <= 0 or both[0][1] <= 0:
        return None, None, len(both)
    pf_ret = both[-1][0] / both[0][0] - 1
    spy_ret = both[-1][1] / both[0][1] - 1
    return spy_ret, pf_ret - spy_ret, len(both)


def compute_metrics(source: str = "paper", days: int = 30) -> PerfMetrics:
    """Hauptfunktion. Wenn nicht genug Daten: PerfMetrics mit n_observations<2."""
    series = _fetch_daily_equity(source, days)
    if len(series) < 2:
        return PerfMetrics(period_days=days, n_observations=len(series))

    first_total = series[0][1]
    last_total  = series[-1][1]
    total_ret   = (last_total / first_total) - 1 if first_total > 0 else 0
    spy_ret, alpha, bench_days = _compute_alpha(source, days)

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
                           first_total_usd=first_total, last_total_usd=last_total,
                           spy_return_pct=spy_ret, alpha_pct=alpha, benchmark_days=bench_days)

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
    for _, val, _spy in series:
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
        spy_return_pct=spy_ret,
        alpha_pct=alpha,
        benchmark_days=bench_days,
    )


def format_metrics(m: PerfMetrics) -> str:
    """Human-readable HTML-formattiert fuer Telegram."""
    if m.n_observations < 2:
        return f"<i>noch nicht genug Daten ({m.n_observations} snapshots)</i>"
    parts = [f"📊 <b>Performance ({m.period_days}d)</b>"]
    if m.total_return_pct is not None:
        parts.append(f"  Total: <b>{m.total_return_pct*100:+.2f}%</b>")
    # Alpha vs SPY — der eigentliche Maßstab. Fenster ausweisen wenn kurz.
    if m.alpha_pct is not None:
        win = f" ({m.benchmark_days}d Bench)" if m.benchmark_days < m.n_observations else ""
        parts.append(f"  vs SPY: <b>{m.alpha_pct*100:+.2f}%</b> (SPY {m.spy_return_pct*100:+.2f}%){win}")
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
