#!/usr/bin/env python3
"""
dca_watchdog.py — Taegliche Ueberwachung der DCA-Positionen.

DCA-Picks sind Mid-to-Long-Term-Investments (6-18 Monate Horizont).
Dieses Script analysiert taeglich ob ein ernstes Problem vorliegt das
einen vorzeitigen Verkauf rechtfertigt — NICHT bei normaler Volatilitaet.

Sell-Signal nur bei:
  - Fundamentaler Verschlechterung (Earnings-Miss, Guidance-Cut, Downgrade-Welle)
  - Struktureller Bruch (Regulierung, Marktanteil-Verlust, CEO-Skandal)
  - Sustained Drawdown >15% mit negativem Momentum

KEIN Sell-Signal bei:
  - Normaler Markt-Volatilitaet (< -5% intraday)
  - Sektor-Rotation ohne Fundamental-Aenderung
  - Kurzfristigen Dips die sich historisch erholen

Pipeline:
  1. Finde alle DCA-Positionen (feedback_type='dca_bought')
  2. Hole aktuelle Kursdaten + historische Performance seit Kauf
  3. Pruefe Fundamental-Signale (Risk-Score, News, Earnings)
  4. Bei Concern: Sonnet-Analyse ob Halten oder Verkaufen
  5. Telegram-Alert nur bei echtem Sell-Signal

Usage:
    python scripts/dca_watchdog.py
    python scripts/dca_watchdog.py --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

env_path = Path(__file__).resolve().parents[1] / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from src.alerts import notifier
from src.common.storage import LEARNING_DB, connect

log = logging.getLogger("invest_pi.dca_watchdog")


def _get_dca_holdings() -> list[dict]:
    """Findet alle DCA-Positionen die als 'gekauft' markiert wurden."""
    with connect(LEARNING_DB) as conn:
        rows = conn.execute(
            """
            SELECT p.id AS prediction_id,
                   p.subject_id AS ticker,
                   json_extract(p.output_json, '$.ticker') AS ticker_from_output,
                   json_extract(p.output_json, '$.reason') AS buy_reason,
                   p.created_at AS recommended_at,
                   fr.created_at AS bought_at
              FROM feedback_reasons fr
              JOIN predictions p ON p.id = fr.prediction_id
             WHERE fr.feedback_type = 'dca_bought'
             ORDER BY fr.created_at DESC
            """
        ).fetchall()

    holdings = []
    for row in rows:
        ticker = row["ticker"] or row["ticker_from_output"]
        if not ticker:
            continue
        holdings.append({
            "prediction_id": row["prediction_id"],
            "ticker": ticker,
            "buy_reason": row["buy_reason"],
            "recommended_at": row["recommended_at"],
            "bought_at": row["bought_at"],
        })
    return holdings


def _get_price_data(ticker: str) -> dict | None:
    """Holt aktuelle + historische Kursdaten via yfinance."""
    try:
        import yfinance as yf
        stock = yf.Ticker(ticker)
        hist = stock.history(period="3mo")
        if hist.empty:
            return None

        current = float(hist["Close"].iloc[-1])
        high_3mo = float(hist["Close"].max())
        low_3mo = float(hist["Close"].min())

        # Performance letzte Perioden
        perf_1w = (current / float(hist["Close"].iloc[-6]) - 1) if len(hist) >= 6 else 0
        perf_1mo = (current / float(hist["Close"].iloc[-22]) - 1) if len(hist) >= 22 else 0
        perf_3mo = (current / float(hist["Close"].iloc[0]) - 1) if len(hist) > 1 else 0

        # Drawdown vom 3-Monats-Hoch
        drawdown_from_high = (current - high_3mo) / high_3mo

        # Volumen-Trend (letztes 5d avg vs 20d avg)
        vol_5d = float(hist["Volume"].tail(5).mean()) if len(hist) >= 5 else 0
        vol_20d = float(hist["Volume"].tail(20).mean()) if len(hist) >= 20 else vol_5d
        vol_ratio = vol_5d / vol_20d if vol_20d > 0 else 1.0

        return {
            "current_price": current,
            "high_3mo": high_3mo,
            "low_3mo": low_3mo,
            "perf_1w": perf_1w,
            "perf_1mo": perf_1mo,
            "perf_3mo": perf_3mo,
            "drawdown_from_high": drawdown_from_high,
            "vol_ratio": vol_ratio,
        }
    except Exception as e:
        log.warning(f"price data failed for {ticker}: {e}")
        return None


def _get_risk_score(ticker: str) -> dict | None:
    """Holt den aktuellen Risk-Score falls vorhanden."""
    try:
        from src.alerts.risk_scorer import score_ticker
        report = score_ticker(ticker)
        return {
            "composite": report.composite,
            "alert_level": report.alert_level,
            "triggered": report.triggered_dimensions,
        }
    except Exception as e:
        log.warning(f"risk score failed for {ticker}: {e}")
        return None


def _needs_deep_analysis(price_data: dict, risk_data: dict | None) -> tuple[bool, str]:
    """
    Entscheidet ob eine tiefere LLM-Analyse noetig ist.
    Returns (needs_analysis, reason).

    Nur bei echten Warnsignalen — nicht bei normaler Volatilitaet.
    """
    reasons = []

    # Drawdown > 15% vom Hoch
    if price_data["drawdown_from_high"] < -0.15:
        reasons.append(f"Drawdown {price_data['drawdown_from_high']:.0%} vom 3M-Hoch")

    # Sustained negative momentum: sowohl 1W als auch 1M negativ > -8%
    if price_data["perf_1w"] < -0.08 and price_data["perf_1mo"] < -0.10:
        reasons.append(f"Sustained drop: 1W {price_data['perf_1w']:.0%}, 1M {price_data['perf_1mo']:.0%}")

    # Ungewoehnliches Volumen bei Kurseinbruch (Panik-Selling)
    if price_data["vol_ratio"] > 2.0 and price_data["perf_1w"] < -0.05:
        reasons.append(f"High volume ({price_data['vol_ratio']:.1f}x) bei {price_data['perf_1w']:.0%} 1W drop")

    # Risk-Score alarm
    if risk_data and risk_data["alert_level"] >= 3:
        reasons.append(f"Risk alert_level={risk_data['alert_level']}, triggered={risk_data['triggered']}")

    if reasons:
        return True, "; ".join(reasons)
    return False, ""


def _llm_analyze(holding: dict, price_data: dict, risk_data: dict | None, trigger_reason: str) -> dict:
    """Sonnet-Call fuer tiefere Analyse."""
    from src.common.llm import call_sonnet, is_configured as llm_configured

    if not llm_configured():
        return {"action": "hold", "reason": "LLM nicht konfiguriert, defaulte auf Hold"}

    system = (
        "Du bist ein erfahrener Investment-Analyst. Deine Aufgabe: Bewerte ob eine "
        "Mid-to-Long-Term DCA-Position (Horizont 6-18 Monate) gehalten oder verkauft werden sollte.\n\n"
        "WICHTIG:\n"
        "- DCA-Positionen sind langfristig. Normale Markt-Volatilitaet ist KEIN Verkaufsgrund.\n"
        "- Kurzfristige Dips von -5% bis -10% sind normal und erholen sich oft.\n"
        "- Nur bei fundamentaler Verschlechterung (Earnings-Miss, Guidance-Cut, Regulierung, "
        "struktureller Marktanteil-Verlust) ist Verkaufen gerechtfertigt.\n"
        "- Sektor-Rotation allein ist kein Verkaufsgrund.\n\n"
        "Antworte ausschliesslich im JSON-Format:\n"
        "{\n"
        '  "action": "hold" | "sell" | "watch",\n'
        '  "confidence": "low" | "medium" | "high",\n'
        '  "reason": "<2-3 Saetze Begruendung>",\n'
        '  "time_horizon": "<wann re-evaluieren>"\n'
        "}\n\n"
        "- hold: Weiter halten, Einbruch ist temporaer oder nicht fundamental.\n"
        "- watch: Erhoehte Aufmerksamkeit, in 2-3 Tagen nochmal pruefen.\n"
        "- sell: Fundamentales Problem, Verlust begrenzen."
    )

    prompt = (
        f"## Position: {holding['ticker']}\n"
        f"Gekauft am: {holding['bought_at']}\n"
        f"Urspruenglicher Kaufgrund: {holding.get('buy_reason', 'unbekannt')[:300]}\n\n"
        f"## Aktuelle Kursdaten\n"
        f"- Aktueller Kurs: ${price_data['current_price']:.2f}\n"
        f"- Performance 1 Woche: {price_data['perf_1w']:+.1%}\n"
        f"- Performance 1 Monat: {price_data['perf_1mo']:+.1%}\n"
        f"- Performance 3 Monate: {price_data['perf_3mo']:+.1%}\n"
        f"- Drawdown vom 3M-Hoch: {price_data['drawdown_from_high']:.1%}\n"
        f"- Volumen-Ratio (5d/20d): {price_data['vol_ratio']:.2f}\n\n"
        f"## Risk-Assessment\n"
        f"{json.dumps(risk_data, indent=2) if risk_data else 'nicht verfuegbar'}\n\n"
        f"## Trigger fuer diese Analyse\n"
        f"{trigger_reason}\n\n"
        "Bewerte: Ist das ein kurzfristiger Dip oder ein fundamentales Problem? "
        "Soll die Position gehalten, beobachtet oder verkauft werden?"
    )

    result = call_sonnet(
        system=system,
        prompt=prompt,
        job_source="dca_watchdog",
        subject_type="ticker",
        subject_id=holding["ticker"],
        input_summary=f"DCA-watchdog {holding['ticker']}: {trigger_reason[:100]}",
        max_tokens=512,
        temperature=0.1,
        estimated_cost_eur=0.03,
    )

    if not result.ok:
        return {"action": "hold", "reason": f"LLM-Fehler: {result.error}, defaulte auf Hold"}

    parsed = result.parsed_json or {}
    return {
        "action": parsed.get("action", "hold"),
        "confidence": parsed.get("confidence", "low"),
        "reason": parsed.get("reason", result.text[:200]),
        "time_horizon": parsed.get("time_horizon", ""),
        "cost_eur": result.cost_eur,
        "prediction_id": result.prediction_id,
    }


def _send_alert(holding: dict, analysis: dict, price_data: dict) -> None:
    """Sendet Telegram-Alert bei sell oder watch."""
    action = analysis["action"]
    emoji = {"sell": "🚨", "watch": "⚠️", "hold": "✅"}.get(action, "ℹ️")

    text = (
        f"{emoji} <b>DCA-Watchdog: {holding['ticker']}</b>\n\n"
        f"Empfehlung: <b>{action.upper()}</b> "
        f"(Konfidenz: {analysis.get('confidence', '?')})\n\n"
        f"Kurs: ${price_data['current_price']:.2f} "
        f"(1W: {price_data['perf_1w']:+.1%}, 1M: {price_data['perf_1mo']:+.1%})\n"
        f"Drawdown vom Hoch: {price_data['drawdown_from_high']:.1%}\n\n"
        f"<i>{analysis.get('reason', '')}</i>"
    )
    if analysis.get("time_horizon"):
        text += f"\n\nNaechste Pruefung: {analysis['time_horizon']}"

    notifier.send_info(text, label="dca_watchdog")


def run(dry_run: bool = False) -> dict:
    """Hauptfunktion: Prueft alle DCA-Holdings."""
    holdings = _get_dca_holdings()
    if not holdings:
        log.info("keine DCA-Holdings gefunden")
        return {"holdings": 0, "checked": 0, "alerts": 0}

    print(f"DCA-Watchdog: {len(holdings)} Position(en) zu pruefen")

    results = []
    alerts_sent = 0

    for h in holdings:
        ticker = h["ticker"]
        print(f"  Pruefe {ticker}...")

        price_data = _get_price_data(ticker)
        if not price_data:
            print(f"    Keine Kursdaten fuer {ticker}, skip")
            results.append({"ticker": ticker, "status": "no_data"})
            continue

        print(f"    Kurs: ${price_data['current_price']:.2f}, "
              f"1W: {price_data['perf_1w']:+.1%}, "
              f"1M: {price_data['perf_1mo']:+.1%}, "
              f"Drawdown: {price_data['drawdown_from_high']:.1%}")

        risk_data = _get_risk_score(ticker)

        needs_analysis, trigger = _needs_deep_analysis(price_data, risk_data)

        if not needs_analysis:
            print(f"    ✅ Keine Auffaelligkeiten, Position OK")
            results.append({"ticker": ticker, "status": "ok", "action": "hold"})
            continue

        print(f"    ⚠️  Trigger: {trigger}")

        if dry_run:
            print(f"    [dry-run] Wuerde LLM-Analyse starten")
            results.append({"ticker": ticker, "status": "triggered", "trigger": trigger})
            continue

        analysis = _llm_analyze(h, price_data, risk_data, trigger)
        action = analysis.get("action", "hold")
        print(f"    Ergebnis: {action.upper()} — {analysis.get('reason', '')[:80]}")

        results.append({
            "ticker": ticker,
            "status": "analyzed",
            "action": action,
            "reason": analysis.get("reason", ""),
        })

        # Alert nur bei sell oder watch
        if action in ("sell", "watch") and notifier.is_configured():
            _send_alert(h, analysis, price_data)
            alerts_sent += 1

    summary = {
        "holdings": len(holdings),
        "checked": len(results),
        "alerts": alerts_sent,
        "results": results,
    }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Keine LLM-Calls, nur Trigger pruefen")
    args = parser.parse_args()

    result = run(dry_run=args.dry_run)
    print(f"\nDCA-Watchdog fertig: {result['holdings']} Holdings, "
          f"{result['alerts']} Alerts gesendet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
